"""SQL 安全闸门：用 AST 白名单替代关键字黑名单。

旧实现是「前缀必须是 SELECT + 一批关键字黑名单」。它能挡住 `DROP TABLE`，
但下面这些语句一个黑名单词都不含，照样能打穿：

- `SELECT load_extension('/tmp/evil')` → 加载任意动态库
- `ATTACH DATABASE '/Users/x/.ssh/id_rsa' AS k` → 读写宿主机文件
- `SELECT * FROM users` → 黑名单只关心动词，不关心你读哪张表
- 没有 LIMIT 的全表扫描 → 在客户的库上能把连接池拖死

所以改成解析成 AST 再判定，五条规则：
1. 只允许单条语句（挡住 `SELECT ...; DROP ...`）
2. 根节点必须是 SELECT（挡住一切 DML / DDL / ATTACH / PRAGMA）
3. 表名必须在当前库的表白名单里（挡住 UNION 读别的库表）
4. 危险函数直接拒绝（load_extension / readfile / writefile）
5. 没有 LIMIT 就补一个（挡住无界扫描）

只在「LLM 生成的 SQL」这条链路上使用；报表、看板等内部固定 SQL 不走这里。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

# 能触达文件系统或加载外部代码的函数
FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "eval", "system"}

# 出现即拒绝的节点（根节点已限定 SELECT，这里兜住子查询/CTE 里夹带的写操作）
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,  # ATTACH / PRAGMA / VACUUM / BEGIN 在 sqlglot 里都落到 Command
    exp.Into,     # SELECT ... INTO
)

_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)", re.I)
_ALLOWED_TABLES_CACHE: set[str] | None = None

# 集合运算的根节点不是 Select，但每个分支都是只读查询，允许（表白名单照样生效）
_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)


def _is_read_only_query(node: exp.Expression) -> bool:
    """根节点是否为只读查询：SELECT，或由 SELECT 组成的集合运算。"""
    if isinstance(node, exp.Select):
        return True
    if isinstance(node, _SET_OPERATIONS):
        return _is_read_only_query(node.this) and _is_read_only_query(node.expression)
    return False


@dataclass
class GuardResult:
    """闸门判定结果。

    ok 为 True 时 sql 是补完 LIMIT、重新渲染过的可执行语句；
    ok 为 False 时 reason 是机器可读的原因码，detail 是给人看的补充。
    """

    ok: bool
    sql: str = ""
    reason: str = ""
    detail: str = ""


def allowed_tables() -> set[str]:
    """当前库允许查询的表名白名单（小写，带缓存）。

    优先读实际库的 sqlite_master，库还没建时退回解析 schema 文件，
    保证 CI 上「只有 schema、没有数据文件」也能跑测试。
    """
    global _ALLOWED_TABLES_CACHE
    if _ALLOWED_TABLES_CACHE is not None:
        return _ALLOWED_TABLES_CACHE

    from app.db.connection import DB_PATH, SQL_PATH

    names: set[str] = set()
    if DB_PATH.exists():
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            names = {
                row[0].lower()
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()

    if not names and SQL_PATH.exists():
        names = {m.lower() for m in _CREATE_TABLE_RE.findall(SQL_PATH.read_text(encoding="utf-8"))}

    _ALLOWED_TABLES_CACHE = names
    return names


def _cte_names(node: exp.Expression) -> set[str]:
    """收集 CTE 别名，避免把 `WITH t AS (...) SELECT * FROM t` 的表误判成非法表。"""
    return {cte.alias_or_name.lower() for cte in node.find_all(exp.CTE) if cte.alias_or_name}


def _find_forbidden_node(node: exp.Expression) -> exp.Expression | None:
    for item in node.walk():
        if isinstance(item, FORBIDDEN_NODES):
            return item
    return None


def _find_forbidden_function(node: exp.Expression) -> str:
    for item in node.walk():
        if isinstance(item, exp.Anonymous) and str(item.name).lower() in FORBIDDEN_FUNCTIONS:
            return str(item.name)
        if isinstance(item, exp.Func):
            name = item.sql_name().lower()
            if name in FORBIDDEN_FUNCTIONS:
                return name
    return ""


def _apply_limit(node: exp.Expression, max_rows: int) -> exp.Expression:
    """没有 LIMIT 就补一个；已有但超过上限的就压到上限。"""
    limit = node.args.get("limit")
    if limit is None:
        node.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return node

    try:
        current = int(limit.expression.name)
    except (AttributeError, TypeError, ValueError):
        # LIMIT 是变量或表达式时无法静态判断，直接压到上限
        current = max_rows + 1
    if current > max_rows:
        node.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return node


def guard_sql(sql: str, *, max_rows: int = 200) -> GuardResult:
    """校验并规范化一条待执行的查询语句。

    Args:
        sql: LLM 生成的原始 SQL。
        max_rows: 结果行数上限；无 LIMIT 时自动补，超出时压到该值。

    Returns:
        GuardResult: ok 为 True 时 sql 可直接执行。
    """
    sql = (sql or "").strip().strip("`").strip()
    if not sql:
        return GuardResult(ok=False, reason="empty_sql", detail="SQL 为空")

    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError as exc:
        return GuardResult(ok=False, reason="parse_error", detail=str(exc).splitlines()[0])

    if len(statements) != 1:
        return GuardResult(
            ok=False,
            reason="multiple_statements",
            detail=f"只允许单条语句，实际 {len(statements)} 条",
        )

    node = statements[0]
    if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select):
        node = node.this

    if not _is_read_only_query(node):
        return GuardResult(
            ok=False,
            reason="not_select",
            detail=f"只允许 SELECT，实际是 {type(node).__name__.upper()}",
        )

    forbidden = _find_forbidden_node(node)
    if forbidden is not None:
        return GuardResult(
            ok=False,
            reason="forbidden_node",
            detail=type(forbidden).__name__.upper(),
        )

    bad_func = _find_forbidden_function(node)
    if bad_func:
        return GuardResult(ok=False, reason="forbidden_function", detail=bad_func)

    ctes = _cte_names(node)
    allowed = allowed_tables()
    for table in node.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in ctes:
            continue
        if table.db:
            return GuardResult(
                ok=False,
                reason="cross_database",
                detail=f"{table.db}.{name}",
            )
        if name not in allowed:
            return GuardResult(
                ok=False,
                reason="table_not_allowed",
                detail=name,
            )

    node = _apply_limit(node, max_rows)
    return GuardResult(ok=True, sql=node.sql(dialect="sqlite"))


def schema_path() -> Path:
    """schema 文件路径（供测试断言用）。"""
    from app.db.connection import SQL_PATH

    return SQL_PATH
