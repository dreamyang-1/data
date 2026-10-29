# -*- coding: utf-8 -*-
"""多数据库方言单元测试：db_type 归一化、时间桶、时间谓词、LIMIT、别名、参数绑定。

MySQL 方言必须与历史生成行为逐字节一致（回归保护）。
"""
import unittest

from sql_dialects import (
    DamengDialect,
    MySqlDialect,
    OracleDialect,
    PostgreSqlDialect,
    SqlServerDialect,
    SqlDialect,
    UnsupportedDbType,
    get_dialect,
    mysql_dialect,
)


class NormalizeTests(unittest.TestCase):
    def test_registry_values(self):
        cases = {
            'MySQL': 'mysql', 'mysql': 'mysql', 'MariaDB': 'mariadb',
            'PostgreSQL': 'postgresql', 'PG': 'postgresql',
            'Oracle': 'oracle', 'SQL Server': 'sqlserver', 'MSSQL': 'sqlserver',
            'DM': 'dm', '达梦': 'dm', 'Dameng': 'dm',
        }
        for raw, expected in cases.items():
            self.assertEqual(SqlDialect.normalize(raw), expected, raw)

    def test_unknown_and_empty(self):
        self.assertIsNone(SqlDialect.normalize('elasticsearch'))
        self.assertIsNone(SqlDialect.normalize(''))
        self.assertIsNone(SqlDialect.normalize(None))

    def test_get_dialect(self):
        self.assertIsInstance(get_dialect('MySQL'), MySqlDialect)
        self.assertIsInstance(get_dialect(None), MySqlDialect)  # 空登记回落 MySQL
        with self.assertRaises(UnsupportedDbType):
            get_dialect('elasticsearch')


class MySqlRegressionTests(unittest.TestCase):
    """MySQL 方言必须逐字节复现历史表达式。"""

    def setUp(self):
        self.d = mysql_dialect()

    def test_time_buckets(self):
        f = 'order_info.pay_time'
        cases = {
            'second': f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:%s')",
            '分钟': f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:00')",
            'hour': f"DATE_FORMAT({f}, '%Y-%m-%d %H:00:00')",
            'day': f"DATE({f})",
            'week': f"DATE_FORMAT({f}, '%x-W%v')",
            '月': f"DATE_FORMAT({f}, '%Y-%m')",
            'quarter': f"CONCAT(YEAR({f}), '-Q', QUARTER({f}))",
            'half_year': f"CONCAT(YEAR({f}), '-H', IF(MONTH({f}) <= 6, 1, 2))",
            'year': f"YEAR({f})",
        }
        for grain, expected in cases.items():
            self.assertEqual(self.d.time_bucket(f, grain), expected, grain)

    def test_time_bucket_no_granularity(self):
        self.assertEqual(self.d.time_bucket('t.f', None), 't.f')
        self.assertEqual(self.d.time_bucket('t.f', '  '), 't.f')

    def test_time_bucket_unknown_raises(self):
        with self.assertRaises(ValueError):
            self.d.time_bucket('t.f', 'decade')

    def test_predicates(self):
        f = 'order_info.pay_time'
        self.assertEqual(
            self.d.time_predicate('this_month', f),
            f"{f} >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND "
            f"{f} < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)")
        self.assertEqual(
            self.d.time_predicate('last_month', f),
            f"{f} >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') AND "
            f"{f} < DATE_FORMAT(CURDATE(), '%Y-%m-01')")
        self.assertEqual(
            self.d.time_predicate('this_year', f),
            f"{f} >= CONCAT(YEAR(CURDATE()), '-01-01') AND "
            f"{f} < CONCAT(YEAR(CURDATE()) + 1, '-01-01')")
        self.assertEqual(self.d.time_predicate('today', f), f"DATE({f}) = CURDATE()")
        self.assertEqual(self.d.time_predicate('yesterday', f),
                         f"DATE({f}) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)")

    def test_alias_escaping(self):
        self.assertEqual(self.d.select_alias('COUNT(*)', '总数'), 'COUNT(*) AS `总数`')
        # 反引号别名内部成对转义，收尾是 两个转义反引号 + 闭合反引号
        self.assertEqual(self.d.select_alias('x', '登记`总数`'), 'x AS `登记``总数```')
        self.assertEqual(self.d.order_alias('指标A'), '`指标A`')

    def test_limit(self):
        self.assertEqual(self.d.limit_suffix(100), 'LIMIT 100')
        self.assertEqual(self.d.select_prefix('SELECT DISTINCT', 100), 'SELECT DISTINCT')

    def test_watermark(self):
        self.assertEqual(
            self.d.watermark_sql('order_info', 'pay_time'),
            "SELECT MAX(`order_info`.`pay_time`) AS source_data_as_of FROM `order_info`")


