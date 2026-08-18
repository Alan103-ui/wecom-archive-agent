"""
app/services/license/manager.py — 机器指纹 + License 签发/验证（RSA 非对称）

设计：
- 厂商用【私钥】签名 License（scripts/gen_license.py），私钥仅厂商持有，不进仓库/不进部署包。
- 客户机用【公钥】验签（本模块），公钥随部署包分发（app/services/license/license_public.pem）。
- 客户机只有公钥，无法伪造 License → 满足私有化年费"防篡改/防盗用"。
- 可选 machine_bound：把 License 绑定到指定机器指纹，换机即失效。
- 开发/演示模式（LICENSE_REQUIRED=False）下，无 License 也放行，不阻断既有演示数据。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import platform
import re
import subprocess
import uuid
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import settings

# 全部可授权模块（生成 License 时从中勾选；校验时用于展示/拦截）
MODULE_CATALOG = [
    "archive",      # 会话存档采集
    "ocr",          # OCR 识别
    "extract",      # 结构化抽取
    "risk",         # 风险研判分级预警
    "delivery",     # 预警投递
    "records",      # 结构化数据查看
    "templates",    # 抽取模板
    "models",       # 模型配置
    "dashboard",    # 看板/统计
    "admin",        # 系统管理（用户/角色/权限）
]
MODULE_LABELS = {
    "archive": "会话存档采集",
    "ocr": "OCR 识别",
    "extract": "结构化抽取",
    "risk": "风险研判预警",
    "delivery": "预警投递",
    "records": "结构化数据",
    "templates": "抽取模板",
    "models": "模型配置",
    "dashboard": "看板统计",
    "admin": "系统管理",
}


# ----------------------------------------------------------------------------
# 机器指纹（跨平台、尽量稳定）
# ----------------------------------------------------------------------------
def _board_uuid() -> str:
    """主板/整机 UUID，最稳定的硬件标识。"""
    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            out = subprocess.check_output(
                "powershell -NoProfile -Command \"(Get-CimInstance Win32_ComputerSystemProduct).UUID\"",
                shell=True, stderr=subprocess.DEVNULL, timeout=10,
            ).decode(errors="ignore")
        elif sys_name == "Linux":
            p = Path("/sys/class/dmi/id/product_uuid")
            out = p.read_text(errors="ignore") if p.exists() else ""
            if not out.strip():
                out = Path("/etc/machine-id").read_text(errors="ignore") if Path("/etc/machine-id").exists() else ""
        else:  # macOS
            out = subprocess.check_output(
                "ioreg -rd1 -c IOPlatformExpertDevice | grep IOPlatformUUID",
                shell=True, stderr=subprocess.DEVNULL, timeout=10,
            ).decode(errors="ignore")
        m = re.search(r"[0-9A-Fa-f]{8}[-_][0-9A-Fa-f]{4}[-_][0-9A-Fa-f]{4}[-_][0-9A-Fa-f]{4}[-_][0-9A-Fa-f]{12}", out)
        if m:
            return m.group(0)
    except Exception:
        pass
    return ""


def _mac_address() -> str:
    """首个物理网卡 MAC（uuid.getnode 在多数机器返回稳定硬件地址）。"""
    try:
        node = uuid.getnode()
        if node and node != 0:
            return ":".join(f"{(node >> i) & 0xff:02x}" for i in (40, 32, 24, 16, 8, 0))
    except Exception:
        pass
    return ""


def machine_fingerprint() -> str:
    raw = "|".join([
        _board_uuid(),
        _mac_address(),
        platform.machine(),
        platform.node(),
    ])
    if not raw.strip("|"):
        raw = f"{platform.node()}:{uuid.getnode()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ----------------------------------------------------------------------------
# 签名 / 验证
# ----------------------------------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_license(payload: dict, private_key_pem: str) -> str:
    """厂商侧：用 RSA 私钥对 payload 签名，返回 `body.signature` 形式的 License 文本。"""
    priv = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = priv.sign(body.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return f"{body}.{_b64u(sig)}"


def _load_public_key():
    path = Path(settings.LICENSE_PUBLIC_KEY_PATH)
    if not path.exists():
        raise FileNotFoundError(f"公钥文件不存在：{path}")
    return serialization.load_pem_public_key(path.read_bytes())


def verify_license(license_text: str) -> dict:
    """客户机侧：用公钥验签并返回结构化结果。

    返回：{status, message, payload}
      status ∈ valid | expired | machine_mismatch | invalid | malformed
    """
    try:
        text = license_text.strip()
        if "." not in text:
            raise ValueError("格式错误（缺少签名段）")
        body, sig_b64 = text.rsplit(".", 1)
        pub = _load_public_key()
        sig = _b64d(sig_b64)
        pub.verify(signature=sig, data=body.encode("utf-8"), padding=padding.PKCS1v15(), algorithm=hashes.SHA256())
        payload = json.loads(_b64d(body))
    except (binascii.Error, ValueError, KeyError, json.JSONDecodeError) as e:
        return {"status": "malformed", "message": f"许可证格式损坏：{e}", "payload": None}
    except Exception as e:  # 验签失败 / 公钥缺失
        return {"status": "invalid", "message": f"许可证签名无效：{e}", "payload": None}

    # 过期检查（宽限期 LICENSE_GRACE_DAYS 内仍视为有效，仅提示）
    expire_at = str(payload.get("expire_at", ""))
    if expire_at:
        try:
            exp = date.fromisoformat(expire_at)
            days_left = (exp - date.today()).days
            payload["_days_left"] = days_left
            if days_left < 0:
                return {"status": "expired", "message": f"许可证已于 {expire_at} 过期", "payload": payload}
            if days_left <= settings.LICENSE_GRACE_DAYS:
                payload["_grace"] = True
        except ValueError:
            pass

    # 机器绑定
    if payload.get("machine_bound"):
        if payload.get("fp") != machine_fingerprint():
            return {"status": "machine_mismatch", "message": "许可证绑定的机器与本机不符", "payload": payload}

    return {"status": "valid", "message": "许可证有效", "payload": payload}


# ----------------------------------------------------------------------------
# 状态汇总（供接口/前端展示）
# ----------------------------------------------------------------------------
def get_license_status() -> dict:
    """读取并校验当前 License 文件，返回前端/接口所需的状态对象。"""
    path = Path(settings.LICENSE_PATH)
    if not path.exists():
        return {
            "status": "not_found",
            "message": "未找到许可证文件",
            "required": settings.LICENSE_REQUIRED,
            "customer": None, "issued_at": None, "expire_at": None,
            "days_left": None, "modules": [], "module_labels": [],
            "machine_bound": False, "max_rooms": 0, "fingerprint": machine_fingerprint(),
        }
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "invalid", "message": f"读取许可证失败：{e}", "required": settings.LICENSE_REQUIRED,
                "fingerprint": machine_fingerprint()}
    res = verify_license(text)
    p = res.get("payload") or {}
    return {
        "status": res["status"],
        "message": res["message"],
        "required": settings.LICENSE_REQUIRED,
        "customer": p.get("customer"),
        "issued_at": p.get("issued_at"),
        "expire_at": p.get("expire_at"),
        "days_left": p.get("_days_left"),
        "grace": p.get("_grace", False),
        "modules": p.get("modules", []),
        "module_labels": [MODULE_LABELS.get(m, m) for m in p.get("modules", [])],
        "machine_bound": bool(p.get("machine_bound")),
        "max_rooms": p.get("max_rooms", 0),
        "fingerprint": machine_fingerprint(),
    }


def is_licensed() -> bool:
    """是否处于「允许运行」状态。开发模式（非强制）下无 License 也返回 True。"""
    if not settings.LICENSE_REQUIRED:
        return True
    st = get_license_status()
    return st["status"] in ("valid", "grace")
