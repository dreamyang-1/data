# -*- coding: utf-8 -*-
"""SQL 方言适配层：按数据源登记的 db_type 生成对应方言的 SQL。

只认目录库 semantic_model_data_source.db_type 的登记值；请求参数不参与
方言选择。MySQL 方言与历史行为逐字节一致，其余方言按标准语法生成。

支持的 db_type 登记值（大小写不敏感）：
- MySQL / MariaDB        -> mysql / mariadb（pymysql 驱动）
- PostgreSQL / PG        -> postgresql（psycopg2 驱动）
- Oracle                 -> oracle（oracledb 驱动）
- SQL Server / MSSQL     -> sqlserver（pymssql 驱动）
- DM / 达梦 / Dameng     -> dm（dmPython 驱动，Oracle 兼容语法）
"""

from typing import Optional


class UnsupportedDbType(ValueError):
    """数据源登记了暂不支持的数据库类型。"""

    def __init__(self, db_type: str):
        super().__init__(f"暂不支持的数据源类型: {db_type}")
        self.db_type = db_type


class SqlDialect:
    """方言基类：标识符引用、时间桶、时间范围、LIMIT、水印查询。"""

    key = ''
    # mysql 家族沿用原执行路径；generic 家族走通用驱动执行路径
    family = 'generic'
    supports_consistent_snapshot = False
    snapshot_id_prefix = 'generic-consistent'

    # ---------- db_type 归一化 ----------

    @staticmethod
    def normalize(db_type: Optional[str]) -> Optional[str]:
        """把登记值归一化为方言 key；无法识别返回 None。"""
        raw = str(db_type or '').strip().lower()
        if not raw:
            return None
        compact = raw.replace(' ', '').replace('-', '').replace('_', '')
        if 'maria' in raw:
            return 'mariadb'
        if 'mysql' in raw:
            return 'mysql'
        if 'postgres' in raw or compact == 'pg':
            return 'postgresql'
        if 'oracle' in raw:
            return 'oracle'
        if compact in ('sqlserver', 'mssql'):
            return 'sqlserver'
        if compact in ('dm', '达梦', 'dameng'):
            return 'dm'
        return None

    # ---------- 驱动名 ----------

    @property
    def driver_module(self) -> str:
        return ''

    # ---------- 标识符与别名 ----------

    @staticmethod
    def quote_ident(name: str) -> str:
        raise NotImplementedError

    @classmethod
    def select_alias(cls, expr: str, alias: str) -> str:
        return f"{expr} AS {cls.quote_ident(alias)}"

    @classmethod
    def order_alias(cls, alias: str) -> str:
        return cls.quote_ident(alias)

    # ---------- 时间桶（SELECT / GROUP BY 粒度） ----------

    def time_bucket(self, field: str, granularity: Optional[str]) -> str:
        normalized = (granularity or '').strip().lower()
        if not normalized:
            return field
        builder = self._bucket_builders().get(normalized)
        if builder is None:
            raise ValueError(f"不支持的时间粒度: {granularity}")
        return builder(field)

    def _bucket_builders(self) -> dict:
        raise NotImplementedError

    # ---------- 时间范围谓词（this_month 等 5 类不可移植表达式） ----------

    def time_predicate(self, time_type: str, field: str) -> str:
        builder = getattr(self, f'_pred_{time_type}', None)
        if builder is None:
            raise ValueError(f"不支持的时间范围类型: {time_type}")
        return builder(field)

    def _pred_this_month(self, field: str) -> str:
        raise NotImplementedError

    def _pred_last_month(self, field: str) -> str:
        raise NotImplementedError

    def _pred_this_year(self, field: str) -> str:
        raise NotImplementedError

    def _pred_today(self, field: str) -> str:
        raise NotImplementedError

    def _pred_yesterday(self, field: str) -> str:
        raise NotImplementedError

    # ---------- LIMIT ----------

    @staticmethod
    def select_prefix(keyword: str, limit: Optional[int]) -> str:
        """返回 SELECT 关键字部分（SQL Server 用 TOP 在前缀里实现 LIMIT）。"""
        return keyword

    @staticmethod
    def limit_suffix(limit: Optional[int]) -> str:
        return f"LIMIT {limit}" if limit is not None else ''

    # ---------- 水印 ----------

    @classmethod
    def watermark_sql(cls, table: str, column: str) -> str:
        return (
            f"SELECT MAX({cls.quote_ident(table)}.{cls.quote_ident(column)}) "
            f"AS source_data_as_of FROM {cls.quote_ident(table)}"
        )

    # ---------- 参数绑定风格：format(%s/%(name)s) | named(:name/:n) | qmark(?) ----------

    bind_style = 'format'

    def bind_parameters(self, sql: str, parameters) -> tuple:
        """把 format 风格占位符（%s / %(name)s）转换为驱动风格。

        MySQL/PG/pymssql 原生支持 format 风格，原样返回；Oracle/达梦 转换为
        命名或问号占位符。参数个数不匹配时抛错，避免静默错绑。
        """
        import re as _re
        if self.bind_style == 'format':
            return sql, parameters
        named_names = _re.findall(r'%\(([^)]+)\)s', sql)
        if self.bind_style == 'named':
            if named_names:
                converted = _re.sub(r'%\(([^)]+)\)s', lambda m: ':' + m.group(1), sql)
                return converted, parameters
            values = (list(parameters.values()) if isinstance(parameters, dict)
                      else list(parameters))
            count = len(_re.findall(r'%s', sql))
            if count != len(values):
                raise ValueError(f"SQL参数占位符数量不匹配: 期望{count}个，实际{len(values)}个")
            counter = iter(range(1, count + 1))
            converted = _re.sub(r'%s', lambda m: f':{next(counter)}', sql)
            return converted, values
        if self.bind_style == 'qmark':
            if named_names:
                values = [parameters[name] for name in named_names]
                converted = _re.sub(r'%\(([^)]+)\)s', '?', sql)
            else:
                values = (list(parameters.values()) if isinstance(parameters, dict)
                          else list(parameters))
                converted = sql
            converted = _re.sub(r'%s', '?', converted)
            count = converted.count('?')
            if count != len(values):
                raise ValueError(f"SQL参数占位符数量不匹配: 期望{count}个，实际{len(values)}个")
            return converted, values
        return sql, parameters


