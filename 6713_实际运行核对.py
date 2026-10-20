"""用固定真值核验完整双站采集，再经真实 Tkinter 入口重复执行；不覆盖已有成果。"""
from collections import Counter
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PIL import Image

BASE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(directory):
    return {path.relative_to(directory).as_posix(): sha(path) for path in directory.rglob("*") if path.is_file()}


def require(value, message):
    if not value:
        raise AssertionError(message)


def validate(output, truth):
    audit = json.loads((output / "6713_审计记录.json").read_text(encoding="utf-8"))
    for key in ("a_products", "products_b", "missing_a", "downloaded", "unresolved"):
        require(audit[key] == truth[key], f"Unexpected {key}: {audit[key]}")
    statuses = {row["sku"]: row["status"] for row in audit["records"]}
    require(statuses == truth["expected_status_by_sku"], "Status by SKU differs from independent fixed truth")
    require(len(audit["records"]) == 40 and audit["missing_a"] == audit["downloaded"] + audit["unresolved"], "Counts do not reconcile")
    recovered = [row for row in audit["records"] if row["status"] == "downloaded"]
    files = [path for path in (output / "6713_图片").iterdir() if path.suffix.lower() in {".jpg", ".png", ".webp"}]
    require(len(files) == 20, "Recovered directory has extra or missing files")
    hashes = {}
    for row in recovered:
        path = output / row["output_file"]
        require(path.is_file() and sha(path) == row["sha256"] and path.stat().st_size == row["bytes"], "Recovered file checksum mismatch")
        with Image.open(path) as image:
            image.verify()
        source_image = BASE / "6713_演示网站" / "6713_网站乙" / f"6713_图片_{row['sku']}.jpg"
        require(path.read_bytes() == source_image.read_bytes(), "Image bytes were changed from the source fixture")
        hashes[row["sku"]] = sha(path)
    with (output / "6713_匹配报告.csv").open(encoding="utf-8-sig", newline="") as stream:
        all_rows = list(csv.DictReader(stream))
    with (output / "6713_待处理清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        pending_rows = list(csv.DictReader(stream))
    require(len(all_rows) == 40 and len(pending_rows) == 10, "CSV record counts differ")
    require({row["sku"] for row in pending_rows} == {sku for sku, status in statuses.items() if status not in {"downloaded", "already_present"}}, "Review CSV contains an incorrect SKU")
    for row in all_rows:
        require(row["status"] == statuses[row["sku"]], "CSV status mismatch")
    return audit, hashes


def main():
    demo = load("demo6713_check", "6713_生成演示网站.py")
    demo.build_demo()
    truth = json.loads((demo.WEBROOT / "6713_样例真值.json").read_text(encoding="utf-8"))
    config = BASE / "6713_示例配置.json"
    baseline = BASE / "6713_示例结果" if (BASE / "6713_示例结果").is_dir() else BASE
    if not (baseline / "6713_审计记录.json").is_file():
        raise SystemExit("请先运行 python 6713_生成演示网站.py --run --output 6713_示例结果")
    source_before, baseline_before, config_before = fingerprint(demo.WEBROOT), fingerprint(baseline), config.read_bytes()
    first, first_hashes = validate(baseline, truth)
    gui = load("gui6713_check", "6713_桌面工具.py")
    errors, opened = [], []
    with tempfile.TemporaryDirectory(prefix="6713_桌面复核_") as temp, demo.DemoServers():
        second = Path(temp) / "6713_第二遍结果"
        app = gui.App(); app.withdraw(); app.output_path.set(str(second))
        try:
            with patch.object(gui.messagebox, "showerror", side_effect=lambda *args, **kwargs: errors.append(str(args))), patch.object(gui.webbrowser, "open", side_effect=opened.append):
                app.run_button.invoke()
                require(str(app.run_button["state"]) == "disabled", "Run button did not disable while busy")
                app.output_path.set(str(Path(temp) / "6713_编辑后的路径"))
                deadline = time.monotonic() + 35
                while app.result is None and not errors and time.monotonic() < deadline:
                    app.update(); time.sleep(.015)
                require(not errors and app.result is not None, "GUI generation failed: " + "; ".join(errors))
                expected = {key: str(truth[key]) for key in ("a_products", "missing_a", "downloaded", "unresolved")}
                require({key: variable.get() for key, variable in app.values.items()} == expected, "GUI counters differ from fixed truth")
                require(app.report == second / "6713_运行报告.html", "Report path followed an edited entry instead of actual output")
                app.open_button.invoke()
                require(opened == [app.report.resolve().as_uri()], "Report button did not open the actual report")
                require(str(app.run_button["state"]) == "normal" and str(app.open_button["state"]) == "normal", "GUI controls did not recover")
                other, second_hashes = validate(second, truth)
                require(first_hashes == second_hashes, "Repeated downloads changed image bytes")
                require({r["sku"]: r["status"] for r in first["records"]} == {r["sku"]: r["status"] for r in other["records"]}, "Repeated matching changed decisions")
                require(not (Path(temp) / "6713_编辑后的路径").exists(), "Edited output entry unexpectedly created files")
        finally:
            app.destroy()
    require(config.read_bytes() == config_before, "Configuration was modified")
    require(fingerprint(demo.WEBROOT) == source_before, "Source fixture files were modified")
    require(fingerprint(baseline) == baseline_before, "Existing output files were modified")
    record = {"status": "passed", "scope": "Independent synthetic local HTTP stores; no client site access", "counts": {key: truth[key] for key in ("a_products", "products_b", "missing_a", "downloaded", "unresolved")}, "status_counts": dict(Counter(r["status"] for r in first["records"])), "checks": {"all_40_statuses_match_fixed_truth": True, "all_20_images_decode": True, "all_20_original_bytes_and_sha256_match": True, "full_csv_40_and_review_csv_10_match": True, "gui_real_event_loop_and_worker": True, "gui_report_button_actual_output": True, "second_run_same_decisions_and_image_bytes": True, "source_config_and_baseline_unchanged": True, "temporary_output_cleaned": True}, "source_config_sha256": hashlib.sha256(config_before).hexdigest(), "image_sha256_by_sku": first_hashes}
    (BASE / "6713_实际运行核对记录.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
