"""Static JSON-LD Product image matching. Python 3.11+; dependency: Pillow.

Read-only requests to two configured origins; downloads are local copies only.
No browser automation, authentication bypass, or writes to either source site.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import warnings
from collections import Counter, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

AGENT = "6713StaticImageMatcher/1.0"
OUTPUTS = {"images": "6713_图片", "csv": "6713_匹配报告.csv", "review": "6713_待处理清单.csv", "json": "6713_审计记录.json", "html": "6713_运行报告.html"}
FIELDS = "sku,name,a_url,a_image_field,status,b_url,b_image_url,output_file,sha256,bytes,error,metadata_issue".split(",")


class WorkflowError(Exception):
    def __init__(self, message, issues=None):
        self.issues = list(issues or [])
        super().__init__(message + ("\n" + "\n".join("- " + item for item in self.issues) if self.issues else ""))


class AccessError(WorkflowError):
    """Restricted access or robots denial stops the entire operation."""


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError("Invalid JSON numeric constant: " + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def url(value, base=None):
    if not isinstance(value, str) or not value or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("URL must be non-empty and contain no whitespace, controls, or backslashes")
    value = urllib.parse.urljoin(base, value) if base else value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or "%" in parts.hostname:
        raise ValueError("Only HTTP(S) URLs without user information are supported")
    port = parts.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid URL port")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    host = "[" + host + "]" if ":" in host else host
    default = 443 if parts.scheme.lower() == "https" else 80
    netloc = host + (":" + str(port) if port is not None and port != default else "")
    path = urllib.parse.quote(parts.path or "/", safe="/;:@!$&'()*+,=-._~%")
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def origin(value):
    parts = urllib.parse.urlsplit(value)
    return parts.scheme, parts.netloc


def product_path(target, prefix):
    path = posixpath.normpath(urllib.parse.unquote(urllib.parse.urlsplit(target).path))
    normalized = posixpath.normpath(urllib.parse.unquote(prefix))
    if prefix.endswith("/") and normalized != "/":
        normalized += "/"
    return path.startswith(normalized)


def load_config(path):
    try:
        raw = Path(path).read_bytes()
        config = strict_json(raw.decode("utf-8-sig"))
        if not isinstance(config, dict) or set(config) != {"version", "site_a", "site_b", "max_pages", "timeout_seconds", "delay_seconds", "max_image_bytes"}:
            raise ValueError("Configuration keys must be exactly version,site_a,site_b,max_pages,timeout_seconds,delay_seconds,max_image_bytes")
        if type(config["version"]) is not int or config["version"] != 1:
            raise ValueError("version must be integer 1")
        for name, low, high in (("max_pages", 1, 500), ("timeout_seconds", 1, 30), ("max_image_bytes", 1024, 20 * 1024 * 1024)):
            if type(config[name]) is not int or not low <= config[name] <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        delay = config["delay_seconds"]
        if type(delay) not in {int, float} or not math.isfinite(delay) or not 0 <= delay <= 5:
            raise ValueError("delay_seconds must be a finite number from 0 to 5")
        for name in ("site_a", "site_b"):
            site = config[name]
            if not isinstance(site, dict) or set(site) != {"start_url", "product_path_prefix"}:
                raise ValueError(name + " requires exactly start_url and product_path_prefix")
            site["start_url"] = url(site["start_url"])
            prefix = site["product_path_prefix"]
            if not isinstance(prefix, str) or not prefix.startswith("/") or prefix.startswith("//") or re.search(r"[?#\\\x00-\x20]", prefix) or ".." in urllib.parse.unquote(prefix).split("/"):
                raise ValueError(name + ": product_path_prefix must be a safe absolute URL path")
        return config, raw
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise WorkflowError("Invalid configuration: " + str(exc)) from exc


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, client):
        self.client = client

    def redirect_request(self, request, fp, code, message, headers, newurl):
        try:
            self.client.check(url(newurl, request.full_url))
            self.client.wait()
        except Exception:
            fp.close()
            raise
        return super().redirect_request(request, fp, code, message, headers, newurl)


class Client:
    def __init__(self, site, config, throttle):
        self.site, self.config, self.throttle = site, config, throttle
        self.delay = config["delay_seconds"]
        self.allowed_origin, self.robots = origin(site["start_url"]), None
        self.opener = urllib.request.build_opener(Redirects(self))
        robots_url = urllib.parse.urlunsplit((*self.allowed_origin, "/robots.txt", "", ""))
        try:
            payload, _, final = self.fetch(robots_url, 512 * 1024)
            policy = urllib.robotparser.RobotFileParser(final)
            policy.parse(payload.decode("utf-8-sig").splitlines())
            self.robots = policy
            required = policy.crawl_delay(AGENT) or 0
            rate = policy.request_rate(AGENT)
            if rate:
                required = max(required, rate.seconds / rate.requests)
            self.delay = max(required, self.delay)
            if self.delay > 5:
                raise AccessError(f"robots.txt requires a request delay of {self.delay:g} seconds; this adapter supports at most 5 seconds")
            self.throttle[1] = max(self.throttle[1], self.delay)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code not in {404, 410}:
                    raise WorkflowError(f"Cannot establish robots policy: HTTP {exc.code}") from exc
                self.robots = urllib.robotparser.RobotFileParser(robots_url)
                self.robots.parse([])
            finally:
                exc.close()
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("Cannot establish robots policy: " + str(exc)) from exc

    def check(self, target):
        if origin(target) != self.allowed_origin:
            raise ValueError("Cross-origin URL or redirect is not permitted")
        if self.robots is not None and not self.robots.can_fetch(AGENT, target):
            raise AccessError("robots.txt disallows: " + target)

    def wait(self):
        remaining = max(self.delay, self.throttle[1]) - (time.monotonic() - self.throttle[0])
        if remaining > 0:
            time.sleep(remaining)
        self.throttle[0] = time.monotonic()

    def fetch(self, target, limit):
        target = url(target)
        self.check(target)
        self.wait()
        request = urllib.request.Request(target, headers={"User-Agent": AGENT, "Accept-Encoding": "identity"})
        try:
            with self.opener.open(request, timeout=self.config["timeout_seconds"]) as response:
                final = url(response.geturl())
                self.check(final)
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > limit:
                    raise ValueError("Response exceeds byte limit")
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise ValueError("Response exceeds byte limit")
                return payload, response.headers, final
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {401, 403, 429}:
                raise AccessError(f"Restricted or rate-limited response HTTP {exc.code}: {target}") from exc
            raise


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.links, self.scripts, self.active, self.buffer = [], [], False, []
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        next_page = "next" in attrs.get("rel", "").lower().split()
        if attrs.get("href") and (tag == "a" or (tag == "link" and next_page)):
            self.links.append((attrs["href"], next_page))
        if tag == "script" and attrs.get("type", "").lower().split(";", 1)[0].strip() == "application/ld+json":
            self.active, self.buffer = True, []

    def handle_data(self, text):
        if self.active:
            self.buffer.append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.scripts.append("".join(self.buffer))
            self.active = False


def products(node):
    if isinstance(node, list):
        for item in node:
            yield from products(item)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(isinstance(t, str) and t.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower() == "product" for t in types):
            yield node
        for key, value in node.items():
            if key != "@type" and isinstance(value, (dict, list)):
                yield from products(value)


def image_values(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [text for item in value for text in image_values(item)]
    if isinstance(value, dict):
        return image_values(value.get("contentUrl")) or image_values(value.get("url"))
    return []


def crawl(client):
    pending, queued, visited, found = deque([client.site["start_url"]]), {client.site["start_url"]}, set(), {}
    page_count = 0
    while pending:
        if page_count >= client.config["max_pages"]:
            raise WorkflowError("Page limit reached while unvisited pages remain; catalog completeness cannot be claimed.")
        target = pending.popleft()
        if target in visited:
            continue
        try:
            payload, headers, final = client.fetch(target, 2 * 1024 * 1024)
            if headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("Expected a static HTML page")
            page = Page(payload.decode(headers.get_content_charset() or "utf-8-sig"))
            nodes = [item for script in page.scripts for item in products(strict_json(script))]
            if product_path(final, client.site["product_path_prefix"]) and not nodes:
                raise ValueError("Product page contains no explicit Product JSON-LD")
            for node_index, item in enumerate(nodes):
                sku = item.get("sku")
                valid_sku = isinstance(sku, str) and bool(sku.strip())
                product_url = url(item["url"], final) if isinstance(item.get("url"), str) and item["url"].strip() else final
                client.check(product_url)
                record = {"sku": sku.strip() if valid_sku else "", "valid_sku": valid_sku, "raw_sku": sku, "name": item.get("name") if isinstance(item.get("name"), str) else sku.strip() if valid_sku else "[invalid SKU]", "url": product_url, "images": image_values(item.get("image"))}
                key = (product_url, record["sku"]) if valid_sku else (product_url, "", node_index)
                if key in found and found[key] != record:
                    raise ValueError("Conflicting Product JSON-LD for the same SKU and product URL")
                found[key] = record
            for href, next_page in page.links:
                try:
                    link = url(href, final)
                except ValueError:
                    if next_page:
                        raise
                    continue
                same = origin(link) == client.allowed_origin
                product_link = product_path(link, client.site["product_path_prefix"])
                if next_page and not same:
                    raise ValueError("Pagination leaves the configured origin")
                if same and (next_page or product_link) and link not in queued:
                    queued.add(link)
                    pending.append(link)
            visited.add(target)
            visited.add(final)
            page_count += 1
            pending = deque(link for link in pending if link not in visited)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("Catalog crawl failed; no complete report was published: " + target, [str(exc)]) from exc
    if not found:
        raise WorkflowError("No explicit Product JSON-LD was found; an empty result cannot establish completeness.")
    return sorted(found.values(), key=lambda row: (row["url"], row["sku"])), sorted(visited)


def download(client, target, directory, sku):
    payload, headers, final = client.fetch(target, client.config["max_image_bytes"])
    if any(part in headers.get_content_type().lower() for part in ("html", "svg")):
        raise ValueError("HTML and SVG responses are not supported images")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                formats = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
                if image.format not in formats or image.width * image.height > 40000000 or max(image.size) > 20000:
                    raise ValueError("Unsupported image format or excessive pixel dimensions")
                extension = formats[image.format]
                image.verify()
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
    except Exception as exc:
        raise ValueError("Invalid or unsupported image data (JPEG/PNG/WebP; maximum 40 million pixels)") from exc
    digest = hashlib.sha256(payload).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", sku).strip("_")[:45] or "SKU"
    identity = hashlib.sha256((sku + "\x00" + final + "\x00" + digest).encode("utf-8")).hexdigest()[:12]
    name = f"6713_商品_{safe}-{identity}.{extension}"
    path = directory / name
    created = False
    try:
        if any(existing.name.casefold() == name.casefold() for existing in directory.iterdir()):
            raise ValueError("Safe output filename collision; manual review is required")
        with path.open("xb") as stream:
            created = True
            stream.write(payload)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Downloaded file failed SHA256 verification")
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return name, digest, len(payload), final


def safe_csv(value):
    text = str(value)
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) else text


def write_html(path, audit):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = "".join(f'<div class="card"><span>{label}</span><strong>{audit[key]:,}</strong></div>' for key, label in [("a_products", "A PRODUCTS"), ("missing_a", "MISSING IN A"), ("downloaded", "RECOVERED FILES"), ("unresolved", "NEEDS REVIEW")])
    rows = []
    for item in audit["records"]:
        before = "Image field present" if item["a_image_field"] else "Missing image field"
        after = f'<img src="{esc(urllib.parse.quote(item["output_file"], safe="/"))}" alt="Recovered product image" loading="lazy">' if item["output_file"] else esc(item["status"].replace("_", " "))
        rows.append(f'<tr><td><b>{esc(item["sku"])}</b><small>{esc(item["name"])}</small></td><td>{before}</td><td>{after}</td><td><span class="badge {esc(item["status"])}">{esc(item["status"])}</span><small>{esc(item["error"])}</small></td><td><details><summary>Source and verification</summary><small>A: {esc(item["a_url"])}<br>B: {esc(item["b_url"])}<br>Image: {esc(item["b_image_url"])}<br>SHA256: {esc(item["sha256"])}</small></details></td></tr>')
    document = '''<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Product image recovery audit</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f8;color:#19374b;font:14px/1.5 system-ui,Segoe UI,sans-serif}main{max-width:1320px;margin:auto;padding:30px 26px}header{background:#15374b;color:white;padding:28px 30px;border-radius:13px}.eyebrow{color:#8cdecd;letter-spacing:.15em;font-size:11px;font-weight:750}h1{font-size:34px;line-height:1.15;margin:12px 0}header p{color:#c7dce7;margin:5px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:23px 0}.card{background:white;border:1px solid #d8e4ec;border-radius:11px;padding:19px}.card span{color:#698293;font-size:11px;letter-spacing:.07em}.card strong{display:block;font-size:35px;color:#0c8178;font-weight:650;margin-top:6px}.note{background:#e4f3ef;border-left:4px solid #198f7c;border-radius:5px;padding:16px 19px}section{background:white;border:1px solid #d9e5ec;border-radius:12px;padding:22px;margin-top:22px}h2{font-size:20px;margin:0 0 15px}.table{overflow:auto;border:1px solid #e0e9ef;border-radius:8px}table{width:100%;border-collapse:collapse;text-align:left}th{background:#edf4f8;color:#59788d;font-size:10px;letter-spacing:.06em}th,td{padding:13px;border-bottom:1px solid #e5edf3;vertical-align:top}td{font-size:12px;max-width:340px;overflow-wrap:anywhere}td:nth-child(2),td:nth-child(3){min-width:155px}small{display:block;color:#6b8293;font-size:11px;margin-top:5px;overflow-wrap:anywhere}img{width:98px;height:78px;object-fit:contain;border:1px solid #dce7ee;border-radius:7px;background:#f7fafb}.badge{display:inline-block;background:#fff0d8;color:#946121;padding:3px 9px;border-radius:12px;font-size:11px}.downloaded{background:#e1f4e9;color:#08765c}.already_present{background:#e8eff5;color:#4f7088}summary{color:#117e7c;cursor:pointer;font-size:11px;white-space:nowrap}footer{color:#6b8293;font-size:11px;margin-top:23px;overflow-wrap:anywhere}.policy{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:20px}.policy b{display:block;font-size:13px}.policy p{color:#6b8293;font-size:12px;margin:5px 0}@media(max-width:750px){main{padding:16px}.cards{grid-template-columns:repeat(2,1fr)}.policy{grid-template-columns:1fr}h1{font-size:27px}}@media print{body{background:white}main{padding:0}header{background:white;color:#19374b;border:1px solid #afc1ce}header p{color:#6b8293}.table{overflow:visible}td,th{font-size:9px;padding:7px}section{break-inside:avoid}}
</style><main><header><div class="eyebrow">PRODUCT IMAGE MATCHING / STATIC JSON-LD ADAPTER</div><h1>Recover missing product images with an exact SKU match</h1><p>Read-only source requests · Local image copies · Complete matching audit</p></header><div class="cards">CARDS</div><div class="note"><b>Independent synthetic/local-fixture demonstration.</b> This static JSON-LD adapter is demonstrated with self-created sites. It does not establish compatibility with an undisclosed client website. No images are uploaded to Site A, and an existing A image field is not a claim that its URL is reachable.</div><div class="policy"><div><b>Exact SKU matching</b><p>Whitespace is trimmed; case is preserved. Duplicate product matches require review.</p></div><div><b>Bounded source access</b><p>Robots policies, origin boundaries, response limits and sequential request delays are enforced.</p></div><div><b>Original image bytes</b><p>JPEG, PNG and WebP downloads are decoded and SHA256 verified without conversion or recompression.</p></div></div><section><h2>Before, after, and items needing review</h2><div class="table"><table><thead><tr><th>PRODUCT</th><th>SITE A / BEFORE</th><th>LOCAL COPY / AFTER</th><th>RESULT</th><th>AUDIT</th></tr></thead><tbody>ROWS</tbody></table></div></section><footer>Generated UTC: GENERATED. Site B products indexed: BCOUNT. All missing-A products reconcile to downloaded + unresolved. Dynamic pages, authentication, anti-bot restrictions, and non-JSON-LD adapters require a separate authorized integration. This report works offline; thumbnails refer only to local verified files.</footer></main></html>'''
    replacements = {"CARDS": cards, "ROWS": "".join(rows), "GENERATED": esc(audit["generated_at"]), "BCOUNT": str(audit["products_b"])}
    Path(path).write_text(re.sub(r"\b(CARDS|ROWS|GENERATED|BCOUNT)\b", lambda m: replacements[m[0]], document), encoding="utf-8")


def generate(config_path, output_dir):
    config, original = load_config(config_path)
    source, raw_output = Path(config_path).resolve(), Path(output_dir).absolute()
    if raw_output.is_symlink():
        raise WorkflowError("Output directory cannot be a symbolic link")
    output = raw_output.resolve()
    if output == source or output in source.parents:
        raise WorkflowError("Output directory must not contain the source configuration")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise WorkflowError("Output directory must be new or empty; existing files are never overwritten")
    throttle = [0.0, config["delay_seconds"]]
    client_a, client_b = Client(config["site_a"], config, throttle), Client(config["site_b"], config, throttle)
    a, pages_a = crawl(client_a)
    b, pages_b = crawl(client_b)
    counts_a, index_b = Counter(item["sku"] for item in a if item["valid_sku"]), {}
    for item in b:
        if item["valid_sku"]:
            index_b.setdefault(item["sku"], []).append(item)
    stage = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="6713_采集暂存_", dir=output.parent))
        directory = stage / OUTPUTS["images"]
        directory.mkdir()
        records = []
        for item in a:
            row = dict.fromkeys(FIELDS, "")
            row.update(sku=item["sku"], name=item["name"], a_url=item["url"], a_image_field=bool(item["images"]), bytes=0)
            if not item["valid_sku"]:
                row["metadata_issue"] = "Product SKU is absent, blank, or not a string"
                row["error"] = row["metadata_issue"]
            matches = index_b.get(item["sku"], [])
            if item["images"]:
                row["status"] = "already_present"
            elif not item["valid_sku"]:
                row["status"] = "invalid_sku"
            elif counts_a[item["sku"]] > 1:
                row["status"] = "ambiguous_a"
            elif len(matches) > 1:
                row["status"] = "ambiguous_b"
            elif not matches:
                row["status"] = "unmatched_b"
            else:
                match = matches[0]
                row["b_url"] = match["url"]
                if not match["images"]:
                    row["status"] = "missing_b_image"
                else:
                    try:
                        row["b_image_url"] = url(match["images"][0], match["url"])
                        name, digest, size, final = download(client_b, row["b_image_url"], directory, item["sku"])
                        row.update(status="downloaded", output_file=OUTPUTS["images"] + "/" + name, sha256=digest, bytes=size, b_image_url=final)
                    except AccessError:
                        raise
                    except Exception as exc:
                        row.update(status="download_error", error=str(exc))
            records.append(row)
        missing = [row for row in records if not row["a_image_field"]]
        recovered = sum(row["status"] == "downloaded" for row in missing)
        expected_files = {Path(row["output_file"]).name for row in records if row["status"] == "downloaded"}
        if {path.name for path in directory.iterdir()} != expected_files:
            raise WorkflowError("Image directory does not match verified download records; no outputs were published")
        audit = {"schema_version": 1, "status": "complete", "a_products": len(a), "missing_a": len(missing), "downloaded": recovered, "unresolved": len(missing) - recovered, "products_b": len(b), "invalid_a_products": sum(not item["valid_sku"] for item in a), "invalid_b_products": sum(not item["valid_sku"] for item in b), "invalid_b_urls": [item["url"] for item in b if not item["valid_sku"]], "invalid_b_records": [{"url": item["url"], "name": item["name"], "raw_sku": item["raw_sku"]} for item in b if not item["valid_sku"]], "records": records, "outputs": dict(OUTPUTS), "status_counts": dict(Counter(row["status"] for row in records)), "source_config": source.name, "source_config_sha256": hashlib.sha256(original).hexdigest(), "config": config, "crawled_pages": {"site_a": pages_a, "site_b": pages_b}, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for key, rows in (("csv", records), ("review", [row for row in missing if row["status"] != "downloaded"])):
            with (stage / OUTPUTS[key]).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows({name: safe_csv(row[name]) for name in FIELDS} for row in rows)
        (stage / OUTPUTS["json"]).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        write_html(stage / OUTPUTS["html"], audit)
        if source.read_bytes() != original:
            raise WorkflowError("Configuration changed during execution; no files were published")
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
        stage = None
        return audit
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError("Image workflow failed; no formal outputs were published: " + str(exc)) from exc
    finally:
        if stage is not None and stage.parent.resolve() == output.parent and stage.name.startswith("6713_采集暂存_"):
            shutil.rmtree(stage)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Match missing product images by exact SKU using static JSON-LD sites")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(args.config, args.output)
        print(json.dumps({key: result[key] for key in ("status", "a_products", "missing_a", "downloaded", "unresolved", "products_b", "outputs")}, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("ERROR: Unexpected failure: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