class MySqlDialect(SqlDialect):
    """MySQL/MariaDB：与历史生成行为逐字节一致。"""

    key = 'mysql'
    family = 'mysql'
    supports_consistent_snapshot = True
    snapshot_id_prefix = 'mysql-consistent'
    bind_style = 'format'

    @property
    def driver_module(self) -> str:
        return 'pymysql'

    @staticmethod
    def quote_ident(name: str) -> str:
        return f"`{str(name).replace('`', '``')}`"

    def _bucket_builders(self) -> dict:
        return {
            'second': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:%s')",
            'seconds': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:%s')",
            '秒': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:%s')",
            '秒钟': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:%s')",
            'minute': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:00')",
            'minutes': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:00')",
            '分': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:00')",
            '分钟': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:%i:00')",
            'hour': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:00:00')",
            'hours': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:00:00')",
            '时': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:00:00')",
            '小时': lambda f: f"DATE_FORMAT({f}, '%Y-%m-%d %H:00:00')",
            'day': lambda f: f"DATE({f})",
            'days': lambda f: f"DATE({f})",
            '日': lambda f: f"DATE({f})",
            '天': lambda f: f"DATE({f})",
            '自然日': lambda f: f"DATE({f})",
            'week': lambda f: f"DATE_FORMAT({f}, '%x-W%v')",
            'weeks': lambda f: f"DATE_FORMAT({f}, '%x-W%v')",
            '周': lambda f: f"DATE_FORMAT({f}, '%x-W%v')",
            '星期': lambda f: f"DATE_FORMAT({f}, '%x-W%v')",
            'month': lambda f: f"DATE_FORMAT({f}, '%Y-%m')",
            'months': lambda f: f"DATE_FORMAT({f}, '%Y-%m')",
            '月': lambda f: f"DATE_FORMAT({f}, '%Y-%m')",
            '月份': lambda f: f"DATE_FORMAT({f}, '%Y-%m')",
            '自然月': lambda f: f"DATE_FORMAT({f}, '%Y-%m')",
            'quarter': lambda f: f"CONCAT(YEAR({f}), '-Q', QUARTER({f}))",
            'quarters': lambda f: f"CONCAT(YEAR({f}), '-Q', QUARTER({f}))",
            '季度': lambda f: f"CONCAT(YEAR({f}), '-Q', QUARTER({f}))",
            '季': lambda f: f"CONCAT(YEAR({f}), '-Q', QUARTER({f}))",
            'half_year': lambda f: f"CONCAT(YEAR({f}), '-H', IF(MONTH({f}) <= 6, 1, 2))",
            '半年': lambda f: f"CONCAT(YEAR({f}), '-H', IF(MONTH({f}) <= 6, 1, 2))",
            '半年度': lambda f: f"CONCAT(YEAR({f}), '-H', IF(MONTH({f}) <= 6, 1, 2))",
            'year': lambda f: f"YEAR({f})",
            'years': lambda f: f"YEAR({f})",
            '年': lambda f: f"YEAR({f})",
            '自然年': lambda f: f"YEAR({f})",
            '年度': lambda f: f"YEAR({f})",
        }

    def _pred_this_month(self, field: str) -> str:
        return (f"{field} >= DATE_FORMAT(CURDATE(), '%Y-%m-01') AND "
                f"{field} < DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)")

    def _pred_last_month(self, field: str) -> str:
        return (f"{field} >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') AND "
                f"{field} < DATE_FORMAT(CURDATE(), '%Y-%m-01')")

    def _pred_this_year(self, field: str) -> str:
        return (f"{field} >= CONCAT(YEAR(CURDATE()), '-01-01') AND "
                f"{field} < CONCAT(YEAR(CURDATE()) + 1, '-01-01')")

    def _pred_today(self, field: str) -> str:
        return f"DATE({field}) = CURDATE()"

    def _pred_yesterday(self, field: str) -> str:
        return f"DATE({field}) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)"


