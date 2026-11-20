"""计划的归一化与合法性校验。

对照小哲电商的 PlanContract.java，三个函数一一对应：
- normalize：把「末日是否包含」翻译成排他末日，再校验
- validate：指标存在、维度受支持、日期 1~366 天、城市筛选有对应关联
- required：这个计划最少需要哪些业务表
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Set

from app.nlsql.semantic.catalog import CATALOG, Catalog
from app.nlsql.semantic.types import Plan

MAX_RANGE_DAYS = 366


def normalize(plan: Plan, catalog: Catalog = CATALOG) -> Plan:
    """把用户语义的末日换算成排他上界，并做一次完整校验。

    小哲的约定：end 保留用户末日 + end_inclusive=true，程序把 end 加一天，
    最终进入 SQL 的始终是 `time < end`。这样模型不需要理解左闭右开。
    """
    if plan.end_inclusive is None:
        raise ValueError("缺少日期末日包含标记")
    if not plan.start or not plan.end:
        raise ValueError("缺少日期范围")

    end = date.fromisoformat(plan.end)
    if plan.end_inclusive:
        end = end + timedelta(days=1)

    normalized = Plan(
        metric=plan.metric,
        start=plan.start,
        end=end.isoformat(),
        dimension=plan.dimension or "none",
        city=plan.city,
        end_inclusive=False,
    )
    validate(normalized, catalog)
    return normalized


def validate(plan: Plan, catalog: Catalog = CATALOG) -> None:
    metric = catalog.metric(plan.metric)
    if metric is None or plan.dimension not in metric.get("dimensions", []):
        raise ValueError("不支持该指标或分组")

    days = (date.fromisoformat(plan.end) - date.fromisoformat(plan.start)).days
    if days <= 0 or days > MAX_RANGE_DAYS:
        raise ValueError("日期范围须为 1 至 366 天")

    if plan.city is not None and not str(plan.city).strip():
        raise ValueError("城市筛选不能为空")
    if plan.city and "city" not in (metric.get("joins") or {}):
        raise ValueError("该指标不支持按城市筛选")

    snapshot = metric.get("snapshotDate")
    if snapshot and not (plan.start == snapshot and days == 1):
        raise ValueError("该日期没有受支持的快照")


def required(plan: Plan, catalog: Catalog = CATALOG) -> Set[str]:
    """计划必需的业务表；城市分组或城市筛选都要带上 drivers。"""
    metric = catalog.metric(plan.metric)
    tables = set(metric.get("tables", []) if metric else [])
    if plan.dimension == "city" or plan.city:
        tables.add("drivers")
    return tables
