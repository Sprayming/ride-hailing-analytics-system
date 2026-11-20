"""语义层契约测试：计划校验、原话依据、参数化编译。

这些测试不调用模型，跑得快，是 CI 的常驻用例（对应小哲的 CourseTest）。
"""

import pytest

from app.nlsql.semantic import plan_contract, query_rules
from app.nlsql.semantic.catalog import CATALOG
from app.nlsql.semantic.compiler import compile_plan
from app.nlsql.semantic.types import Choice, Decision, Evidence, Plan, Session


def make_session(messages, decision, stage="checking"):
    return Session(id="t", messages=messages, decision=decision, stage=stage)


# ---------------- PlanContract ----------------

def test_end_inclusive_is_translated_to_exclusive_upper_bound():
    plan = Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True)
    assert plan_contract.normalize(plan).end == "2026-12-01"


def test_end_exclusive_is_kept():
    plan = Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=False)
    assert plan_contract.normalize(plan).end == "2026-11-30"


def test_unknown_metric_is_rejected():
    plan = Plan(metric="profit", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True)
    with pytest.raises(ValueError):
        plan_contract.normalize(plan)


def test_unsupported_dimension_is_rejected():
    plan = Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="driver", end_inclusive=True)
    with pytest.raises(ValueError):
        plan_contract.normalize(plan)


def test_range_over_366_days_is_rejected():
    plan = Plan(metric="gmv", start="2025-01-01", end="2026-11-30", dimension="none", end_inclusive=True)
    with pytest.raises(ValueError):
        plan_contract.normalize(plan)


def test_city_dimension_requires_drivers_table():
    plan = Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="city", end_inclusive=True)
    assert "drivers" in plan_contract.required(plan)


# ---------------- 编译 ----------------

def test_compile_binds_all_user_values_as_params():
    plan = Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True)
    sql = compile_plan(plan)
    assert "2026-11-01" not in sql.text
    assert sql.params == ["2026-11-01", "2026-12-01"]
    assert sql.text.startswith("SELECT SUM(o.order_amount) AS value FROM orders o WHERE")
    assert sql.text.endswith("LIMIT 1001")


def test_compile_matches_small_catalog_not_raw_sql():
    """目录里的表达式是唯一来源：改 SQL 只能改目录，模型碰不到。"""
    assert "o.order_amount" in CATALOG.metrics["gmv"]["expression"]
    assert CATALOG.metrics["redemption_rate"]["expression"].startswith("ROUND(SUM(CASE WHEN c.status = 'used'")


def test_compile_city_filter_appends_join_and_param():
    plan = Plan(
        metric="order_count",
        start="2026-11-01",
        end="2026-11-30",
        dimension="none",
        city="上海",
        end_inclusive=True,
    )
    sql = compile_plan(plan)
    assert "JOIN drivers d ON d.id = o.driver_id" in sql.text
    assert "d.city = ?" in sql.text
    assert sql.params == ["2026-11-01", "2026-12-01", "上海"]
    assert "上海" not in sql.text


# ---------------- QueryRules ----------------

def test_invalid_status_raises():
    session = make_session(["查询"], Decision(status="guess", reason="", tables=[]))
    with pytest.raises(ValueError):
        query_rules.check(session)


def test_plan_error_becomes_reject():
    decision = Decision(
        status="query",
        reason="按月看",
        plan=Plan(metric="profit", start="2026-11-01", end="2026-11-30", dimension="month", end_inclusive=True),
        tables=["orders"],
    )
    session = make_session(["查利润"], decision)
    query_rules.check(session)
    assert session.stage == "reject"
    assert "不支持" in session.reason


def test_unknown_table_becomes_reject():
    decision = Decision(
        status="query",
        reason="看 GMV",
        plan=Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True),
        tables=["orders", "payments"],
    )
    session = make_session(["11月GMV"], decision)
    query_rules.check(session)
    assert session.stage == "reject"


