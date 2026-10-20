"""SKU缺图匹配桌面入口；默认演示仅使用自建本地商品页。"""
from datetime import datetime
import importlib.util
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

BASE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SKU Image Recovery | Independent Demo")
        self.geometry("1040x660"); self.minsize(920, 610)
        self.configure(bg="#f3f6fa")
        self.events = queue.Queue(); self.result = None; self.report = None
        self.config_path = tk.StringVar(value=str(BASE / "6713_示例配置.json"))
        self.output_path = tk.StringVar(value=str(BASE / ("6713_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))
        self.status = tk.StringVar(value="Default configuration uses two synthetic stores served on this computer.")
        self.values = {key: tk.StringVar(value="—") for key in ("a_products", "missing_a", "downloaded", "unresolved")}
        style = ttk.Style(self); style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa"); style.configure("TLabel", background="#f3f6fa", font=("Segoe UI", 10))
        style.configure("Title.TLabel", foreground="#17374a", font=("Segoe UI", 24, "bold"))
        style.configure("Value.TLabel", foreground="#0b817b", font=("Segoe UI", 30, "bold"))
        style.configure("TButton", padding=9, font=("Segoe UI", 10))
        outer = ttk.Frame(self, padding=28); outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Recover product images by exact SKU", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="Independent portfolio demo · Static JSON-LD adapter · No upload to a store").pack(anchor="w", pady=(7, 24))
        form = ttk.Frame(outer); form.pack(fill="x"); form.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((("JSON configuration", self.config_path), ("New output folder", self.output_path))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=9, padx=(0, 14))
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=9)
        ttk.Button(form, text="Browse", command=self.choose_config).grid(row=0, column=2, padx=(12, 0))
        ttk.Button(form, text="Choose parent", command=self.choose_output).grid(row=1, column=2, padx=(12, 0))
        cards = ttk.Frame(outer); cards.pack(fill="x", pady=(22, 20))
        for column, (key, title) in enumerate((("a_products", "A PRODUCTS"), ("missing_a", "MISSING IN A"), ("downloaded", "RECOVERED FILES"), ("unresolved", "NEEDS REVIEW"))):
            cards.columnconfigure(column, weight=1)
            card = ttk.Frame(cards, padding=12); card.grid(row=0, column=column, sticky="nsew")
            ttk.Label(card, text=title, foreground="#61798c").pack(anchor="w")
            ttk.Label(card, textvariable=self.values[key], style="Value.TLabel").pack(anchor="w", pady=8)
        ttk.Label(outer, text="Missing image → Exact SKU match → Verified download → Audit", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Duplicate SKUs and unavailable images are reported for review.\nSupports static Product JSON-LD and same-origin images; no login or JavaScript execution.\nAn existing A image field is recorded as present; its link availability is not tested.", wraplength=960).pack(anchor="w", pady=(12, 18))
        controls = ttk.Frame(outer); controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Collect, match and verify", command=self.start); self.run_button.pack(side="left")
        self.open_button = ttk.Button(controls, text="Open offline report", command=self.open_report, state="disabled"); self.open_button.pack(side="left", padx=12)
        ttk.Label(outer, textvariable=self.status, wraplength=940).pack(anchor="w", pady=(20, 0))
        self.after(80, self.poll)

    def choose_config(self):
        filename = filedialog.askopenfilename(filetypes=[("JSON configuration", "*.json")])
        if filename:
            self.config_path.set(filename)

    def choose_output(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_path.set(str(Path(folder) / ("6713_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))

    def start(self):
        self.run_button.configure(state="disabled"); self.open_button.configure(state="disabled")
        self.result = None; self.report = None
        config, self.active_output = Path(self.config_path.get()), Path(self.output_path.get())
        output = self.active_output
        self.status.set("Collecting static product pages, matching SKUs and verifying downloaded images…")
        def worker():
            try:
                result = load("engine6713_gui", "6713_缺图采集.py").generate(config, output)
                self.events.put((True, result))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            ok, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.run_button.configure(state="normal")
            if ok:
                self.result = value
                for key, variable in self.values.items():
                    variable.set(f"{value[key]:,}")
                self.report = self.active_output / "6713_运行报告.html"
                self.open_button.configure(state="normal")
                self.status.set("Complete. Verified files, full matching CSV, review list and audit report are ready.")
            else:
                self.status.set("Could not complete: " + value)
                messagebox.showerror("Collection could not complete", value, parent=self)
        self.after(80, self.poll)

    def open_report(self):
        if self.report and self.report.is_file():
            webbrowser.open(self.report.resolve().as_uri())


def main():
    demo = load("demo6713_gui", "6713_生成演示网站.py")
    demo.build_demo()
    try:
        with demo.DemoServers():
            App().mainloop()
    except OSError as exc:
        raise SystemExit(f"本地演示端口8770或8771不可用，请关闭本工具之前的演示服务后重试：{exc}")


if __name__ == "__main__":
    main()
