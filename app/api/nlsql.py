"""语义查询接口：受控计划 + 人工确认。

和 /api/query 的区别：
- /api/query 是「一句话 → 模型写 SQL → 直接执行」，适合探索；
- /api/nlsql 是「一句话 → 受控计划 → 用户确认 → 参数化执行」，适合对口径有要求的场景。

两条链路互不影响，原接口保持不动。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.nlsql.semantic import workflow
from app.nlsql.semantic.types import Session

router = APIRouter(prefix="/api/nlsql", tags=["Semantic NL2SQL"])


class StartRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2048)


class MessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2048)


class ChoiceRequest(BaseModel):
    metric: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


def _load(session_id: str) -> Session:
    try:
        return workflow.get_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")


def _reply(session_id: str, action: str, **payload) -> Session:
    _load(session_id)
    try:
        return workflow.reply(session_id, action, **payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    except Exception as exc:  # noqa: BLE001 —— 没有挂起中的确认卡时不能提交回执
        logger.warning("回执被拒绝 [{}]: {}", action, exc)
        raise HTTPException(status_code=409, detail=f"当前状态不接受该操作：{exc}")


@router.post("/session", response_model=Session)
def create_session(request: StartRequest):
    """新建会话，推进到确认卡 / 澄清卡 / 拒绝。"""
    return workflow.start_session(request.question)


@router.get("/session/{session_id}", response_model=Session)
def read_session(session_id: str):
    return _load(session_id)


@router.post("/session/{session_id}/message", response_model=Session)
def send_message(session_id: str, request: MessageRequest):
    """在澄清/确认卡上补充一句话，重新规划。"""
    return _reply(session_id, "message", text=request.text)


@router.post("/session/{session_id}/choice", response_model=Session)
def choose(session_id: str, request: ChoiceRequest):
    """点选补齐缺失字段，保留原有分组与城市。"""
    return _reply(session_id, "choice", choice=request.model_dump())


@router.post("/session/{session_id}/confirm", response_model=Session)
def confirm(session_id: str):
    """确认计划并执行（只有这一步会真正查库）。"""
    return _reply(session_id, "confirm")


@router.post("/session/{session_id}/regenerate", response_model=Session)
def regenerate(session_id: str):
    """计划不对，让模型重来。"""
    return _reply(session_id, "regenerate")


@router.post("/session/{session_id}/cancel", response_model=Session)
def cancel(session_id: str):
    return _reply(session_id, "cancel")