def test_redundant_table_only_warns():
    decision = Decision(
        status="query",
        reason="看 GMV",
        plan=Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True),
        tables=["orders", "drivers"],
        evidence={"metric": [Evidence(message=0, quote="GMV")], "date": [Evidence(message=0, quote="11月")]},
    )
    session = make_session(["11月GMV"], decision)
    query_rules.check(session)
    # 与小哲一致：check 只判到 review，confirm 是工作流节点推进的
    assert session.stage == "review"
    assert session.warnings and "冗余表" in session.warnings[0]


def test_evidence_quote_must_exist_in_message():
    decision = Decision(
        status="query",
        reason="看核销率",
        plan=Plan(metric="redemption_rate", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True),
        tables=["coupons"],
        evidence={"metric": [Evidence(message=0, quote="退款率")], "date": [Evidence(message=0, quote="11月")]},
    )
    session = make_session(["11月核销率"], decision)
    query_rules.check(session)
    assert session.stage == "clarify"
    assert "metric" in session.decision.missing_fields


def test_missing_date_clarifies_and_keeps_partial_plan():
    decision = Decision(
        status="query",
        reason="看核销率",
        plan=Plan(metric="redemption_rate", start=None, end=None, dimension="month", end_inclusive=True),
        tables=["coupons"],
        evidence={"metric": [Evidence(message=0, quote="核销率")]},
    )
    session = make_session(["按月的核销率"], decision)
    query_rules.check(session)
    # 计划本身缺日期会先被判为 reject（和小哲一致），提示补日期
    assert session.stage in {"reject", "clarify"}


def test_choose_merges_only_selected_fields():
    decision = Decision(
        status="clarify",
        reason="缺日期",
        missing_fields=["date"],
        plan=Plan(metric="gmv", start=None, end=None, dimension="month", end_inclusive=None),
        tables=["orders"],
    )
    session = make_session(["按月看GMV"], decision, stage="clarify")
    query_rules.choose(session, Choice(start="2026-11-01", end="2026-11-30"))
    assert session.stage == "chosen"
    assert session.decision.plan.metric == "gmv"
    assert session.decision.plan.dimension == "month"
    assert session.decision.plan.end == "2026-11-30"
    assert session.decision.missing_fields == []


def test_choose_rejects_unknown_metric():
    decision = Decision(status="clarify", reason="缺指标", plan=Plan(dimension="none"), tables=[])
    session = make_session(["看看"], decision, stage="clarify")
    with pytest.raises(ValueError):
        query_rules.choose(session, Choice(metric="profit"))


# ---------------- 维度扩展 ----------------

def test_coupon_type_dimension_joins_coupon_types():
    plan = Plan(
        metric="redemption_rate",
        start="2026-11-01",
        end="2026-11-30",
        dimension="coupon_type",
        end_inclusive=True,
    )
    sql = compile_plan(plan)
    assert "JOIN coupon_types ct ON ct.id = c.coupon_type_id" in sql.text
    assert "ct.name AS dimension" in sql.text
    assert "coupon_types" in plan_contract.required(plan)


def test_driver_level_dimension_joins_drivers():
    plan = Plan(
        metric="avg_order_amount",
        start="2026-11-01",
        end="2026-11-30",
        dimension="driver_level",
        end_inclusive=True,
    )
    sql = compile_plan(plan)
    assert "JOIN drivers d ON d.id = o.driver_id" in sql.text
    assert "d.driver_level AS dimension" in sql.text


def test_dimension_not_supported_by_metric_is_rejected():
    plan = Plan(
        metric="coupon_issued",
        start="2026-11-01",
        end="2026-11-30",
        dimension="driver_level",
        end_inclusive=True,
    )
    with pytest.raises(ValueError):
        compile_plan(plan)


def test_coupon_used_keeps_zero_rows():
    """核销数用条件聚合而不是 WHERE 过滤，否则 0 核销的券类型会整行消失。"""
    metric = CATALOG.metrics["coupon_used"]
    assert metric["condition"] == "1 = 1"
    assert "CASE WHEN c.status = 'used'" in metric["expression"]
