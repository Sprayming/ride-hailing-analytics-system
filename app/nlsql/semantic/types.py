"""语义层的数据类型。

对照小哲电商 text2sql 的 Types：
    Plan / Evidence / Decision / Choice / Reply / Sql / Session

区别只有领域字段：category（品类）换成 city（城市）。
所有字段都可序列化——会话要整体进 checkpoint，不能依赖 Python 对象身份。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Plan(BaseModel):
    """受控计划：模型只能决定「查什么指标、按什么分组、哪段时间、哪个城市」。"""

    metric: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    dimension: str = "none"
    city: Optional[str] = None
    end_inclusive: Optional[bool] = None


class Evidence(BaseModel):
    """模型给出的原话依据：第几条用户消息里的哪一段连续原文。"""

    message: int
    quote: str


class Decision(BaseModel):
    """模型的一次结构化决策。"""

    status: str = "reject"
    reason: str = ""
    missing_fields: List[str] = Field(default_factory=list)
    plan: Optional[Plan] = None
    tables: List[str] = Field(default_factory=list)
    evidence: Dict[str, List[Evidence]] = Field(default_factory=dict)


class Choice(BaseModel):
    """用户在澄清卡上的点选结果。"""

    metric: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


class Sql(BaseModel):
    """编译产物：文本 + 参数，用户值永远不拼进字符串。"""

    text: str
    params: List[Any] = Field(default_factory=list)


class Session(BaseModel):
    """一次查询会话的完整状态，整体进出 checkpoint。"""

    id: str
    stage: str = "pending"
    reason: str = ""
    revision: int = 0
    executions: int = 0
    messages: List[str] = Field(default_factory=list)
    decision: Optional[Decision] = None
    choice: Choice = Field(default_factory=Choice)
    warnings: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)
    sql: Optional[str] = None
    params: List[Any] = Field(default_factory=list)
    elapsed_ms: float = 0.0