class PostgreSqlTests(unittest.TestCase):
    def setUp(self):
        self.d = get_dialect('PostgreSQL')

    def test_buckets(self):
        f = 't.pay_time'
        self.assertEqual(self.d.time_bucket(f, 'month'), f"TO_CHAR({f}, 'YYYY-MM')")
        self.assertEqual(self.d.time_bucket(f, 'day'), f"CAST({f} AS DATE)")
        self.assertEqual(self.d.time_bucket(f, 'week'),
                         f"TO_CHAR({f}, 'IYYY') || '-W' || TO_CHAR({f}, 'IW')")
        self.assertEqual(self.d.time_bucket(f, 'quarter'),
                         f"CONCAT(EXTRACT(YEAR FROM {f}), '-Q', EXTRACT(QUARTER FROM {f}))")
        self.assertEqual(self.d.time_bucket(f, 'year'), f"EXTRACT(YEAR FROM {f})")

    def test_predicates(self):
        f = 't.pay_time'
        self.assertIn("DATE_TRUNC('month', CURRENT_DATE)", self.d.time_predicate('this_month', f))
        self.assertIn("INTERVAL '1 month'", self.d.time_predicate('this_month', f))
        self.assertIn("CURRENT_DATE - INTERVAL '1 day'", self.d.time_predicate('yesterday', f))

    def test_alias_and_limit(self):
        self.assertEqual(self.d.select_alias('x', '总数'), 'x AS "总数"')
        self.assertEqual(self.d.limit_suffix(50), 'LIMIT 50')

    def test_bind_format_style(self):
        sql, params = self.d.bind_parameters('SELECT %s, %(name)s', {'name': 1})
        self.assertEqual(params, {'name': 1})


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.d = get_dialect('Oracle')

    def test_buckets(self):
        f = 'T.PAY_TIME'
        self.assertEqual(self.d.time_bucket(f, 'month'), f"TO_CHAR({f}, 'YYYY-MM')")
        self.assertEqual(self.d.time_bucket(f, 'day'), f"TRUNC({f})")
        self.assertEqual(self.d.time_bucket(f, 'quarter'),
                         f"TO_CHAR({f}, 'YYYY') || '-Q' || TO_CHAR({f}, 'Q')")
        self.assertIn('CASE WHEN TO_NUMBER(TO_CHAR(', self.d.time_bucket(f, 'half_year'))

    def test_predicates(self):
        f = 'T.PAY_TIME'
        self.assertEqual(self.d.time_predicate('this_month', f),
                         f"{f} >= TRUNC(SYSDATE, 'MM') AND {f} < ADD_MONTHS(TRUNC(SYSDATE, 'MM'), 1)")
        self.assertEqual(self.d.time_predicate('today', f), f"TRUNC({f}) = TRUNC(SYSDATE)")

    def test_limit_fetch_first(self):
        self.assertEqual(self.d.limit_suffix(100), 'FETCH FIRST 100 ROWS ONLY')
        self.assertEqual(self.d.select_prefix('SELECT', 100), 'SELECT')

    def test_bind_named_style(self):
        sql, params = self.d.bind_parameters('SELECT %s, %s', {'a': 1, 'b': 2})
        self.assertEqual(sql, 'SELECT :1, :2')
        self.assertEqual(params, [1, 2])
        sql, params = self.d.bind_parameters('WHERE x = %(x)s', {'x': 5})
        self.assertEqual(sql, 'WHERE x = :x')
        self.assertEqual(params, {'x': 5})

    def test_bind_count_mismatch(self):
        with self.assertRaises(ValueError):
            self.d.bind_parameters('SELECT %s, %s', {'a': 1})


class DamengTests(unittest.TestCase):
    def setUp(self):
        self.d = get_dialect('达梦')

    def test_is_oracle_family(self):
        self.assertIsInstance(self.d, OracleDialect)
        self.assertIsInstance(self.d, DamengDialect)
        self.assertEqual(self.d.key, 'dm')
        self.assertEqual(self.d.driver_module, 'dmPython')
        self.assertEqual(self.d.limit_suffix(10), 'FETCH FIRST 10 ROWS ONLY')

    def test_bind_qmark(self):
        sql, params = self.d.bind_parameters('x = %(a)s AND y = %(b)s', {'a': 1, 'b': 2})
        self.assertEqual(sql, 'x = ? AND y = ?')
        self.assertEqual(params, [1, 2])
        sql, params = self.d.bind_parameters('x = %s AND y = %s', (1, 2))
        self.assertEqual(sql, 'x = ? AND y = ?')
        self.assertEqual(params, [1, 2])


class SqlServerTests(unittest.TestCase):
    def setUp(self):
        self.d = get_dialect('SQL Server')

    def test_buckets(self):
        f = 't.pay_time'
        self.assertEqual(self.d.time_bucket(f, 'month'), f"CONVERT(varchar(7), {f}, 120)")
        self.assertEqual(self.d.time_bucket(f, 'day'), f"CAST({f} AS DATE)")
        self.assertIn('DATEPART(ISO_WEEK', self.d.time_bucket(f, 'week'))

    def test_top_prefix(self):
        self.assertEqual(self.d.select_prefix('SELECT', 50), 'SELECT TOP (50)')
        self.assertEqual(self.d.select_prefix('SELECT DISTINCT', 50), 'SELECT DISTINCT TOP (50)')
        self.assertEqual(self.d.limit_suffix(50), '')

    def test_predicates(self):
        f = 't.pay_time'
        self.assertIn('DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)',
                      self.d.time_predicate('this_month', f))

    def test_bracket_alias(self):
        self.assertEqual(self.d.order_alias('总数'), '[总数]')
        self.assertEqual(self.d.select_alias('x', 'a]b'), 'x AS [a]]b]')


if __name__ == '__main__':
    unittest.main()
