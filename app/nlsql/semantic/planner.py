"""规划器：把一条用户消息变成结构化决策。

对照小哲电商的 QueryPlanner.java：
- 模型只加载 policy + 目录工具 + 输出 Schema；
- 通过 read_catalog 工具拿目录，看不到物理 schema；
- 输出必须是 Decision JSON，缺字段直接判为无效，不做宽容修补。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from loguru import logger
from openai import OpenAI

from app.config import settings
from app.nlsql.semantic.catalog import CATALOG, Catalog, load_resource
from app.nlsql.semantic.types import Decision, Session

_MAX_TOOL_ROUNDS = 3
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_REJECT_HINT = re.compile(r"不支持|不受支持|超出支持范围|无法支持|故reject")

_TOOL = {
    "type": "function",
    "function": {
        "name": "read_catalog",
        "description": "读取指标定义、别名、可用维度、候选模型与关系，再开始规划。",
        "parameters": {"type": "object", "properties": {}},
    },
}


def interpret(session: Session, catalog: Catalog = CATALOG) -> Decision:
    """调用模型生成决策；没有 API Key 时降级为明确拒绝。"""
    if not settings.llm_api_key:
        return Decision(
            status="reject",
            reason="模型未配置：缺少 LLM_API_KEY，语义查询暂不可用",
            tables=[],
            evidence={},
        )

    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": load_resource("policy.txt")},
        {
            "role": "system",
            "content": "输出必须符合以下 JSON Schema，只输出 JSON：\n" + load_resource("decision-schema.json"),
        },
        {
            "role": "user",
            "content": json.dumps({"messages_oldest_first": session.messages}, ensure_ascii=False),
        },
    ]

    for _ in range(_MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            tools=[_TOOL],
            temperature=0,
        )
        message = response.choices[0].message
        if getattr(message, "tool_calls", None):
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"},
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            for call in message.tool_calls:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": catalog.read_catalog()}
                )
            continue
        return parse_decision(message.content or "")

    raise ValueError("模型未在 3 轮内给出决策")


def parse_decision(raw: str) -> Decision:
    """解析并做完整性检查；模型输出缺字段时视为无效。"""
    text = _FENCE_RE.sub("", raw.strip())
    payload = json.loads(text)

    decision = Decision.model_validate(payload)

    # 模型有时把「不支持」写成 query + 空 plan，这里按小哲的规则改判 reject
    if (
        decision.status == "query"
        and decision.plan is None
        and decision.reason
        and _REJECT_HINT.search(decision.reason)
    ):
        decision.status = "reject"

    if not decision.status or not decision.reason:
        raise ValueError("incomplete structured decision")
    if decision.status != "reject" and (decision.plan is None or not decision.plan.dimension):
        raise ValueError("incomplete structured decision")
    return decision


def safe_interpret(session: Session, catalog: Catalog = CATALOG) -> Decision:
    """规划节点的容错包装：异常不抛给图，落成 error 阶段的决策。"""
    try:
        return interpret(session, catalog)
    except Exception as exc:  # noqa: BLE001 —— 模型输出、网络、配额都可能失败
        logger.warning("语义规划失败: {}", exc)
        return Decision(status="reject", reason=f"模型输出格式错误：{exc}", tables=[], evidence={})
