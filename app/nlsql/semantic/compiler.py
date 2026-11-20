"""语义编译器：计划 → 参数化 SQL。

对照小哲电商的 SemanticSqlCompiler.java。三条铁律：
1. 指标表达式只来自目录，模型永远碰不到 SQL 文本；
2. 所有用户值（日期、城市）都走 `?` 绑定，不存在拼接；
3. 只产出 SELECT，且强制 LIMIT 1001，超量由执行器报错。

SQLite 方言：日期格式化用 strftime，不是 MySQL 的 DATE_FORMAT。
"""

from __future__ import annotations

from app.nlsql.semantic import plan_contract
from app.nlsql.semantic.catalog import CATALOG, Catalog
from app.nlsql.semantic.types import Plan, Sql

# 比业务上限多 1 行：拿到 1001 行才能确定「超过 1000 行」，由执行器拒绝
LIMIT_WITH_PROBE = 1001


def compile_plan(plan: Plan, catalog: Catalog = CATALOG) -> Sql:
    """把一个已校验的计划编译成参数化 SQL。"""
    plan = plan_contract.normalize(plan, catalog)
    metric = catalog.metric(plan.metric)
    if metric is None:  # normalize 已保证存在，这里是防御性兜底
        raise ValueError("未知指标")

    source = metric["source"]
    value = metric["expression"]
    time_column = metric["timeColumn"]
    params: list = [plan.start, plan.end]

    uses_city = plan.dimension == "city" or bool(plan.city)
    joins = (metric.get("joins") or {}).get("city", "") if uses_city else ""

    group = {
        "day": f"strftime('%Y-%m-%d', {time_column})",
        "month": f"strftime('%Y-%m', {time_column})",
        "city": "d.city",
    }.get(plan.dimension)

    where = f"{metric['condition']} AND {time_column} >= ? AND {time_column} < ?"
    if plan.city:
        where += " AND d.city = ?"
        params.append(plan.city)

    select = f"{group} AS dimension, " if group else ""
    grouping = " GROUP BY dimension ORDER BY dimension" if group else ""
    from_clause = f"{source} {joins}".strip()

    text = f"SELECT {select}{value} AS value FROM {from_clause} WHERE {where}{grouping} LIMIT {LIMIT_WITH_PROBE}"
    return Sql(text=text, params=params)
