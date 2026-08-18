"""
License签发台 — 独立桌面程序（厂商侧离线签发，Windows exe）

功能：选私钥 → 填客户/到期日/模块 →（可选）绑机器指纹 → 生成 license.key
签名算法与系统后端完全一致：RSA PKCS#1 v1.5 + SHA-256，base64url 拼接。

打包（在本项目 .venv 下）：
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name "License签发台" tools/license_signer.py

自测（命令行，不弹窗）：
    python tools/license_signer.py --selftest data/license_private.pem
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

MODULES = [
    ("archive", "会话存档采集"), ("ocr", "OCR 识别"), ("extract", "结构化抽取"),
    ("risk", "风险研判预警"), ("delivery", "预警投递"), ("records", "结构化数据"),
    ("templates", "抽取模板"), ("models", "模型配置"), ("dashboard", "看板统计"),
    ("admin", "系统管理"),
]


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def sign_license(payload: dict, private_key_pem: str) -> str:
    priv = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    body = b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = priv.sign(body.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return f"{body}.{b64u(sig)}"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("License 签发台 · 企业微信会话存档（厂商工具）")
        self.geometry("660x720")
        self.minsize(600, 680)
        self.priv_pem = ""
        self.priv_name = ""
        self.module_vars: dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self) -> None:
        pad = {"padx": 14, "pady": 6}
        row = 0

        # 1) 私钥
        ttk.Label(self, text="① 选择厂商私钥（license_private.pem，仅本机使用，不出本机）").grid(row=row, column=0, sticky="w", **pad); row += 1
        frm = ttk.Frame(self)
        frm.grid(row=row, column=0, sticky="we", **pad); row += 1
        self.key_lbl = ttk.Label(frm, text="未选择", foreground="#888")
        self.key_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(frm, text="选择私钥…", command=self._pick_key).pack(side="right")

        ttk.Separator(self).grid(row=row, column=0, sticky="we", pady=8); row += 1

        # 2) 授权信息
        ttk.Label(self, text="② 填写授权信息").grid(row=row, column=0, sticky="w", **pad); row += 1
        frm2 = ttk.Frame(self)
        frm2.grid(row=row, column=0, sticky="we", **pad); row += 1
        frm2.columnconfigure(1, weight=1)
        ttk.Label(frm2, text="客户名称 *").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.customer = ttk.Entry(frm2, width=34)
        self.customer.grid(row=0, column=1, sticky="we", pady=4)
        ttk.Label(frm2, text="到期日期 *（YYYY-MM-DD）").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.expire = ttk.Entry(frm2, width=34)
        self.expire.insert(0, f"{date.today().year + 1}-08-18")
        self.expire.grid(row=1, column=1, sticky="we", pady=4)
        ttk.Label(frm2, text="签发日期（默认今天）").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.issued = ttk.Entry(frm2, width=34)
        self.issued.insert(0, date.today().isoformat())
        self.issued.grid(row=2, column=1, sticky="we", pady=4)
        ttk.Label(frm2, text="群数上限（0=不限）").grid(row=3, column=0, sticky="e", padx=6, pady=4)
        self.max_rooms = ttk.Entry(frm2, width=34)
        self.max_rooms.insert(0, "0")
        self.max_rooms.grid(row=3, column=1, sticky="we", pady=4)

        # 模块
        mods_frm = ttk.LabelFrame(self, text="授权模块（默认全部）")
        mods_frm.grid(row=row, column=0, sticky="we", **pad); row += 1
        for i, (key, label) in enumerate(MODULES):
            var = tk.BooleanVar(value=True)
            self.module_vars[key] = var
            ttk.Checkbutton(mods_frm, text=f"{label}（{key}）", variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=12, pady=3)

        # 机器绑定
        self.bind_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="绑定机器（防多机共用；不绑=可迁移部署）", variable=self.bind_var,
                        command=self._toggle_fp).grid(row=row, column=0, sticky="w", **pad); row += 1
        frm3 = ttk.Frame(self)
        frm3.grid(row=row, column=0, sticky="we", **pad); row += 1
        frm3.columnconfigure(1, weight=1)
        ttk.Label(frm3, text="客户机指纹（32 位）").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.fp = ttk.Entry(frm3, width=34)
        self.fp.grid(row=0, column=1, sticky="we", pady=4)

        ttk.Separator(self).grid(row=row, column=0, sticky="we", pady=8); row += 1

        # 3) 生成
        ttk.Label(self, text="③ 生成并保存").grid(row=row, column=0, sticky="w", **pad); row += 1
        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, sticky="w", **pad); row += 1
        ttk.Button(btns, text="生成 License…", command=self._generate).pack(side="left", padx=4)
        self.status = ttk.Label(self, text="", foreground="#888")
        self.status.grid(row=row, column=0, sticky="w", **pad); row += 1

        tip = ("使用说明：\n"
               "1. 客户部署后，在系统「系统管理 → 授权 License」复制本机指纹发给你（不绑机器可跳过）。\n"
               "2. 本程序选择私钥 → 填写客户/到期日 →（可选）绑机器填指纹 → 生成。\n"
               "3. 把生成的 license.key 发给客户，客户在「授权 License」粘贴或上传激活。\n"
               "4. 到期前系统自动提醒；续费用同一私钥重签新到期日即可。")
        ttk.Label(self, text=tip, foreground="#667", justify="left").grid(row=row, column=0, sticky="w", **pad); row += 1

    def _pick_key(self) -> None:
        path = filedialog.askopenfilename(title="选择厂商私钥", filetypes=[("PEM 私钥", "*.pem *.key"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            pem = Path(path).read_text(encoding="utf-8")
            self._check_key(pem)
            self.priv_pem = pem
            self.priv_name = Path(path).name
            self.key_lbl.config(text=f"已加载：{self.priv_name}（PKCS#8 RSA）", foreground="#16a34a")
            self.status.config(text="", foreground="#888")
        except Exception as e:
            messagebox.showerror("私钥错误", f"无法加载私钥：\n{e}")

    def _check_key(self, pem: str) -> None:
        serialization.load_pem_private_key(pem.encode("utf-8"), password=None)

    def _toggle_fp(self) -> None:
        self.fp.config(state="normal" if self.bind_var.get() else "disabled")

    def _generate(self) -> None:
        if not self.priv_pem:
            messagebox.showwarning("缺少私钥", "请先选择厂商私钥（license_private.pem）")
            return
        customer = self.customer.get().strip()
        expire = self.expire.get().strip()
        if not customer or not expire:
            messagebox.showwarning("信息不完整", "请填写客户名称与到期日期")
            return
        modules = [k for k, v in self.module_vars.items() if v.get()]
        if not modules:
            messagebox.showwarning("未选模块", "请至少勾选一个授权模块")
            return
        try:
            max_rooms = int(self.max_rooms.get() or "0")
        except ValueError:
            messagebox.showwarning("群数上限", "群数上限需为数字（0=不限）")
            return
        bind = self.bind_var.get()
        fp = self.fp.get().strip() if bind else ""
        if bind and not fp:
            messagebox.showwarning("缺少指纹", "已勾选绑定机器，请填写客户机指纹")
            return

        payload = {
            "customer": customer,
            "issued_at": self.issued.get().strip() or date.today().isoformat(),
            "expire_at": expire,
            "modules": modules,
            "max_rooms": max_rooms,
            "machine_bound": bind,
            "fp": fp,
        }
        try:
            lic = sign_license(payload, self.priv_pem)
        except Exception as e:
            messagebox.showerror("生成失败", f"签名失败：\n{e}")
            return
        out = filedialog.asksaveasfilename(
            title="保存 License", defaultextension=".key",
            initialfile="license.key", filetypes=[("License 文件", "*.key"), ("所有文件", "*.*")])
        if not out:
            return
        Path(out).write_text(lic, encoding="utf-8")
        self.status.config(
            text=f"✓ 已生成并保存：{Path(out).name}｜{customer}｜到期 {expire}｜模块 {len(modules)} 个"
                 f"{'｜已绑机器' if bind else '｜未绑机器'}",
            foreground="#16a34a")
        messagebox.showinfo("生成成功",
                            f"License 已保存到：\n{out}\n\n"
                            f"客户：{customer}\n到期：{expire}\n模块：{len(modules)} 个\n"
                            f"{'已绑定机器（不可迁移部署）' if bind else '未绑定机器（可迁移部署）'}\n\n"
                            "把该文件发给客户，在系统「系统管理 → 授权 License」上传激活即可。")


def _selftest(priv_path: str) -> int:
    pem = Path(priv_path).read_text(encoding="utf-8")
    payload = {
        "customer": "自测客户",
        "issued_at": date.today().isoformat(),
        "expire_at": f"{date.today().year + 1}-08-18",
        "modules": [m[0] for m in MODULES],
        "max_rooms": 0,
        "machine_bound": False,
        "fp": "",
    }
    lic = sign_license(payload, pem)
    print(lic)
    print(f"\nSELFTEST_OK: len={len(lic)}，可粘贴到系统「授权 License」验证激活。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="PRIV_KEY", help="命令行自测：用指定私钥生成一份测试 License 并打印")
    args = ap.parse_args()
    if args.selftest:
        return _selftest(args.selftest)
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
