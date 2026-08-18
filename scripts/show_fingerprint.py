"""
scripts/show_fingerprint.py — 客户机生成机器指纹（用于绑定 License）

部署到客户机后，客户运行本脚本，把输出的指纹发给厂商，
厂商用 `gen_license.py --bind-machine --fp <指纹>` 签发绑定本机的 License。

用法：
  python scripts/show_fingerprint.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.license.manager import machine_fingerprint


if __name__ == "__main__":
    fp = machine_fingerprint()
    print("请将以下机器指纹提供给厂商，用于签发「绑定本机」的 License：")
    print()
    print("  " + fp)
    print()
    print("（该指纹仅由本机硬件信息生成，不含任何业务数据，可安全外发。）")