class MariaDbDialect(MySqlDialect):
    key = 'mariadb'


class PostgreSqlDialect(SqlDialect):
    key = 'postgresql'
    family = 'generic'
    supports_consistent_snapshot = True
    snapshot_id_prefix = 'postgresql-consistent'
    bind_style = 'format'

    @property
    def driver_module(self) -> str:
        return 'psycopg2'

    @staticmethod
    def quote_ident(name: str) -> str:
        return f'"{str(name).replace(chr(34), chr(34) * 2)}"'

    def _bucket_builders(self) -> dict:
        return {
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:MI:SS')")
               for k in ('second', 'seconds', '秒', '秒钟')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:MI:00')")
               for k in ('minute', 'minutes', '分', '分钟')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:00:00')")
               for k in ('hour', 'hours', '时', '小时')},
            **{k: (lambda f: f"CAST({f} AS DATE)")
               for k in ('day', 'days', '日', '天', '自然日')},
            **{k: (lambda f: f"TO_CHAR({f}, 'IYYY') || '-W' || TO_CHAR({f}, 'IW')")
               for k in ('week', 'weeks', '周', '星期')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM')")
               for k in ('month', 'months', '月', '月份', '自然月')},
            **{k: (lambda f: f"CONCAT(EXTRACT(YEAR FROM {f}), '-Q', EXTRACT(QUARTER FROM {f}))")
               for k in ('quarter', 'quarters', '季度', '季')},
            **{k: (lambda f: f"CONCAT(EXTRACT(YEAR FROM {f}), '-H', "
                            f"CASE WHEN EXTRACT(MONTH FROM {f}) <= 6 THEN 1 ELSE 2 END)")
               for k in ('half_year', '半年', '半年度')},
            **{k: (lambda f: f"EXTRACT(YEAR FROM {f})")
               for k in ('year', 'years', '年', '自然年', '年度')},
        }

    def _pred_this_month(self, field: str) -> str:
        return (f"{field} >= DATE_TRUNC('month', CURRENT_DATE) AND "
                f"{field} < DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month'")

    def _pred_last_month(self, field: str) -> str:
        return (f"{field} >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' AND "
                f"{field} < DATE_TRUNC('month', CURRENT_DATE)")

    def _pred_this_year(self, field: str) -> str:
        return (f"{field} >= DATE_TRUNC('year', CURRENT_DATE) AND "
                f"{field} < DATE_TRUNC('year', CURRENT_DATE) + INTERVAL '1 year'")

    def _pred_today(self, field: str) -> str:
        return f"CAST({field} AS DATE) = CURRENT_DATE"

    def _pred_yesterday(self, field: str) -> str:
        return f"CAST({field} AS DATE) = CURRENT_DATE - INTERVAL '1 day'"


