"""查询工作流：模型规划 → 程序校验 → 人工确认 → 编译执行。

对照小哲电商的 Workflow.java：

    planner ──(checking)──> plan_validation ──(confirm/clarify)──> human_feedback
      ↑                                                                  │
      └────────(pending: 用户改口)──────────────────────────────────────┤
                                                                         ├─(approved)→ sql_execute → END
                                                                         └─(chosen)  → plan_validation

和 ride 原来的单 Agent 链路最大的区别：图里没有「模型直接执行 SQL」这条边，
执行节点只接受 approved 状态，且只执行编译器产出的参数化语句。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from loguru import logger

from app.nlsql.semantic import compiler, executor, query_rules
from app.nlsql.semantic.planner import safe_interpret
from app.nlsql.semantic.types import Choice, Session

SESSION_DB = Path(__file__).resolve().parents[3] / "data" / "nlsql_sessions.db"


class GraphState(TypedDict):
    """图状态只有一个字段，整段会话替换式更新（对应小哲的 ReplaceStrategy）。"""

    session: Dict[str, Any]


def _planner_node(state: GraphState) -> Dict[str, Any]:
    session = Session.model_validate(state["session"])
    session.decision = safe_interpret(session)
    session.revision += 1
    session.stage = "checking"
    return {"session": session.model_dump()}


def _plan_validation_node(state: GraphState) -> Dict[str, Any]:
    session = Session.model_validate(state["session"])
    try:
        query_rules.check(session)
    except Exception as exc:  # noqa: BLE001 —— 模型决策结构非法时不让整张图崩
        logger.warning("计划校验异常: {}", exc)
        session.stage = "error"
        session.reason = str(exc)
    if session.stage == "review":
        session.stage = "confirm"
    return {"session": session.model_dump()}


def _human_feedback_node(state: GraphState) -> Dict[str, Any]:
    """确认/澄清卡：只有 API 明确提交后，图才继续。

    对应小哲的 HumanFeedbackNode + InterruptableAction：
    stage 为 confirm / clarify 时挂起，等待前端提交 confirm / choice / message。
    """
    session = Session.model_validate(state["session"])
    if session.stage in ("clarify", "confirm"):
        decision = session.decision
        reply = interrupt(
            {
                "stage": session.stage,
                "reason": session.reason,
                "missing_fields": list(decision.missing_fields) if decision else [],
                "plan": decision.plan.model_dump() if decision and decision.plan else None,
                "session": session.model_dump(),
            }
        )
        _apply_reply(session, reply or {})
    return {"session": session.model_dump()}


def _apply_reply(session: Session, reply: Dict[str, Any]) -> None:
    action = reply.get("action")
    if action == "confirm":
        session.stage = "approved"
    elif action == "choice":
        query_rules.choose(session, Choice.model_validate(reply.get("choice") or {}))
    elif action == "message":
        text = str(reply.get("text") or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        session.messages.append(text)
        session.stage = "pending"
    elif action == "regenerate":
        session.stage = "pending"
    elif action == "cancel":
        session.stage = "cancelled"
        session.reason = reply.get("reason") or "用户取消"
    else:
        raise ValueError(f"未知的回执动作：{action}")


def _sql_execute_node(state: GraphState) -> Dict[str, Any]:
    session = Session.model_validate(state["session"])
    if session.stage != "approved":
        raise RuntimeError("confirmation required")

    started = time.perf_counter()
    try:
        plan = session.decision.plan if session.decision else None
        if plan is None:
            raise ValueError("缺少计划")
        sql = compiler.compile_plan(plan)
        rows, columns = executor.execute(sql.text, sql.params)
        session.sql = sql.text
        session.params = sql.params
        session.rows = rows
        session.columns = columns
        session.executions += 1
        session.stage = "done"
        session.reason = f"已执行，返回 {len(rows)} 行"
    except Exception as exc:  # noqa: BLE001 —— 执行失败落成 error 阶段，前端可重试
        session.stage = "error"
        session.reason = f"数据库执行失败：{exc}"
    session.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {"session": session.model_dump()}


def _after_planner(state: GraphState) -> str:
    session = Session.model_validate(state["session"])
    return "plan_validation" if session.stage == "checking" else END


def _after_validation(state: GraphState) -> str:
    session = Session.model_validate(state["session"])
    return "human_feedback" if session.stage in ("confirm", "clarify") else END


def _after_feedback(state: GraphState) -> str:
    session = Session.model_validate(state["session"])
    return {
        "approved": "sql_execute",
        "pending": "planner",
        "chosen": "plan_validation",
    }.get(session.stage, END)


def build_graph(checkpointer: Optional[Any] = None):
    """构建并编译状态图；测试可注入 MemorySaver，线上用 SqliteSaver。"""
    builder = StateGraph(GraphState)
    builder.add_node("planner", _planner_node)
    builder.add_node("plan_validation", _plan_validation_node)
    builder.add_node("human_feedback", _human_feedback_node)
    builder.add_node("sql_execute", _sql_execute_node)

    builder.set_entry_point("planner")
    builder.add_conditional_edges(
        "planner", _after_planner, {"plan_validation": "plan_validation", END: END}
    )
    builder.add_conditional_edges(
        "plan_validation", _after_validation, {"human_feedback": "human_feedback", END: END}
    )
    builder.add_conditional_edges(
        "human_feedback",
        _after_feedback,
        {"sql_execute": "sql_execute", "planner": "planner", "plan_validation": "plan_validation", END: END},
    )
    builder.add_edge("sql_execute", END)
    return builder.compile(checkpointer=checkpointer or MemorySaver())


_GRAPH = None
_GRAPH_LOCK = threading.Lock()


def get_graph():
    """进程内单例；checkpoint 落 SQLite，重启后会话仍在。"""
    global _GRAPH
    with _GRAPH_LOCK:
        if _GRAPH is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            SESSION_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(SESSION_DB), check_same_thread=False)
            _GRAPH = build_graph(SqliteSaver(conn))
        return _GRAPH


def _config(session_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": session_id}}


def start_session(question: str) -> Session:
    """新建会话并推进到第一个挂起点（确认卡 / 澄清卡 / 结束）。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    session = Session(id=uuid.uuid4().hex, messages=[question])
    graph = get_graph()
    graph.invoke({"session": session.model_dump()}, _config(session.id))
    return get_session(session.id)


def get_session(session_id: str) -> Session:
    values = get_graph().get_state(_config(session_id)).values
    if not values or "session" not in values:
        raise KeyError(session_id)
    return Session.model_validate(values["session"])


def reply(session_id: str, action: str, **payload: Any) -> Session:
    """提交一次用户回执：confirm / choice / message / regenerate / cancel。"""
    graph = get_graph()
    try:
        graph.invoke(Command(resume={"action": action, **payload}), _config(session_id))
    except KeyError:
        raise
    return get_session(session_id)
