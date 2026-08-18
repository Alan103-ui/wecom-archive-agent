"""
app/api/license.py — 许可证状态查询与激活

安全：所有接口要求登录（require_auth）。激活接口在前端仅对系统管理员可见入口，
写入前先 verify_license 验签，伪造/篡改的 License 不会被写入。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from app.config import settings
from app.services.auth.rbac import require_auth
from app.services.license.manager import get_license_status, verify_license

router = APIRouter(prefix="/license", tags=["授权管理"])


@router.get("/status", summary="获取许可证状态")
def license_status(_: object = Depends(require_auth)):
    return get_license_status()


@router.post("/activate", summary="激活 / 更新许可证")
async def activate_license(
    _: object = Depends(require_auth),
    payload: dict | None = Body(default=None),
    file: UploadFile | None = File(default=None),
):
    text = ""
    if payload and payload.get("license_text"):
        text = str(payload["license_text"]).strip()
    if not text and file is not None:
        text = (await file.read()).decode("utf-8", errors="ignore").strip()
    if not text:
        raise HTTPException(400, "请提供 license_text 或上传许可证文件")

    res = verify_license(text)
    if res["status"] in ("invalid", "malformed"):
        raise HTTPException(400, res["message"])

    out = Path(settings.LICENSE_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"ok": True, **get_license_status()}