class OracleDialect(SqlDialect):
    """Oracle 与达梦（DM8 兼容 Oracle 语法）。"""

    key = 'oracle'
    family = 'generic'
    supports_consistent_snapshot = True  # SET TRANSACTION READ ONLY 事务级一致性
    snapshot_id_prefix = 'oracle-consistent'
    bind_style = 'named'

    @property
    def driver_module(self) -> str:
        return 'oracledb'

    @staticmethod
    def quote_ident(name: str) -> str:
        return f'"{str(name).replace(chr(34), chr(34) * 2)}"'

    def _bucket_builders(self) -> dict:
        return {
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:MI:SS')")
               for k in ('second', 'seconds', '秒', '秒钟')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:MI:00')")
               for k in ('minute', 'minutes', '分', '分钟')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM-DD HH24:00:00')")
               for k in ('hour', 'hours', '时', '小时')},
            **{k: (lambda f: f"TRUNC({f})")
               for k in ('day', 'days', '日', '天', '自然日')},
            **{k: (lambda f: f"TO_CHAR({f}, 'IYYY') || '-W' || TO_CHAR({f}, 'IW')")
               for k in ('week', 'weeks', '周', '星期')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY-MM')")
               for k in ('month', 'months', '月', '月份', '自然月')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY') || '-Q' || TO_CHAR({f}, 'Q')")
               for k in ('quarter', 'quarters', '季度', '季')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY') || '-H' || "
                            f"CASE WHEN TO_NUMBER(TO_CHAR({f}, 'MM')) <= 6 THEN 1 ELSE 2 END")
               for k in ('half_year', '半年', '半年度')},
            **{k: (lambda f: f"TO_CHAR({f}, 'YYYY')")
               for k in ('year', 'years', '年', '自然年', '年度')},
        }

    def _pred_this_month(self, field: str) -> str:
        return f"{field} >= TRUNC(SYSDATE, 'MM') AND {field} < ADD_MONTHS(TRUNC(SYSDATE, 'MM'), 1)"

    def _pred_last_month(self, field: str) -> str:
        return (f"{field} >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -1) AND "
                f"{field} < TRUNC(SYSDATE, 'MM')")

    def _pred_this_year(self, field: str) -> str:
        return f"{field} >= TRUNC(SYSDATE, 'YYYY') AND {field} < ADD_MONTHS(TRUNC(SYSDATE, 'YYYY'), 12)"

    def _pred_today(self, field: str) -> str:
        return f"TRUNC({field}) = TRUNC(SYSDATE)"

    def _pred_yesterday(self, field: str) -> str:
        return f"TRUNC({field}) = TRUNC(SYSDATE) - 1"

    @staticmethod
    def limit_suffix(limit: Optional[int]) -> str:
        return f"FETCH FIRST {limit} ROWS ONLY" if limit is not None else ''


class DamengDialect(OracleDialect):
    """达梦 DM8：Oracle 兼容语法；驱动用 dmPython。"""

    key = 'dm'
    snapshot_id_prefix = 'dm-consistent'
    bind_style = 'qmark'

    @property
    def driver_module(self) -> str:
        return 'dmPython'


