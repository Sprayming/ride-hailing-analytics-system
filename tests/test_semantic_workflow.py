"""工作流测试：不调用模型，用假规划器跑通「确认 → 执行」与「澄清 → 点选」。

对应小哲的 WorkflowTest：验证图上的每条边，以及执行节点只接受 approved。
"""

import sqlite3

import pytest
from langgraph.types import Command

from app.nlsql.semantic import executor as executor_mod
from app.nlsql.semantic import workflow
from app.nlsql.semantic.types import Choice, Decision, Evidence, Plan, Session


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "demo.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE drivers (id INTEGER PRIMARY KEY, city TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY, driver_id INTEGER, order_amount REAL,
            order_time TEXT, status TEXT
        );
        CREATE TABLE coupons (id INTEGER PRIMARY KEY, driver_id INTEGER, status TEXT, issued_at TEXT);
        INSERT INTO drivers VALUES (1, '上海'), (2, '北京');
        INSERT INTO orders VALUES
            (1, 1, 100.0, '2026-11-05 10:00:00', 'completed'),
            (2, 1, 200.0, '2026-11-06 11:00:00', 'completed'),
            (3, 2, 500.0, '2026-11-07 12:00:00', 'cancelled');
        INSERT INTO coupons VALUES (1, 1, 'used', '2026-11-01 09:00:00'),
                                   (2, 1, 'unused', '2026-11-02 09:00:00');
        """
    )
    conn.commit()
    conn.close()
    return path


def _patch_planner(monkeypatch, decisions):
    """按顺序返回预设决策，模拟模型的多次调用。"""
    items = list(decisions)
    monkeypatch.setattr(workflow, "safe_interpret", lambda session: items.pop(0) if items else Decision(status="reject", reason="无更多预设"))


def _patch_executor(monkeypatch, db_path):
    real = executor_mod.execute
    monkeypatch.setattr(
        workflow.executor, "execute", lambda text, params: real(text, params, db_path=db_path)
    )


def _query_decision(message_index=0, date_quote="11月"):
    return Decision(
        status="query",
        reason="按 11 月统计 GMV",
        plan=Plan(metric="gmv", start="2026-11-01", end="2026-11-30", dimension="none", end_inclusive=True),
        tables=["orders"],
        evidence={
            "metric": [Evidence(message=0, quote="GMV")],
            "date": [Evidence(message=message_index, quote=date_quote)],
        },
    )


def _run(graph, session):
    cfg = {"configurable": {"thread_id": session.id}}
    graph.invoke({"session": session.model_dump()}, cfg)
    return cfg


def _state(graph, cfg):
    return Session.model_validate(graph.get_state(cfg).values["session"])


def test_confirm_then_execute(monkeypatch, db):
    _patch_planner(monkeypatch, [_query_decision()])
    _patch_executor(monkeypatch, db)
    graph = workflow.build_graph()
    cfg = _run(graph, Session(id="s1", messages=["查11月GMV"]))

    pending = _state(graph, cfg)
    assert pending.stage == "confirm"
    assert pending.decision.plan.start == "2026-11-01"

    graph.invoke(Command(resume={"action": "confirm"}), cfg)
    done = _state(graph, cfg)
    assert done.stage == "done"
    assert done.executions == 1
    assert done.rows[0][0] == 300.0  # 已完成订单 100 + 200，取消单不计
    assert done.sql.startswith("SELECT SUM(o.order_amount)")
    assert "2026-11-01" not in done.sql


def test_clarify_choice_then_confirm(monkeypatch, db):
    clarify = Decision(
        status="clarify",
        reason="缺日期",
        missing_fields=["date"],
        plan=Plan(metric="gmv", start=None, end=None, dimension="none", end_inclusive=None),
        tables=["orders"],
        evidence={"metric": [Evidence(message=0, quote="GMV")]},
    )
    _patch_planner(monkeypatch, [clarify])
    _patch_executor(monkeypatch, db)
    graph = workflow.build_graph()
    cfg = _run(graph, Session(id="s2", messages=["查 GMV"]))
    assert _state(graph, cfg).stage == "clarify"

    graph.invoke(
        Command(resume={"action": "choice", "choice": Choice(start="2026-11-01", end="2026-11-30").model_dump()}),
        cfg,
    )
    after_choice = _state(graph, cfg)
    assert after_choice.stage == "confirm"
    assert after_choice.decision.plan.start == "2026-11-01"

    graph.invoke(Command(resume={"action": "confirm"}), cfg)
    assert _state(graph, cfg).stage == "done"


def test_regenerate_calls_planner_again(monkeypatch, db):
    first = Decision(
        status="clarify",
        reason="缺日期",
        missing_fields=["date"],
        plan=Plan(metric="gmv", start=None, end=None, dimension="none", end_inclusive=None),
        tables=["orders"],
        evidence={"metric": [Evidence(message=0, quote="GMV")]},
    )
    _patch_planner(monkeypatch, [first, _query_decision(message_index=1)])
    _patch_executor(monkeypatch, db)
    graph = workflow.build_graph()
    cfg = _run(graph, Session(id="s3", messages=["查 GMV"]))
    assert _state(graph, cfg).stage == "clarify"

    graph.invoke(Command(resume={"action": "message", "text": "11月的GMV"}), cfg)
    state = _state(graph, cfg)
    assert state.stage == "confirm"
    assert state.revision == 2
    assert state.messages[-1] == "11月的GMV"


def test_reject_stops_without_execution(monkeypatch, db):
    reject = Decision(status="reject", reason="不支持预测类问题", tables=[], evidence={})
    _patch_planner(monkeypatch, [reject])
    _patch_executor(monkeypatch, db)
    graph = workflow.build_graph()
    cfg = _run(graph, Session(id="s4", messages=["预测下个月 GMV"]))

    state = _state(graph, cfg)
    assert state.stage == "reject"
    assert state.executions == 0
    assert graph.get_state(cfg).next == ()


def test_execute_node_refuses_unapproved_stage():
    with pytest.raises(RuntimeError):
        workflow._sql_execute_node({"session": Session(id="x", stage="confirm").model_dump()})
