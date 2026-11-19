from __future__ import annotations
import sqlite3
import time
from pathlib import Path
from loguru import logger

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ride_hailing.db"
SQL_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "schema_sqlite.sql"

# 查询硬上限：超过这个时间无论 SQL 写成什么样都中断
DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_readonly_connection(timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS):
    """只读连接：库文件以 mode=ro 打开，再开 query_only。

    这是第二道兜底——即使 LLM 生成的 SQL 漏过了 AST 闸门，
    这一层在数据库层面也写不进去（ATTACH 的新库同样没有写权限）。
    """
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def execute_readonly_sql(
    sql: str,
    *,
    max_rows: int = 200,
    timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
) -> tuple[list[dict], list[str]]:
    """在只读连接上执行查询，带超时中断与结果行数上限。

    超时用 sqlite 的 progress handler 实现：每 1000 条虚拟机指令回调一次，
    返回非 0 即中断本次查询（长笛卡尔积、无索引全扫都会被打断）。
    """
    conn = get_readonly_connection(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    try:
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(row) for row in cursor.fetchmany(max_rows)]
        return rows, columns
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def init_db():
    conn = get_connection()
    schema = SQL_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.commit()
    conn.close()
    logger.info("SQLite DB initialized: {}", DB_PATH)


def execute_sql(sql: str) -> tuple[list[dict], list[str]]:
    conn = get_connection()
    try:
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(row) for row in cursor.fetchall()]
        return rows, columns
    except Exception as e:
        logger.error("SQL execute error: {}", e)
        return [], []
    finally:
        conn.close()