class SqlServerDialect(SqlDialect):
    key = 'sqlserver'
    family = 'generic'
    supports_consistent_snapshot = False  # SNAPSHOT 隔离需库级开关，默认降级为无快照证据
    snapshot_id_prefix = 'sqlserver-consistent'
    bind_style = 'format'

    @property
    def driver_module(self) -> str:
        return 'pymssql'

    @staticmethod
    def quote_ident(name: str) -> str:
        return f'[{str(name).replace("]", "]]")}]'

    def _bucket_builders(self) -> dict:
        return {
            **{k: (lambda f: f"CONVERT(varchar(19), {f}, 120)")
               for k in ('second', 'seconds', '秒', '秒钟')},
            **{k: (lambda f: f"CONVERT(varchar(13), {f}, 120) + ':00'")
               for k in ('minute', 'minutes', '分', '分钟')},
            **{k: (lambda f: f"CONVERT(varchar(13), {f}, 120) + ':00:00'")
               for k in ('hour', 'hours', '时', '小时')},
            **{k: (lambda f: f"CAST({f} AS DATE)")
               for k in ('day', 'days', '日', '天', '自然日')},
            **{k: (lambda f: f"CONCAT(DATEPART(ISO_YEAR, {f}), '-W', DATEPART(ISO_WEEK, {f}))")
               for k in ('week', 'weeks', '周', '星期')},
            **{k: (lambda f: f"CONVERT(varchar(7), {f}, 120)")
               for k in ('month', 'months', '月', '月份', '自然月')},
            **{k: (lambda f: f"CONCAT(YEAR({f}), '-Q', DATEPART(QUARTER, {f}))")
               for k in ('quarter', 'quarters', '季度', '季')},
            **{k: (lambda f: f"CONCAT(YEAR({f}), '-H', CASE WHEN MONTH({f}) <= 6 THEN 1 ELSE 2 END)")
               for k in ('half_year', '半年', '半年度')},
            **{k: (lambda f: f"YEAR({f})")
               for k in ('year', 'years', '年', '自然年', '年度')},
        }

    def _pred_this_month(self, field: str) -> str:
        return (f"{field} >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1) AND "
                f"{field} < DATEADD(MONTH, 1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))")

    def _pred_last_month(self, field: str) -> str:
        return (f"{field} >= DATEADD(MONTH, -1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)) AND "
                f"{field} < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)")

    def _pred_this_year(self, field: str) -> str:
        return (f"{field} >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1) AND "
                f"{field} < DATEFROMPARTS(YEAR(GETDATE()) + 1, 1, 1)")

    def _pred_today(self, field: str) -> str:
        return f"CAST({field} AS DATE) = CAST(GETDATE() AS DATE)"

    def _pred_yesterday(self, field: str) -> str:
        return f"CAST({field} AS DATE) = DATEADD(DAY, -1, CAST(GETDATE() AS DATE))"

    @staticmethod
    def select_prefix(keyword: str, limit: Optional[int]) -> str:
        if limit is None:
            return keyword
        # SELECT / SELECT DISTINCT -> SELECT [DISTINCT] TOP (n)
        top = f'TOP ({int(limit)})'
        if keyword.strip().upper() == 'SELECT DISTINCT':
            return f'SELECT DISTINCT {top}'
        return f'SELECT {top}'

    @staticmethod
    def limit_suffix(limit: Optional[int]) -> str:
        return ''


_DIALECTS = {
    'mysql': MySqlDialect(),
    'mariadb': MariaDbDialect(),
    'postgresql': PostgreSqlDialect(),
    'oracle': OracleDialect(),
    'dm': DamengDialect(),
    'sqlserver': SqlServerDialect(),
}

_FALLBACK_DIALECT = _DIALECTS['mysql']


def get_dialect(db_type: Optional[str]) -> SqlDialect:
    """按登记的 db_type 取方言；空值回落 MySQL（与历史行为一致）。"""
    key = SqlDialect.normalize(db_type)
    if key is None:
        if db_type:
            raise UnsupportedDbType(db_type)
        return _FALLBACK_DIALECT
    return _DIALECTS[key]


def mysql_dialect() -> SqlDialect:
    return _FALLBACK_DIALECT
