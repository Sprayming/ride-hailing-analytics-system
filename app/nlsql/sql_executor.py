"""查询执行层：先过 AST 白名单闸门，再走只读连接。

两层职责分得很清楚：
- `sql_guard` 只做判定，不碰数据库（纯函数，好测）
- `connection.execute_readonly_sql` 只负责执行，就算闸门被绕过也写不了库

`run_sql` 是两者的组合，也是单 Agent 与多 Agent 两条链路的共用入口。
"""

from __future__ import annotations

from loguru import logger

from app.config import settings
from app.db.connection import execute_readonly_sql
from app.nlsql.sql_guard import GuardResult, guard_sql


def check_sql(sql: str) -> GuardResult:
    """过闸门但不在数据库上执行，返回完整判定结果（原因码 + 规范化 SQL）。"""
    return guard_sql(sql, max_rows=settings.sql_max_rows)


def validate_sql(sql: str) -> bool:
    """兼容旧接口：只关心能不能执行。"""
    return check_sql(sql).ok


def run_sql(sql: str) -> tuple[list[dict], list[str]]:
    """校验并执行一条 LLM 生成的查询。

    被拒时不抛异常，返回 ([], [])，把原因写进日志——
    和旧实现保持同一契约，调用方（单 Agent / 多 Agent / 评测脚本）无需改动。
    """
    result = check_sql(sql)
    if not result.ok:
        logger.warning("SQL 被安全闸门拒绝 [{}]: {}", result.reason, result.detail)
        return [], []

    try:
        rows, columns = execute_readonly_sql(
            result.sql,
            max_rows=settings.sql_max_rows,
            timeout_seconds=settings.sql_timeout_seconds,
        )
        logger.info("SQL executed: {} rows returned", len(rows))
        return rows, columns
    except Exception as e:  # noqa: BLE001 —— 执行失败一律降级为空结果，由上层给兜底话术
        logger.error("SQL execution failed: {}", e)
        return [], []
