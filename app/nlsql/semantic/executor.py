"""语义层的只读执行器。

对照小哲电商的 ReadOnlyQueryExecutor.java：
- 只读连接（mode=ro + query_only）；
- 参数化执行，不接受拼接 SQL；
- 超过 1000 行直接报错，让用户缩小范围，而不是悄悄截断。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import List, Sequence

from app.config import settings
from app.db.connection import DB_PATH

MAX_RESULT_ROWS = 1000


class SemanticExecutionError(RuntimeError):
    """执行失败或结果超限。"""


def execute(
    sql_text: str,
    params: Sequence = (),
    *,
    db_path: Path | None = None,
    max_rows: int = MAX_RESULT_ROWS,
    timeout_seconds: float | None = None,
) -> tuple[List[List], List[str]]:
    """在只读连接上执行参数化查询。

    超时用 sqlite 的 progress handler：每 1000 条虚拟机指令检查一次截止时间，
    与小哲的 `statement.setQueryTimeout(3)` 同一作用，只是 SQLite 没有原生实现。
    """
    path = db_path or DB_PATH
    timeout = timeout_seconds or settings.sql_timeout_seconds
    deadline = time.monotonic() + timeout

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    try:
        cursor = conn.execute(sql_text, list(params))
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise SemanticExecutionError(f"结果超过 {max_rows} 行，请缩小范围或增加分组")
        return [list(row) for row in rows], columns
    except sqlite3.Error as exc:
        raise SemanticExecutionError(str(exc)) from exc
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
