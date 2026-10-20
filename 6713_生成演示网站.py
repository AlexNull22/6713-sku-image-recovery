"""生成原创静态商品站点和固定真值，通过两个本地 HTTP 端口演示采集。"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from functools import partial
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent
WEBROOT = BASE / "6713_演示网站"
CONFIG = BASE / "6713_示例配置.json"
SITES = ("6713_网站甲", "6713_网站乙")
PORTS = (8770, 8771)


def image_file(path: Path, sku: str, variant: int = 0):
    """原创几何图形测试图，不使用客户图片或外部图片。"""
    palettes = [(21, 103, 115), (175, 97, 53), (69, 86, 145), (92, 119, 64)]
    color = palettes[(sum(map(ord, sku)) + variant) % len(palettes)]
    image = Image.new("RGB", (360, 240), "#f1f5f7")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((120, 40, 240, 180), radius=22, fill=color)
    draw.rectangle((145, 24, 215, 47), fill=color)
    draw.rounded_rectangle((137, 90, 223, 135), radius=6, fill="white")
    draw.text((151, 105), sku, fill=color)
    draw.text((20, 210), f"SYNTHETIC PRODUCT / {sku} / {variant}", fill="#213c50")
    image.save(path, "JPEG", quality=88)


def page(title: str, body: str, data=None):
    structured = "" if data is None else '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("<", "\\u003c") + "</script>"
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>{escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{{font:16px system-ui;background:#f3f6fa;color:#17354a;max-width:1120px;margin:38px auto;padding:0 24px}}h1{{font-size:30px}}.note{{padding:16px;background:#deefe9;border-left:4px solid #16796d}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;margin:22px 0}}a{{color:#0b6b78}}.card{{background:white;padding:18px;border-radius:12px}}img,.empty{{width:100%;height:160px;object-fit:contain;background:#ecf1f5;border-radius:8px}}.empty{{display:grid;place-items:center;color:#667c8e}}code{{background:#e5edf3;padding:3px 7px}}nav{{margin:24px 0}}small{{color:#627e90}}</style>
{structured}<body><small>6713 / INDEPENDENT SYNTHETIC STOREFRONT</small><h1>{escape(title)}</h1>
<p class="note">Fictional product pages for testing an image-recovery workflow. No real customer store or commercial product data.</p>{body}</body></html>'''


def build_site(folder: Path, label: str, products: list[dict]):
    folder.mkdir()
    cards = []
    for index, product in enumerate(products):
        sku, token = product["sku"], product["token"]
        filename = f"6713_商品_{token}.html"
        image_name = f"6713_图片_{token}.jpg"
        image_url = quote(image_name) if product["image"] else None
        if product["image"]:
            if product.get("corrupt"):
                (folder / image_name).write_bytes(b"This is deliberately not a JPEG image.\n")
            else:
                image_file(folder / image_name, sku, product.get("variant", 0))
        structured = {"@context": "https://schema.org", "@type": "Product", "sku": sku, "name": f"Demo bottle {sku}"}
        if image_url:
            structured["image"] = image_url if index % 3 == 0 else ([image_url] if index % 3 == 1 else {"@type": "ImageObject", "contentUrl": image_url})
        elif index % 2:
            structured["image"] = []
        wrapped = {"@context": "https://schema.org", "@graph": [structured]} if index % 4 == 0 else ([{"@type": "Organization", "name": label}, structured] if index % 4 == 1 else structured)
        art = f'<img src="{image_url}" alt="Synthetic {escape(sku)}">' if image_url else '<div class="empty">Image missing</div>'
        body = f'<div class="card">{art}<h2>{escape(structured["name"])}</h2><p>Exact SKU: <code>{escape(sku)}</code></p><p>No order or checkout functionality.</p></div><nav><a href="{quote("6713_目录_01.html")}">Back to catalog</a></nav>'
        (folder / filename).write_text(page(f"{label} / {sku}", body, wrapped), encoding="utf-8")
        cards.append(f'<a class="card" href="{quote(filename)}">{art}<h2>{escape(sku)}</h2><small>{escape(label)}</small></a>')
    chunks = [cards[n:n + 10] for n in range(0, len(cards), 10)]
    for number, chunk in enumerate(chunks, 1):
        next_number = number + 1 if number < len(chunks) else 1
        link = f'<a rel="next" href="{quote(f"6713_目录_{next_number:02d}.html")}">{"Next page" if number < len(chunks) else "Back to first page"}</a>'
        body = f'<p>Page {number} of {len(chunks)} · {len(products)} product pages</p><div class="grid">{"".join(chunk)}</div><nav>{link}</nav>'
        (folder / f"6713_目录_{number:02d}.html").write_text(page(f"{label} catalog", body), encoding="utf-8")


def build_demo():
    expected = {f"M{i:03d}": "downloaded" for i in range(1, 21)}
    expected.update({f"M{i:03d}": "unmatched_b" for i in range(21, 25)})
    expected.update({f"M{i:03d}": "ambiguous_b" for i in range(25, 28)})
    expected.update({"M028": "missing_b_image", "M029": "download_error", "M030": "missing_b_image"})
    expected.update({f"H{i:03d}": "already_present" for i in range(1, 11)})
    truth = {"synthetic": True, "a_products": 40, "products_b": 29, "missing_a": 30, "downloaded": 20, "unresolved": 10, "expected_status_by_sku": expected}
    if WEBROOT.exists():
        marker = WEBROOT / "6713_样例真值.json"
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != truth:
            raise ValueError("演示网站目录已存在但不是本项目固定样例；请保留原文件并换新目录。")
    else:
        stage = Path(tempfile.mkdtemp(prefix="6713_造站_", dir=BASE))
        try:
            a = [{"sku": f"H{i:03d}", "token": f"H{i:03d}", "image": True} for i in range(1, 11)]
            a += [{"sku": f"M{i:03d}", "token": f"M{i:03d}", "image": False} for i in range(1, 31)]
            b = [{"sku": f"M{i:03d}", "token": f"M{i:03d}", "image": True} for i in range(1, 21)]
            b += [{"sku": f"M{i:03d}", "token": f"M{i:03d}_{v}", "image": True, "variant": v} for i in range(25, 28) for v in (1, 2)]
            b += [{"sku": "M028", "token": "M028", "image": False}, {"sku": "M029", "token": "M029", "image": True, "corrupt": True}, {"sku": "M030", "token": "M030", "image": False}]
            build_site(stage / SITES[0], "Store A", a)
            build_site(stage / SITES[1], "Store B", b)
            (stage / "6713_样例真值.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            stage.rename(WEBROOT)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    config = {"version": 1, "site_a": {"start_url": f"http://127.0.0.1:{PORTS[0]}/" + quote("6713_目录_01.html"), "product_path_prefix": "/6713_商品_"}, "site_b": {"start_url": f"http://127.0.0.1:{PORTS[1]}/" + quote("6713_目录_01.html"), "product_path_prefix": "/6713_商品_"}, "max_pages": 150, "timeout_seconds": 5, "delay_seconds": 0, "max_image_bytes": 2097152}
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return truth


class DemoHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *_):
        pass


class DemoServers(AbstractContextManager):
    def __init__(self):
        self.servers = []

    def __enter__(self):
        try:
            for folder, port in zip(SITES, PORTS):
                server = ThreadingHTTPServer(("127.0.0.1", port), partial(DemoHandler, directory=str(WEBROOT / folder)))
                self.servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        for server in self.servers:
            server.shutdown(); server.server_close()
        self.servers.clear()


def main():
    parser = argparse.ArgumentParser(description="建立原创双站测试数据，或启动本地演示服务")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, default=BASE / "6713_示例结果")
    args = parser.parse_args()
    build_demo()
    if args.serve or args.run:
        with DemoServers():
            if args.run:
                spec = importlib.util.spec_from_file_location("engine6713", BASE / "6713_缺图采集.py")
                engine = importlib.util.module_from_spec(spec); spec.loader.exec_module(engine)
                result = engine.generate(CONFIG, args.output)
                print(json.dumps({key: result[key] for key in ("a_products", "products_b", "missing_a", "downloaded", "unresolved")}, ensure_ascii=False))
            else:
                print("本地双站服务已启动：127.0.0.1:8770 / 8771。Ctrl+C 停止。", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
    else:
        print("演示网站及配置已就绪，已有文件保持不变。")


if __name__ == "__main__":
    main()
