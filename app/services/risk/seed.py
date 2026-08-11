"""
app/services/risk/seed.py — 风险默认数据播种（幂等，按 name/id 去重）

首次启动建好：管理层(L1/L2/L3)、默认投递目标(系统通知)、默认风险规则。
真实部署后，在「风控配置」页按 roomid 隔离规则即可实现"不同群→不同层"。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.risk import AlertLayer, AlertTarget, RiskRule
from app.services.risk import categories as cat

logger = logging.getLogger(__name__)


def seed_risk_defaults(db: Session) -> dict:
    counts = {"layers": 0, "targets": 0, "rules": 0}

    # ---- 管理层 ----
    for spec in cat.DEFAULT_LAYERS:
        existing = db.execute(
            select(AlertLayer).where(AlertLayer.id == spec["id"])
        ).scalar_one_or_none()
        if existing is None:
            db.add(AlertLayer(id=spec["id"], name=spec["name"],
                              level=spec["level"], description=spec["description"]))
            counts["layers"] += 1

    # ---- 投递目标 ----
    for spec in cat.DEFAULT_TARGETS:
        existing = db.execute(
            select(AlertTarget).where(
                AlertTarget.layer_id == spec["layer_id"],
                AlertTarget.channel == spec["channel"],
                AlertTarget.target == spec["target"],
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(AlertTarget(
                layer_id=spec["layer_id"], channel=spec["channel"],
                target=spec["target"], label=spec.get("label"),
                enabled=spec.get("enabled", True),
            ))
            counts["targets"] += 1

    # ---- 风险规则 ----
    for spec in cat.DEFAULT_RULES:
        existing = db.execute(
            select(RiskRule).where(RiskRule.name == spec["name"])
        ).scalar_one_or_none()
        if existing is None:
            db.add(RiskRule(
                name=spec["name"],
                description=spec.get("description"),
                category=spec["category"],
                severity=spec["severity"],
                scope_rooms=spec.get("scope_rooms", []),
                keywords=spec.get("keywords", []),
                alert_layers=spec.get("alert_layers", []),
                enabled=True,
                priority=0,
            ))
            counts["rules"] += 1

    if any(counts.values()):
        db.commit()
        logger.info("已播种风险默认数据：%s", counts)
    return counts
