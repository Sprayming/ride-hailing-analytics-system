"""SQL 安全闸门与只读执行的测试。

覆盖三类风险：
1. 语法层面：多语句、非 SELECT、ATTACH / PRAGMA / load_extension
2. 数据层面：跨表读取（UNION 到非业务表）、跨库读取
3. 执行层面：只读连接写不进去、无界查询被 LIMIT 截断、超时被中断
"""

import sqlite3

import pytest

from app.db import connection as conn_mod
from app.nlsql.sql_executor import run_sql, validate_sql
from app.nlsql.sql_guard import guard_sql


class TestGuardRejects:
    """必须被拒绝的输入"""

    def test_empty(self):
        assert guard_sql("").ok is False
        assert guard_sql("   \n\t ").ok is False

    def test_multiple_statements(self):
        result = guard_sql("SELECT * FROM drivers; DROP TABLE drivers;")
        assert result.ok is False
        assert result.reason == "multiple_statements"

    def test_non_select(self):
        for sql in (
            "INSERT INTO drivers (name) VALUES ('x')",
            "UPDATE drivers SET name = 'x'",
            "DELETE FROM drivers",
            "DROP TABLE drivers",
            "CREATE TABLE t (id INT)",
            "ALTER TABLE drivers ADD COLUMN x INT",
        ):
            result = guard_sql(sql)
            assert result.ok is False, sql
            assert result.reason in {"not_select", "parse_error", "forbidden_node"}, sql

    def test_attach_and_pragma(self):
        for sql in (
            "ATTACH DATABASE '/tmp/evil.db' AS evil",
            "PRAGMA table_info(drivers)",
            "VACUUM",
        ):
            assert guard_sql(sql).ok is False, sql

    def test_forbidden_function(self):
        assert guard_sql("SELECT load_extension('/tmp/evil')").ok is False
        assert guard_sql("SELECT readfile('/etc/passwd')").ok is False

    def test_table_not_in_whitelist(self):
        result = guard_sql("SELECT * FROM users")
        assert result.ok is False
        assert result.reason == "table_not_allowed"

    def test_union_to_other_table_blocked(self):
        result = guard_sql("SELECT id FROM drivers UNION SELECT password FROM users")
        assert result.ok is False
        assert result.reason == "table_not_allowed"

    def test_cross_database_blocked(self):
        result = guard_sql("SELECT * FROM other.drivers")
        assert result.ok is False
        assert result.reason == "cross_database"


class TestGuardAllows:
    """必须放行的正常查询"""

    def test_simple_select_gets_limit(self):
        result = guard_sql("SELECT * FROM drivers", max_rows=50)
        assert result.ok is True
        assert "LIMIT 50" in result.sql.upper()

    def test_existing_limit_is_capped(self):
        result = guard_sql("SELECT * FROM drivers LIMIT 5000", max_rows=50)
        assert result.ok is True
        assert "LIMIT 50" in result.sql.upper()

    def test_join_and_group_by(self):
        sql = (
            "SELECT d.city, COUNT(o.id) AS cnt FROM drivers d "
            "JOIN orders o ON o.driver_id = d.id GROUP BY d.city"
        )
        assert guard_sql(sql).ok is True

    def test_cte_alias_not_treated_as_table(self):
        sql = "WITH recent AS (SELECT * FROM orders) SELECT COUNT(*) FROM recent"
        assert guard_sql(sql).ok is True

    def test_unicode_literal(self):
        assert guard_sql("SELECT * FROM drivers WHERE name = '张三'").ok is True

    def test_validate_sql_matches_guard(self):
        assert validate_sql("SELECT * FROM orders") is True
        assert validate_sql("SELECT * FROM not_a_table") is False


class TestReadonlyExecution:
    """执行层兜底：连接本身写不进去，超时和行数上限生效"""

    @pytest.fixture()
    def temp_db(self, tmp_path, monkeypatch):
        db = tmp_path / "guard_test.db"
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE drivers (id INTEGER PRIMARY KEY, name TEXT);")
        conn.executemany(
            "INSERT INTO drivers (id, name) VALUES (?, ?)",
            [(i, f"d{i}") for i in range(1, 301)],
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(conn_mod, "DB_PATH", db)
        return db

    def test_select_works(self, temp_db):
        rows, columns = conn_mod.execute_readonly_sql("SELECT id FROM drivers LIMIT 3")
        assert columns == ["id"]
        assert len(rows) == 3

    def test_write_is_rejected_at_connection_level(self, temp_db):
        with pytest.raises(sqlite3.OperationalError):
            conn_mod.execute_readonly_sql("INSERT INTO drivers (id, name) VALUES (999, 'x')")

    def test_row_limit(self, temp_db):
        rows, _ = conn_mod.execute_readonly_sql(
            "SELECT id FROM drivers", max_rows=10
        )
        assert len(rows) == 10

    def test_timeout_interrupts_query(self, temp_db):
        with pytest.raises(sqlite3.OperationalError):
            conn_mod.execute_readonly_sql(
                "SELECT COUNT(*) FROM drivers a, drivers b, drivers c, drivers d",
                timeout_seconds=0.01,
            )

    def test_run_sql_rejects_dangerous_statement(self):
        assert run_sql("DROP TABLE drivers") == ([], [])
