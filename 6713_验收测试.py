"""缺图匹配工具的业务验收；仅访问两个临时本机HTTP站点，不访问真实网站。"""
from __future__ import annotations

from collections import Counter
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote, unquote, urlsplit
import zlib


ENGINE_PATH = Path(__file__).with_name("6713_缺图采集.py")
SPEC = importlib.util.spec_from_file_location("missing_images_6713", ENGINE_PATH)
engine = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = engine
SPEC.loader.exec_module(engine)


def png_bytes(width=8, height=6, noisy=False):
    """用标准库创建真正可解码的RGB PNG，样本全部自制。"""
    rng = random.Random(6713)
    rows = []
    for y in range(height):
        pixels = bytes(rng.randrange(256) for _ in range(width * 3)) if noisy else bytes(
            component for x in range(width) for component in ((x * 31) % 256, (y * 43) % 256, 170))
        rows.append(b"\x00" + pixels)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


class LocalSite:
    def __init__(self):
        self.routes = {}
        self.requests = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                path = unquote(parsed.path) + ("?" + parsed.query if parsed.query else "")
                with fixture.lock:
                    fixture.requests.append(path)
                    response = fixture.routes.get(path, (404, {"Content-Type": "text/plain"}, b"Not found"))
                status, headers, body = response
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.set("/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")

    def set(self, path, body, status=200, content_type="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        with self.lock:
            self.routes[path] = (status, {"Content-Type": content_type, **(headers or {})}, body)

    def redirect(self, path, target):
        self.set(path, b"", status=302, headers={"Location": quote(target, safe="/:?#[]@!$&'()*+,;=%")})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class MissingImageAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="6713_验收_")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.a, self.b = LocalSite(), LocalSite()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.image = png_bytes()
        self.b.set("/images/6713_样本.png", self.image, content_type="image/png")
        self.catalog(self.a, ["/product/a"])
        self.product(self.a, "/product/a", "SKU-001")
        self.catalog(self.b, ["/product/b"])
        self.product(self.b, "/product/b", "SKU-001", image="/images/6713_样本.png")

    @staticmethod
    def catalog(site, links, path="/catalog", next_page=None):
        body = "<!doctype html><html><body>" + "".join(f'<a href="{link}">Synthetic product</a>' for link in links)
        if next_page:
            body += f'<a rel="next" href="{next_page}">Next</a>'
        site.set(path, body + "</body></html>")

    @staticmethod
    def jsonld(site, path, payload):
        text = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        site.set(path, '<!doctype html><html><body><script type="application/ld+json">' + text + "</script></body></html>")

    def product(self, site, path, sku, image=None, name="Synthetic product"):
        payload = {"@context": "https://schema.org", "@type": "Product", "sku": sku, "name": name}
        if image is not None:
            payload["image"] = image
        self.jsonld(site, path, payload)

    def config(self, **changes):
        return {"version": 1,
                "site_a": {"start_url": self.a.url + "/catalog", "product_path_prefix": "/product/"},
                "site_b": {"start_url": self.b.url + "/catalog", "product_path_prefix": "/product/"},
                "max_pages": 20, "timeout_seconds": 3, "delay_seconds": 0, "max_image_bytes": 1024 * 1024, **changes}

    def write_config(self, config=None, tag="默认"):
        path = self.root / f"6713_配置_{tag}.json"
        path.write_text(json.dumps(self.config() if config is None else config, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def generate(self, config=None, tag="默认"):
        source = self.write_config(config, tag)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        output = self.root / f"6713_中文输出_{tag}"
        audit = engine.generate(source, output)
        self.assertEqual(set(audit["outputs"]), {"images", "csv", "review", "json", "html"})
        paths = {key: output / value for key, value in audit["outputs"].items()}
        for key, path in paths.items():
            self.assertEqual(path.parent, output)
            self.assertTrue(path.name.startswith("6713_"))
            self.assertTrue(path.is_dir() if key == "images" else path.is_file())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash, "配置源文件被修改")
        self.assertEqual(audit["a_products"], len(audit["records"]))
        self.assertEqual(audit["missing_a"], audit["downloaded"] + audit["unresolved"])
        return audit, paths

    def assert_blocked(self, config=None, tag="默认"):
        source = self.write_config(config, "拒绝" + tag)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        output = self.root / f"6713_拒绝输出_{tag}"
        with self.assertRaises(engine.WorkflowError) as caught:
            engine.generate(source, output)
        self.assertTrue(str(caught.exception))
        self.assertFalse(output.exists() and any(output.iterdir()), "失败运行发布了部分结果")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    @staticmethod
    def csv_records(path):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_paginated_catalogs_stop_cycles_and_visit_robots_before_products(self):
        self.catalog(self.a, ["/product/a"], next_page="/catalog?page=2")
        self.catalog(self.a, ["/product/a2"], path="/catalog?page=2", next_page="/catalog")
        self.product(self.a, "/product/a2", "SKU-002")
        self.catalog(self.b, ["/product/b"], next_page="/catalog?page=2")
        self.catalog(self.b, ["/product/b2"], path="/catalog?page=2", next_page="/catalog")
        self.product(self.b, "/product/b2", "SKU-002", image="/images/6713_样本.png")
        audit, _ = self.generate()
        self.assertEqual((audit["a_products"], audit["products_b"], audit["downloaded"], audit["unresolved"]), (2, 2, 2, 0))
        for site in (self.a, self.b):
            self.assertEqual(site.requests[0], "/robots.txt")
            self.assertEqual(Counter(site.requests)["/catalog"], 1)
            self.assertEqual(Counter(site.requests)["/catalog?page=2"], 1)

    def test_matching_is_exact_case_sensitive_and_does_not_use_similar_names(self):
        self.catalog(self.a, ["/product/a", "/product/a2", "/product/a3"])
        self.product(self.a, "/product/a", "Case-001", name="Same item")
        self.product(self.a, "/product/a2", "AA-01", name="Same item")
        self.product(self.a, "/product/a3", "  EXACT  ", name="Different source name")
        self.catalog(self.b, ["/product/b", "/product/b2", "/product/b3"])
        self.product(self.b, "/product/b", "case-001", image="/images/6713_样本.png", name="Same item")
        self.product(self.b, "/product/b2", "AA01", image="/images/6713_样本.png", name="Same item")
        self.product(self.b, "/product/b3", "EXACT", image="/images/6713_样本.png", name="Another name")
        audit, _ = self.generate()
        self.assertEqual({row["sku"]: row["status"] for row in audit["records"]}, {"Case-001": "unmatched_b", "AA-01": "unmatched_b", "EXACT": "downloaded"})

    def test_duplicate_b_sku_remains_ambiguous_even_with_identical_image(self):
        self.catalog(self.b, ["/product/b", "/product/b2"])
        self.product(self.b, "/product/b2", "SKU-001", image="/images/6713_样本.png")
        audit, paths = self.generate()
        self.assertEqual(audit["records"][0]["status"], "ambiguous_b")
        self.assertEqual((audit["downloaded"], audit["unresolved"]), (0, 1))
        self.assertFalse(list(paths["images"].iterdir()))
        self.assertNotIn("/images/6713_样本.png", self.b.requests)

    def test_a_duplicates_are_ambiguous_but_existing_image_remains_present(self):
        self.catalog(self.a, ["/product/a", "/product/a2", "/product/a3"])
        self.product(self.a, "/product/a2", "SKU-001")
        self.product(self.a, "/product/a3", "SKU-001", image="/images/not-requested.png")
        audit, _ = self.generate()
        self.assertEqual(Counter(row["status"] for row in audit["records"]), {"ambiguous_a": 2, "already_present": 1})
        self.assertEqual((audit["missing_a"], audit["downloaded"], audit["unresolved"]), (2, 0, 2))
        self.assertNotIn("/images/not-requested.png", self.a.requests)

    def test_missing_or_blank_sku_is_unresolved_without_guessing(self):
        self.catalog(self.a, ["/product/a", "/product/a2", "/product/a3"])
        self.jsonld(self.a, "/product/a", {"@type": "Product", "name": "No SKU"})
        self.product(self.a, "/product/a2", "   ")
        self.product(self.a, "/product/a3", 123)
        audit, _ = self.generate()
        self.assertEqual([row["status"] for row in audit["records"]], ["invalid_sku"] * 3)
        self.assertEqual((audit["downloaded"], audit["unresolved"]), (0, 3))

    def test_product_arrays_graphs_and_image_objects_are_extracted(self):
        paths = ["/product/a", "/product/a2", "/product/a3"]
        self.catalog(self.a, paths)
        for path, sku in zip(paths, ("ARRAY", "GRAPH", "OBJECT")):
            self.product(self.a, path, sku, image=[] if sku == "ARRAY" else None)
        self.catalog(self.b, ["/product/b", "/product/b2", "/product/b3"])
        self.jsonld(self.b, "/product/b", [{"@type": "BreadcrumbList"}, {"@type": "Product", "sku": "ARRAY", "name": "Array", "image": ["/images/6713_样本.png"]}])
        self.jsonld(self.b, "/product/b2", {"@graph": [{"@type": "Product", "sku": "GRAPH", "name": "Graph", "image": {"@type": "ImageObject", "url": "/images/6713_样本.png"}}]})
        self.jsonld(self.b, "/product/b3", {"@type": "Product", "sku": "OBJECT", "name": "Object", "image": {"@type": "ImageObject", "contentUrl": "/images/6713_样本.png"}})
        audit, _ = self.generate()
        self.assertEqual((audit["a_products"], audit["products_b"], audit["downloaded"]), (3, 3, 3))
        self.assertTrue(all(row["status"] == "downloaded" for row in audit["records"]))

    def test_missing_b_image_is_distinct_from_absent_b_product(self):
        self.product(self.b, "/product/b", "SKU-001", image=[])
        audit, paths = self.generate()
        self.assertEqual(audit["records"][0]["status"], "missing_b_image")
        self.assertEqual(len(self.csv_records(paths["review"])), 1)

    def test_nonstring_b_sku_is_audited_and_never_converted_into_a_match(self):
        self.product(self.a, "/product/a", "123")
        self.product(self.b, "/product/b", 123, image="/images/6713_样本.png")
        audit, _ = self.generate()
        self.assertEqual(audit["records"][0]["status"], "unmatched_b")
        self.assertEqual(audit["invalid_b_products"], 1)
        self.assertNotIn("/images/6713_样本.png", self.b.requests)

    def test_robots_disallow_blocks_before_requesting_forbidden_product(self):
        self.a.set("/robots.txt", "User-agent: *\nDisallow: /product/\n", content_type="text/plain")
        self.assert_blocked()
        self.assertNotIn("/product/a", self.a.requests)

    def test_robots_403_does_not_start_catalog_crawl(self):
        self.a.set("/robots.txt", "Forbidden", status=403, content_type="text/plain")
        self.assert_blocked()
        self.assertNotIn("/catalog", self.a.requests)

    def test_http_403_and_429_are_full_run_failures(self):
        for status in (403, 429):
            with self.subTest(status=status):
                self.b.set("/product/b", "Access unavailable", status=status)
                self.assert_blocked(tag=str(status))

    def test_restricted_or_robots_denied_images_stop_the_entire_run(self):
        for status in (403, 429):
            with self.subTest(image_status=status):
                self.b.set("/images/6713_样本.png", "Access unavailable", status=status, content_type="text/plain")
                self.assert_blocked(tag=f"图片状态{status}")
        self.b.set("/images/6713_样本.png", self.image, content_type="image/png")
        self.b.set("/robots.txt", "User-agent: *\nDisallow: /images/\n", content_type="text/plain")
        before = self.b.requests.count("/images/6713_样本.png")
        self.assert_blocked(tag="图片robots")
        self.assertEqual(self.b.requests.count("/images/6713_样本.png"), before)

    def test_restriction_after_a_successful_download_publishes_no_partial_batch(self):
        self.product(self.a, "/product/a", "AAA")
        self.catalog(self.a, ["/product/a", "/product/a2"])
        self.product(self.a, "/product/a2", "ZZZ")
        self.product(self.b, "/product/b", "AAA", image="/images/6713_样本.png")
        self.catalog(self.b, ["/product/b", "/product/b2"])
        self.product(self.b, "/product/b2", "ZZZ", image="/images/6713_forbidden.png")
        self.b.set("/images/6713_forbidden.png", "Forbidden", status=403, content_type="text/plain")
        self.assert_blocked(tag="已下载后限制")
        self.assertIn("/images/6713_样本.png", self.b.requests)
        self.assertIn("/images/6713_forbidden.png", self.b.requests)

    def test_empty_catalog_or_recognized_product_without_product_data_fails(self):
        self.catalog(self.a, [])
        self.assert_blocked(tag="空目录")
        self.catalog(self.a, ["/product/a"])
        self.a.set("/product/a", "<!doctype html><html><body>Navigation only</body></html>")
        self.assert_blocked(tag="无商品数据")
        self.product(self.a, "/product/a", "SKU-001")
        self.catalog(self.b, [])
        self.assert_blocked(tag="空B目录")

    def test_catalog_limit_cannot_publish_incomplete_inventory(self):
        self.catalog(self.a, ["/product/a"], next_page="/catalog?page=2")
        self.catalog(self.a, ["/product/a2"], path="/catalog?page=2")
        self.product(self.a, "/product/a2", "SKU-002")
        self.assert_blocked(self.config(max_pages=1))

    def test_non_html_product_response_is_not_silently_skipped(self):
        self.b.set("/product/b", '{"sku":"SKU-001"}', content_type="application/json")
        self.assert_blocked()

    def test_cross_origin_catalog_redirect_is_refused_before_target_request(self):
        self.b.redirect("/catalog", self.a.url + "/6713_越界目标")
        self.a.set("/6713_越界目标", "Not an allowed redirect")
        self.assert_blocked()
        self.assertNotIn("/6713_越界目标", self.a.requests)

    def test_cross_origin_image_and_redirect_are_row_errors_without_external_fetch(self):
        target = self.a.url + "/6713_越界图片.png"
        self.a.set("/6713_越界图片.png", self.image, content_type="image/png")
        for index, image_url in enumerate((target, "/images/6713_重定向.png")):
            with self.subTest(image_url=image_url):
                self.b.redirect("/images/6713_重定向.png", target)
                self.product(self.b, "/product/b", "SKU-001", image=image_url)
                audit, paths = self.generate(tag=f"越界图片{index}")
                self.assertEqual(audit["records"][0]["status"], "download_error")
                self.assertFalse(list(paths["images"].iterdir()))
                self.assertNotIn("/6713_越界图片.png", self.a.requests)
                if index == 1:
                    self.assertIn("/images/6713_重定向.png", self.b.requests)

    def test_claimed_image_containing_html_is_rejected(self):
        self.b.set("/images/6713_样本.png", b"<html>This is not an image</html>", content_type="image/png")
        audit, paths = self.generate()
        self.assertEqual(audit["records"][0]["status"], "download_error")
        self.assertTrue(audit["records"][0]["error"])
        self.assertFalse(list(paths["images"].iterdir()))

    def test_image_byte_limit_rejects_a_real_but_oversized_png(self):
        oversized = png_bytes(64, 64, noisy=True)
        self.assertGreater(len(oversized), 1024)
        self.b.set("/images/6713_样本.png", oversized, content_type="image/png")
        audit, paths = self.generate(self.config(max_image_bytes=1024))
        self.assertEqual(audit["records"][0]["status"], "download_error")
        self.assertEqual((audit["downloaded"], audit["unresolved"]), (0, 1))
        self.assertFalse(list(paths["images"].iterdir()))

    def test_one_bad_image_is_reviewed_while_other_exact_match_is_downloaded(self):
        self.catalog(self.a, ["/product/a", "/product/a2"])
        self.product(self.a, "/product/a2", "GOOD")
        self.product(self.b, "/product/b", "SKU-001", image="/images/not-found.png")
        self.catalog(self.b, ["/product/b", "/product/b2"])
        self.product(self.b, "/product/b2", "GOOD", image="/images/6713_样本.png")
        audit, paths = self.generate()
        self.assertEqual({row["sku"]: row["status"] for row in audit["records"]}, {"SKU-001": "download_error", "GOOD": "downloaded"})
        self.assertEqual((audit["downloaded"], audit["unresolved"]), (1, 1))
        self.assertEqual(len(list(paths["images"].iterdir())), 1)

    def test_output_records_hashes_and_review_csv_reconcile(self):
        self.catalog(self.a, ["/product/a", "/product/a2", "/product/a3"])
        self.product(self.a, "/product/a2", "PRESENT", image="/images/not-requested.png")
        self.product(self.a, "/product/a3", "NOT-IN-B")
        audit, paths = self.generate()
        self.assertEqual((audit["a_products"], audit["missing_a"], audit["downloaded"], audit["unresolved"]), (3, 2, 1, 1))
        self.assertEqual(len(self.csv_records(paths["csv"])), 3)
        review = self.csv_records(paths["review"])
        self.assertEqual([(row["sku"], row["status"]) for row in review], [("NOT-IN-B", "unmatched_b")])
        saved = json.loads(paths["json"].read_text(encoding="utf-8-sig"))
        self.assertEqual(saved["records"], audit["records"])
        downloaded = next(row for row in audit["records"] if row["status"] == "downloaded")
        output_file = paths["images"].parent / downloaded["output_file"]
        self.assertEqual(output_file.parent, paths["images"])
        self.assertEqual(output_file.read_bytes(), self.image)
        self.assertEqual(downloaded["sha256"], hashlib.sha256(self.image).hexdigest())
        self.assertEqual(downloaded["bytes"], len(self.image))

    def test_untrusted_product_text_is_safe_in_reports_and_filename(self):
        payload = '<img src=x onerror="alert(6713)">'
        self.product(self.a, "/product/a", "=1+1", name=payload)
        self.product(self.b, "/product/b", "=1+1", image="/images/6713_样本.png", name=payload)
        audit, paths = self.generate()
        self.assertEqual(audit["records"][0]["sku"], "=1+1")
        self.assertEqual(self.csv_records(paths["csv"])[0]["sku"], "'=1+1")
        document = paths["html"].read_text(encoding="utf-8")
        self.assertNotIn(payload, document)
        self.assertIn("&lt;img src=x", document)
        image_path = paths["images"].parent / audit["records"][0]["output_file"]
        self.assertEqual(image_path.parent, paths["images"])
        self.assertTrue(image_path.name.startswith("6713_"))
        self.assertNotIn("=", image_path.name)

    def test_existing_output_is_preserved(self):
        config = self.write_config()
        before = config.read_bytes()
        output = self.root / "6713_已有输出"
        output.mkdir()
        sentinel = output / "6713_原有文件.txt"
        sentinel.write_text("keep unchanged", encoding="utf-8")
        with self.assertRaises(engine.WorkflowError):
            engine.generate(config, output)
        self.assertEqual([path.name for path in output.iterdir()], [sentinel.name])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep unchanged")
        self.assertEqual(config.read_bytes(), before)

    def test_config_rejects_unknown_keys_invalid_bounds_and_non_http_sources(self):
        cases = [self.config(max_pages=0), self.config(max_pages=True), self.config(timeout_seconds=31),
                 self.config(delay_seconds=-1), self.config(max_image_bytes=1023), self.config(unexpected=True)]
        bad = self.config()
        bad["site_a"]["start_url"] = "file:///C:/6713_禁止访问.html"
        cases.append(bad)
        for index, config in enumerate(cases):
            with self.subTest(index=index):
                self.assert_blocked(config, tag=f"配置{index}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
