"""决策校验：模型说什么不算数，这里说了才算。

对照小哲电商的 QueryRules.java：
1. status 只能是 query / clarify / reject；
2. query 的计划要过 PlanContract；
3. 模型选的表必须在目录里，且覆盖必需表（多选的只警告）；
4. 每个结论字段都要有「用户原话依据」，找不到就澄清，不许猜；
5. 澄清不是重问——用户点选后走 choose，保留原来的分组与城市。
"""

from __future__ import annotations

import re
from typing import List

from app.nlsql.semantic import plan_contract
from app.nlsql.semantic.catalog import CATALOG, Catalog
from app.nlsql.semantic.types import Choice, Plan, Session

_STATUSES = {"query", "clarify", "reject"}
_DATE_HINT = re.compile(r"\d{4}[-/年]\d{1,2}|\d{1,2}月")
_RELATIVE_HINTS = ("今天", "昨天", "前天", "本周", "上周", "本月", "上月", "最近", "近")


def check(session: Session, catalog: Catalog = CATALOG) -> None:
    """校验模型决策并推进会话阶段（review / clarify / reject）。"""
    decision = session.decision
    if decision is None:
        raise ValueError("模型没有给出决策")
    if decision.status not in _STATUSES:
        raise ValueError("invalid model status")

    session.stage = decision.status
    session.reason = decision.reason or ""
    if decision.status != "query":
        return

    plan: Plan | None = decision.plan
    try:
        plan = plan_contract.normalize(plan, catalog)
    except (ValueError, TypeError) as exc:
        session.stage = "reject"
        session.reason = str(exc)
        return

    known = set(catalog.models)
    selected = set(decision.tables or [])
    required = plan_contract.required(plan, catalog)
    if not known.issuperset(selected) or not selected.issuperset(required):
        session.stage = "reject"
        session.reason = "所选表未知或缺少必要业务表"
        return

    redundant = selected - required
    session.warnings = [f"目录选择含冗余表：{sorted(redundant)}"] if redundant else []

    decision.plan = plan
    decision.missing_fields = []
    decision.evidence = decision.evidence or {}

    missing: List[str] = []
    if session.choice.metric is None and (not has_evidence(session, "metric") or not _meaning_metric(session, catalog)):
        missing.append("metric")
    if session.choice.start is None and (not has_evidence(session, "date") or not _meaning_date(session)):
        missing.append("date")
    if plan.dimension != "none" and not has_evidence(session, "dimension"):
        missing.append("dimension")
    if plan.city and not has_evidence(session, "city"):
        missing.append("city")

    if missing:
        clarify(session, "请补充明确依据：" + "、".join(missing), missing)
    else:
        session.stage = "review"


def has_evidence(session: Session, field: str) -> bool:
    """依据必须能被原文验证：下标合法、引文非空、且真的出现在那条消息里。"""
    quotes = (session.decision.evidence or {}).get(field) if session.decision else None
    if not quotes:
        return False
    return all(
        item.message is not None
        and 0 <= item.message < len(session.messages)
        and item.quote is not None
        and item.quote.strip() != ""
        and item.quote in session.messages[item.message]
        for item in quotes
    )


def _last_message(session: Session) -> str:
    return session.messages[-1] if session.messages else ""


def _meaning_metric(session: Session, catalog: Catalog) -> bool:
    """用户说出指标名或别名，本身就算口径依据（对应小哲的 MeaningEvidence.metric）。"""
    plan = session.decision.plan if session.decision else None
    metric = catalog.metric(plan.metric if plan else None)
    if not metric:
        return False
    text = _last_message(session).lower()
    names = list(metric.get("aliases", [])) + [metric.get("name", "")]
    return any(name and str(name).lower() in text for name in names)


def _meaning_date(session: Session) -> bool:
    """显式日期或相对时间词，本身就算日期依据。"""
    text = _last_message(session)
    return bool(_DATE_HINT.search(text)) or any(word in text for word in _RELATIVE_HINTS)


def clarify(session: Session, reason: str, missing: List[str]) -> None:
    old = session.decision
    session.stage = "clarify"
    session.reason = reason
    if old is not None:
        old.status = "clarify"
        old.reason = reason
        old.missing_fields = missing


def choose(session: Session, choice: Choice, catalog: Catalog = CATALOG) -> None:
    """用户点选补齐：只替换点选字段，其余条件原样保留。"""
    if choice is None or session.stage != "clarify":
        raise ValueError("clarification required")
    if choice.metric is not None and choice.metric not in catalog.metrics:
        raise ValueError("unknown metric choice")
    if (choice.start is None) != (choice.end is None):
        raise ValueError("both dates required")

    old = session.decision.plan if session.decision else None
    if old is None:
        raise ValueError("clarification requires partial plan")

    metric = choice.metric or old.metric
    start = choice.start or old.start
    end = choice.end or old.end
    plan = Plan(
        metric=metric,
        start=start,
        end=end,
        dimension=old.dimension,
        city=old.city,
        end_inclusive=False if choice.start else old.end_inclusive,
    )

    session.choice = Choice(
        metric=choice.metric or session.choice.metric,
        start=choice.start or session.choice.start,
        end=choice.end or session.choice.end,
    )

    tables = list(plan_contract.required(plan, catalog)) if metric else list(session.decision.tables)
    missing = list(session.decision.missing_fields)
    if choice.metric:
        missing = [item for item in missing if item != "metric"]
    if choice.start:
        missing = [item for item in missing if item != "date"]
    if metric is None and "metric" not in missing:
        missing.append("metric")
    if (start is None or end is None) and "date" not in missing:
        missing.append("date")

    session.decision.status = "query" if not missing else "clarify"
    session.decision.reason = "已采用用户选项"
    session.decision.missing_fields = missing
    session.decision.plan = plan
    session.decision.tables = tables
    session.stage = "chosen"
