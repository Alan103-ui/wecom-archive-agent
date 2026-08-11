"""
app/services/risk/detector.py — 风险检测（关键词 + LLM 双引擎）

流程：
    1. 关键词引擎：按规则的正则逐条匹配，命中即 producing 一条 RiskHit（确定、秒级、零成本）
    2. LLM 引擎：复用本地 Ollama，对消息做语义研判，兜住"绕开关键词的隐晦话术"
        （如"这单咱们私底下处理""别走系统，我直接转你"）

两条引擎的结果在调用方（pipeline）按 (category) 归并，去重后落库。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy.orm import Session

from app.config import settings
from app.models.risk import RiskRule
from app.services.extract import llm as extract_llm
from app.services.risk import categories as cat

logger = logging.getLogger(__name__)

# 正则编译缓存：key=规则 keywords 元组
_KW_CACHE: dict[tuple[str, ...], list[re.Pattern]] = {}


@dataclass
class RiskHit:
    category: str
    severity: str
    detection_method: str  # keyword / llm
    rule_id: str | None = None
    matched_keyword: str | None = None
    snippet: str | None = None
    detail: str | None = None


def _compile(keywords: list[str]) -> list[re.Pattern]:
    key = tuple(keywords)
    cached = _KW_CACHE.get(key)
    if cached is not None:
        return cached
    pats = []
    for kw in keywords:
        try:
            pats.append(re.compile(kw, re.IGNORECASE))
        except re.error:
            # 非法正则退化为普通子串匹配
            pats.append(re.compile(re.escape(kw), re.IGNORECASE))
    _KW_CACHE[key] = pats
    return pats


def _scope_match(rule: RiskRule, room_id: str) -> bool:
    if not rule.scope_rooms:
        return True
    return room_id in rule.scope_rooms


def _excerpt(text: str, start: int, end: int, ctx: int = 40) -> str:
    s = max(0, start - ctx)
    e = min(len(text), end + ctx)
    return text[s:e].replace("\n", " ").strip()


def detect_keyword(text: str, rules: Iterable[RiskRule], room_id: str) -> list[RiskHit]:
    """关键词引擎：逐规则匹配，每条规则取首个命中"""
    if not text:
        return []
    hits: list[RiskHit] = []
    for rule in rules:
        if not rule.enabled or not rule.keywords:
            continue
        if not _scope_match(rule, room_id):
            continue
        for pat in _compile(rule.keywords):
            m = pat.search(text)
            if m:
                hits.append(
                    RiskHit(
                        category=rule.category,
                        severity=rule.severity,
                        detection_method="keyword",
                        rule_id=rule.id,
                        matched_keyword=m.group(0),
                        snippet=_excerpt(text, m.start(), m.end()),
                    )
                )
                break  # 一条规则只报一次
    return hits


_LLM_SYSTEM = (
    "你是企业合规风控助手。判断一条企业微信工作群（采购/客户沟通场景）聊天消息"
    "是否含有业务风险。只输出 JSON，不要解释。\n"
    "可选风险分类（category 必须是其中之一）：\n"
    + "、".join(cat.ALL_CATEGORIES)
    + "\n严重度 severity 取值：low / medium / high / critical。\n"
    "返回格式：{\"risks\": [{\"category\": \"...\", \"severity\": \"...\", "
    "\"snippet\": \"命中原文片段(不超过60字)\", \"reason\": \"简要理由\"}]}\n"
    "若没有风险，返回 {\"risks\": []}。不要编造，宁缺毋漏。"
)


def detect_llm(text: str, room_id: str) -> list[RiskHit]:
    """LLM 语义引擎：兜底识别隐晦话术。失败返回空，不影响关键词结果"""
    if not settings.RISK_LLM_ENABLED or not text or not text.strip():
        return []
    try:
        data = extract_llm.chat_json(text, system=_LLM_SYSTEM, role="risk",
                                      timeout=min(settings.OLLAMA_TIMEOUT, 60))
    except Exception as e:  # noqa: BLE001
        logger.warning("风险 LLM 研判失败（已忽略）：%s", e)
        return []

    risks = (data or {}).get("risks") or []
    if not isinstance(risks, list):
        return []

    hits: list[RiskHit] = []
    for r in risks:
        if not isinstance(r, dict):
            continue
        c = r.get("category")
        if c not in cat.ALL_CATEGORIES:
            continue
        sev = r.get("severity", cat.SEVERITY_MEDIUM)
        if sev not in cat.SEVERITY_ORDER:
            sev = cat.SEVERITY_MEDIUM
        hits.append(
            RiskHit(
                category=c,
                severity=sev,
                detection_method="llm",
                matched_keyword=None,
                snippet=str(r.get("snippet") or "")[:120] or None,
                detail=str(r.get("reason") or "")[:300] or None,
            )
        )
    return hits


def scan(text: str, rules: list[RiskRule], room_id: str) -> list[RiskHit]:
    """双引擎合并扫描。返回去重前的原始命中列表（归并在 pipeline 完成）"""
    kw_hits = detect_keyword(text, rules, room_id)

    # 省成本模式：关键词已命中则不再调 LLM
    if settings.RISK_LLM_ONLY_WHEN_KEYWORD_MISS and kw_hits:
        return kw_hits

    llm_hits = detect_llm(text, room_id)
    return kw_hits + llm_hits


def load_rules(db: Session) -> list[RiskRule]:
    from sqlalchemy import select

    return list(db.execute(select(RiskRule).where(RiskRule.enabled == True)).scalars().all())
