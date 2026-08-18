"""
app/api/permissions.py — 权限目录（角色分配权限时的勾选清单）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services.auth.catalog import PERMISSION_CATALOG
from app.services.auth.rbac import require_perm

router = APIRouter(prefix="/permissions", tags=["权限管理"])


@router.get("", summary="权限目录（按模块分组）")
def list_permissions(_=Depends(require_perm("permissions", "view"))):
    return [
        {
            "module": m,
            "name": meta["name"],
            "actions": [
                {"action": a, "name": n, "code": f"{m}:{a}"}
                for a, n in meta["actions"].items()
            ],
        }
        for m, meta in PERMISSION_CATALOG.items()
    ]
