"""
scripts/gen_license.py — 厂商签发 License（需 RSA 私钥，私钥不进仓库）

用法：
  # 1) 普通年费授权（不绑机器，客户可迁移部署）
  python scripts/gen_license.py --customer "XX集团" --expire 2027-08-18

  # 2) 绑定机器授权（防多机共用；指纹由客户机 show_fingerprint.py 产出）
  python scripts/gen_license.py --customer "XX集团" --expire 2027-08-18 \
      --bind-machine --fp 8f3a...客户指纹

  # 3) 只授权部分模块 / 限制群数
  python scripts/gen_license.py --customer "XX集团" --expire 2027-08-18 \
      --modules archive,ocr,extract,risk --max-rooms 50

可选参数：--private-key（默认 data/license_private.pem）、--out（默认 data/license.key）
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.license.manager import MODULE_CATALOG, machine_fingerprint, sign_license


def main() -> None:
    ap = argparse.ArgumentParser(description="签发私有化 License")
    ap.add_argument("--customer", required=True, help="客户名称")
    ap.add_argument("--expire", required=True, help="到期日 YYYY-MM-DD")
    ap.add_argument("--issued", default=date.today().isoformat(), help="签发日（默认今天）")
    ap.add_argument("--modules", default=",".join(MODULE_CATALOG), help="逗号分隔授权模块")
    ap.add_argument("--max-rooms", type=int, default=0, help="最多监控群数，0=不限")
    ap.add_argument("--bind-machine", action="store_true", help="绑定机器指纹")
    ap.add_argument("--fp", default="", help="客户机指纹（--bind-machine 时必填或省略则用本机指纹）")
    ap.add_argument("--private-key", default=settings.LICENSE_PRIVATE_KEY_PATH, help="厂商私钥路径")
    ap.add_argument("--out", default="data/license.key", help="License 输出文件")
    args = ap.parse_args()

    fp = ""
    if args.bind_machine:
        fp = args.fp or machine_fingerprint()

    payload = {
        "customer": args.customer,
        "issued_at": args.issued,
        "expire_at": args.expire,
        "modules": [m.strip() for m in args.modules.split(",") if m.strip()],
        "max_rooms": args.max_rooms,
        "machine_bound": bool(args.bind_machine),
        "fp": fp,
    }
    if not payload["modules"]:
        sys.exit("错误：modules 不能为空")

    priv_path = Path(args.private_key)
    if not priv_path.exists():
        sys.exit(f"错误：私钥不存在 {priv_path}（私钥由厂商保存，勿提交仓库）")

    lic = sign_license(payload, priv_path.read_text(encoding="utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(lic, encoding="utf-8")

    print(f"License 已签发 -> {out}")
    print(f"  客户: {args.customer}")
    print(f"  生效: {args.issued}  到期: {args.expire}")
    print(f"  模块: {', '.join(payload['modules'])}")
    print(f"  群数上限: {'不限' if args.max_rooms == 0 else args.max_rooms}")
    print(f"  机器绑定: {'是 (' + fp + ')' if fp else '否'}")
    print()
    print("将该文件发给客户，客户在「系统管理 → License 授权」上传激活，或放入 data/license.key")


if __name__ == "__main__":
    main()
