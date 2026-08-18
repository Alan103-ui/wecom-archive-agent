"""
tests/test_license.py — License 签发/验签/过期/机器绑定 回归测试

注意：本测试依赖厂商私钥 data/license_private.pem（本地生成，不提交）。
若私钥缺失（如 CI 环境），相关用例自动跳过，不影响整体套件。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.license.manager import (
    MODULE_CATALOG,
    machine_fingerprint,
    sign_license,
    verify_license,
)

PRIV = Path(settings.LICENSE_PRIVATE_KEY_PATH)
PUB = Path(settings.LICENSE_PUBLIC_KEY_PATH)


def _sign(payload: dict) -> str:
    key = PRIV.read_text(encoding="utf-8")
    return sign_license(payload, key)


def test_public_key_exists():
    assert PUB.exists(), "公钥文件缺失：app/services/license/license_public.pem"


def test_valid_license():
    if not PRIV.exists():
        return
    p = {
        "customer": "测试客户",
        "issued_at": date.today().isoformat(),
        "expire_at": (date.today() + timedelta(days=365)).isoformat(),
        "modules": MODULE_CATALOG,
        "max_rooms": 0,
        "machine_bound": False,
        "fp": "",
    }
    lic = _sign(p)
    r = verify_license(lic)
    assert r["status"] == "valid", r
    assert r["payload"]["customer"] == "测试客户"
    assert r["payload"]["_days_left"] > 300


def test_expired_license():
    if not PRIV.exists():
        return
    p = {
        "customer": "x",
        "issued_at": date.today().isoformat(),
        "expire_at": (date.today() - timedelta(days=1)).isoformat(),
        "modules": ["admin"],
        "machine_bound": False,
        "fp": "",
    }
    assert verify_license(_sign(p))["status"] == "expired"


def test_tampered_license():
    if not PRIV.exists():
        return
    p = {
        "customer": "x",
        "expire_at": (date.today() + timedelta(days=10)).isoformat(),
        "modules": ["admin"],
        "machine_bound": False,
        "fp": "",
    }
    lic = _sign(p)
    bad = lic[:-4] + ("AAAA" if lic[-4:] != "AAAA" else "BBBB")
    assert verify_license(bad)["status"] == "invalid"


def test_machine_bound():
    if not PRIV.exists():
        return
    fp = machine_fingerprint()
    p = {
        "customer": "x",
        "expire_at": (date.today() + timedelta(days=10)).isoformat(),
        "modules": ["admin"],
        "machine_bound": True,
        "fp": fp,
    }
    assert verify_license(_sign(p))["status"] == "valid"
    p2 = dict(p, fp="wrongfp")
    assert verify_license(_sign(p2))["status"] == "machine_mismatch"
