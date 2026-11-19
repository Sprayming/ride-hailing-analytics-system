import pytest
from app.nlsql.sql_executor import validate_sql


class TestSQLInjection:
    """测试SQL注入防护

    闸门实现从「关键词黑名单」换成了「AST 白名单」，
    因此下面的断言也跟着从「放行」改成「拒绝」。
    """

    def test_union_select_injection(self):
        """UNION 本身不危险，危险的是它能把另一张表的数据拼进结果集。

        `users` 不在业务库白名单内，按表白名单判直接拒绝。
        """
        sql = "SELECT * FROM drivers UNION SELECT username, password FROM users"
        assert validate_sql(sql) is False

    def test_union_same_domain_allowed(self):
        """同一业务库内的 UNION 允许（表名都在白名单内）"""
        sql = "SELECT id FROM drivers UNION SELECT driver_id FROM orders"
        assert validate_sql(sql) is True

    def test_attachment_injection(self):
        """ATTACH 不含任何黑名单关键词，但能读写宿主文件"""
        sql = "ATTACH DATABASE '/tmp/evil.db' AS evil"
        assert validate_sql(sql) is False

    def test_pragma_injection(self):
        """PRAGMA 能读表结构、改运行时开关"""
        sql = "PRAGMA table_info(drivers)"
        assert validate_sql(sql) is False

    def test_load_extension_injection(self):
        """load_extension 能加载任意动态库"""
        sql = "SELECT load_extension('/tmp/evil')"
        assert validate_sql(sql) is False

    def test_multi_statement_injection(self):
        """分号拼接第二条语句必须拒绝"""
        sql = "SELECT * FROM drivers; DROP TABLE drivers;"
        assert validate_sql(sql) is False

    def test_alter_table_injection(self):
        """测试ALTER TABLE注入"""
        sql = "SELECT * FROM drivers; ALTER TABLE drivers ADD COLUMN hacked VARCHAR(100);"
        assert validate_sql(sql) is False

    def test_drop_table_injection(self):
        """测试DROP TABLE注入"""
        sql = "SELECT * FROM drivers; DROP TABLE drivers;"
        assert validate_sql(sql) is False

    def test_insert_injection(self):
        """测试INSERT注入"""
        sql = "SELECT * FROM drivers; INSERT INTO users (username, password) VALUES ('hacker', 'password');"
        assert validate_sql(sql) is False

    def test_update_injection(self):
        """测试UPDATE注入"""
        sql = "SELECT * FROM drivers; UPDATE users SET password = 'hacked' WHERE username = 'admin';"
        assert validate_sql(sql) is False


class TestInputValidation:
    """测试输入验证"""

    def test_special_characters(self):
        """测试SQL特殊字符"""
        sql = "SELECT * FROM drivers WHERE name = 'test' OR '1'='1'"
        assert validate_sql(sql) is True

    def test_unicode_characters(self):
        """测试Unicode字符"""
        sql = "SELECT * FROM drivers WHERE name = '张三'"
        assert validate_sql(sql) is True

    def test_very_long_query(self):
        """测试超长查询"""
        long_sql = "SELECT * FROM drivers WHERE " + " OR ".join([f"id = {i}" for i in range(1000)])
        assert validate_sql(long_sql) is True
