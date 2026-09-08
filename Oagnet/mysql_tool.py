"""
MySQL 查询工具 - 适配新数据库表结构（层级隔离版）

数据隔离层级:
  semantic_model（语义建模）
    └── semantic_model_business_domain（业务域）
          └── 实体 / 指标 / 维度 / 关系 / 属性 / 枚举
  data_source（数据源）
    └── semantic_model_table（物理表元信息）
          └── semantic_model_field（物理字段元信息）

关联关系:
  - semantic_model:                       语义建模主表
  - semantic_model_business_domain:       业务域（semantic_model_id → semantic_model.id）
  - semantic_model_entity_type:           实体（business_domain_id → business_domain.id）
  - semantic_model_attribute_config:      属性（entity_type_id → entity_type.id）
  - semantic_model_relation_config:       关系（source_entity_type_id → entity_type.id）
  - semantic_model_indicator:             指标（semantic_model_id + business_domain_id）
  - semantic_model_dimension:             维度（semantic_model_id，跨业务域共享）
  - semantic_model_enum:                  枚举
  - semantic_model_entity_bind_indicator: 实体绑定指标
  - semantic_model_table:                 物理表元信息（data_source_id 隔离，semantic_model_id 可空）
  - semantic_model_field:                 物理字段元信息（table_id → semantic_model_table.id）

调用方式:
  # 1. 列出所有语义建模
  models = get_semantic_models()
  # 2. 列出某语义建模下的业务域
  domains = get_business_domains(semantic_model_id=6)
  # 3. 获取某业务域作用域下的全部 DSL（实体/指标/维度）
  dsl = get_dsl_by_scope(semantic_model_id=6, business_domain_id=8)
  # 4. 获取某数据源作用域下的全部物理表 + 字段
  docs = get_table_field_by_scope(data_source_id=2)
  # 5. 获取所有数据源的物理表 + 字段（全量索引重建用）
  docs = load_all_table_fields()
"""

import hashlib
import json
import re
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from config import (
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_USER,
    MYSQL_PASSWORD,
    MYSQL_DATABASE,
    MYSQL_CHARSET,
    MYSQL_CONNECT_TIMEOUT,
    MYSQL_READ_TIMEOUT,
    require_runtime_secret,
)


def _get_connection():
    """创建 MySQL 连接，调用方负责关闭。"""
    return pymysql.connect(
        host=require_runtime_secret(MYSQL_HOST, "OAGNET_MYSQL_HOST/MYSQL_HOST"),
        port=int(MYSQL_PORT),
        user=require_runtime_secret(MYSQL_USER, "OAGNET_MYSQL_USER/MYSQL_USER"),
        password=require_runtime_secret(
            MYSQL_PASSWORD, "OAGNET_MYSQL_PASSWORD/MYSQL_PASSWORD"
        ),
        database=require_runtime_secret(
            MYSQL_DATABASE, "OAGNET_MYSQL_DATABASE/MYSQL_DATABASE"
        ),
        charset=MYSQL_CHARSET,
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        read_timeout=MYSQL_READ_TIMEOUT,
        write_timeout=MYSQL_READ_TIMEOUT,
        autocommit=True,
    )


def _parse_json(value):
    """安全解析 JSON 字段，非字符串或解析失败时原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _query(sql: str, args=None):
    """执行查询并返回 dict 列表，异常向上抛出。"""
    conn = _get_connection()
    try:
        with conn.cursor(DictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def resolve_entity_code_by_table(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    table_name: str,
) -> str | None:
    """Resolve the current active entity for a physical table.

    This deliberately queries semantic metadata on every ASL request. Users may
    add, remove or replace entities, so the agent must not rely on a hard-coded
    entity code or a stale metric binding.
    """
    if not semantic_model_id or not table_name:
        return None
    params: list = [semantic_model_id, table_name]
    domain_clause = ""
    if business_domain_id is None:
        domain_ids: list[int] = []
    elif isinstance(business_domain_id, int) and not isinstance(business_domain_id, bool):
        domain_ids = [business_domain_id]
    elif isinstance(business_domain_id, (list, tuple)):
        domain_ids = list(dict.fromkeys(business_domain_id))
    else:
        raise ValueError("business domain scope must be an integer, list, tuple, or None")
    if any(type(item) is not int or item <= 0 for item in domain_ids):
        raise ValueError("business domain scope must contain positive integers")
    if domain_ids:
        placeholders = ", ".join(["%s"] * len(domain_ids))
        domain_clause = f" AND e.business_domain_id IN ({placeholders})"
        params.extend(domain_ids)
    # 历史数据中 semantic_model_entity_type.semantic_model_id 可能为空，
    # 但 business_domain_id 始终指向所属语义模型。通过业务域表确定作用域，
    # 既兼容历史数据，也避免仅按物理表名跨模型串数据。
    rows = _query(
        """
        SELECT e.code
        FROM semantic_model_entity_type e
        JOIN semantic_model_business_domain b
          ON b.id = e.business_domain_id
         AND COALESCE(b.is_deleted, 0) = 0
        WHERE b.semantic_model_id = %s
          AND e.main_table_name = %s
          AND COALESCE(e.is_deleted, 0) = 0
          AND e.status = 1
        """ + domain_clause
        + " ORDER BY e.update_time DESC, e.id LIMIT 1",
        tuple(params),
    )
    return str(rows[0]["code"]) if rows and rows[0].get("code") else None


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validated_identifier(value: str, *, label: str) -> str:
    """校验动态表/字段标识符，防止把配置值直接变成 SQL 注入入口。"""
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"非法{label}: {value!r}")
    return value


def _normalized_domain_ids(
    business_domain_id: int | list[int] | tuple[int, ...] | None,
) -> list[int]:
    if business_domain_id is None:
        return []
    if isinstance(business_domain_id, int) and not isinstance(
        business_domain_id, bool
    ):
        values = [business_domain_id]
    elif isinstance(business_domain_id, (list, tuple)):
        values = list(business_domain_id)
    else:
        raise ValueError(
            "business domain scope must be an integer, list, tuple, or None"
        )
    if any(type(item) is not int or item <= 0 for item in values):
        raise ValueError("business domain scope must contain positive integers")
    return list(dict.fromkeys(values))


def _data_source_has_exact_value(
    source: dict,
    table_name: str,
    column_name: str,
    value: str,
) -> bool:
    """Check one metadata-approved MySQL field using a bound value only."""
    db_type = re.sub(r"[^a-z]", "", str(source.get("db_type") or "").casefold())
    if db_type not in {"mysql", "mariadb"}:
        raise ValueError("unsupported semantic-model data source type")
    table = _validated_identifier(table_name, label="表名")
    column = _validated_identifier(column_name, label="字段名")
    try:
        port = int(source.get("port"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid semantic-model data source port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid semantic-model data source port")
    host = str(source.get("host") or "").strip()
    username = str(source.get("username") or "").strip()
    database = str(source.get("db_name") or "").strip()
    if not host or not username or not database:
        raise ValueError("incomplete semantic-model data source configuration")

    normalized_value = normalize_catalog_text(value)
    conn = pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=source.get("password") or "",
        database=database,
        charset=MYSQL_CHARSET,
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        read_timeout=MYSQL_READ_TIMEOUT,
        write_timeout=MYSQL_READ_TIMEOUT,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            textual = f"CAST(`{column}` AS CHAR)"
            normalized_column = _normalized_catalog_sql_expression(textual)
            cur.execute(
                # Compare in the textual domain.  MySQL otherwise coerces a
                # non-numeric literal such as a department/brand name to 0
                # when the configured candidate column is numeric, producing
                # false "exact" hits on every zero-valued amount/rate field.
                f"SELECT 1 FROM `{table}` "
                f"WHERE {normalized_column} = %s LIMIT 1",
                (normalized_value,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _authorized_entity_field_rows(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    candidates: list[dict] | tuple[dict, ...],
    *,
    require_unique_main: bool,
    max_candidates: int,
) -> tuple[list[tuple[str, int, str, str]], list[dict]]:
    """Authorize candidate fields against active semantic and physical metadata.

    The entity's business-domain ownership is the model authority.  Historical
    attribute rows can retain a stale denormalized ``semantic_model_id`` after
    a domain is reassigned or republished; filtering on that redundant column
    would make an otherwise current entity attribute disappear from grounding.
    Data source, table and field registrations are still checked against the
    requested model below, so dropping that redundant predicate cannot cross
    the model's physical-data boundary.
    """
    if (
        type(semantic_model_id) is not int
        or semantic_model_id <= 0
    ):
        return [], []
    if not isinstance(candidates, (list, tuple)) or not candidates:
        return [], []
    if len(candidates) > max_candidates:
        return [], []

    requested_domains = set(_normalized_domain_ids(business_domain_id))
    normalized: list[tuple[str, int, str, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return [], []
        entity_code = str(candidate.get("entity_code") or "").strip()
        field = str(candidate.get("field") or "").strip()
        if not entity_code or len(entity_code) > 255 or field.count(".") != 1:
            return [], []
        table_name, column_name = field.split(".", 1)
        table = _validated_identifier(table_name, label="表名")
        column = _validated_identifier(column_name, label="字段名")
        candidate_domain = candidate.get("business_domain_id")
        if type(candidate_domain) is not int or candidate_domain <= 0:
            if len(requested_domains) != 1:
                return [], []
            candidate_domain = next(iter(requested_domains))
        if requested_domains and candidate_domain not in requested_domains:
            return [], []
        item = (entity_code, candidate_domain, table, column)
        if item not in normalized:
            normalized.append(item)

    candidate_clauses: list[str] = []
    params: list = [semantic_model_id]
    for entity_code, domain_id, table, column in normalized:
        candidate_clauses.append(
            "(e.code = %s AND e.business_domain_id = %s "
            "AND a.mapping_table = %s AND a.mapping_column = %s)"
        )
        params.extend((entity_code, domain_id, table, column))

    main_attribute_scope = ""
    if require_unique_main:
        main_attribute_scope = """
         AND COALESCE(a.is_main_attribute, 0) = 1
        """
    unique_main_scope = ""
    if require_unique_main:
        unique_main_scope = """
          AND NOT EXISTS (
              SELECT 1
              FROM semantic_model_attribute_config other
              WHERE other.entity_type_id = e.id
                AND COALESCE(other.is_deleted, '0') = '0'
                AND COALESCE(other.is_main_attribute, 0) = 1
                AND other.mapping_table IS NOT NULL
                AND other.mapping_column IS NOT NULL
                AND (
                    other.mapping_table <> a.mapping_table
                    OR other.mapping_column <> a.mapping_column
                )
          )
        """

    rows = _query(
        """
        SELECT DISTINCT
               e.code AS entity_code,
               e.business_domain_id,
               a.mapping_table,
               a.mapping_column,
               a.is_main_attribute,
               ds.id AS data_source_id,
               ds.db_type,
               ds.host,
               ds.port,
               ds.username,
               ds.password,
               ds.db_name
        FROM semantic_model m
        JOIN semantic_model_business_domain b
          ON b.semantic_model_id = m.id
         AND COALESCE(b.is_deleted, 0) = 0
        JOIN semantic_model_entity_type e
          ON e.business_domain_id = b.id
         AND (e.semantic_model_id = m.id OR e.semantic_model_id IS NULL)
         AND COALESCE(e.is_deleted, 0) = 0
         AND e.status = 1
        JOIN semantic_model_attribute_config a
          ON a.entity_type_id = e.id
         AND COALESCE(a.is_deleted, '0') = '0'
        """
        + main_attribute_scope
        + """
        JOIN semantic_model_data_source ds
          ON ds.id = e.data_source_id
         AND ds.semantic_model_id = m.id
         AND COALESCE(ds.is_deleted, 0) = 0
         AND ds.status = 1
        JOIN semantic_model_table t
          ON t.semantic_model_id = m.id
         AND t.data_source_id = ds.id
         AND t.name = a.mapping_table
         AND COALESCE(t.is_deleted, 0) = 0
        JOIN semantic_model_field f
          ON f.semantic_model_id = m.id
         AND f.data_source_id = ds.id
         AND f.table_id = t.id
         AND f.name = a.mapping_column
         AND COALESCE(f.is_deleted, 0) = 0
        WHERE m.id = %s
          AND COALESCE(m.is_deleted, 0) = 0
        """
        + unique_main_scope
        + """
          AND (
        """
        + " OR ".join(candidate_clauses)
        + ")",
        tuple(params),
    )
    return normalized, list(rows)


def _resolve_exact_entity_fields(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    candidates: list[dict] | tuple[dict, ...],
    value: str,
    *,
    require_unique_main: bool,
    max_candidates: int,
) -> list[str]:
    """Shared metadata authorization and exact physical-source lookup."""
    if not isinstance(value, str) or not value.strip():
        return []
    normalized, rows = _authorized_entity_field_rows(
        semantic_model_id,
        business_domain_id,
        candidates,
        require_unique_main=require_unique_main,
        max_candidates=max_candidates,
    )
    approved = set(normalized)
    matched_fields: set[str] = set()
    checked: set[tuple[int, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("entity_code") or ""),
            row.get("business_domain_id"),
            str(row.get("mapping_table") or ""),
            str(row.get("mapping_column") or ""),
        )
        if key not in approved:
            continue
        table, column = key[2], key[3]
        source_key = (int(row.get("data_source_id")), table, column)
        if source_key in checked:
            continue
        checked.add(source_key)
        if _data_source_has_exact_value(row, table, column, value.strip()):
            matched_fields.add(f"{table}.{column}")
    return sorted(matched_fields)


def _data_source_catalog_matches(
    source: dict,
    table_name: str,
    column_name: str,
    value: str,
) -> list[str]:
    """Return at most three canonical values related to one bound mention."""
    db_type = re.sub(r"[^a-z]", "", str(source.get("db_type") or "").casefold())
    if db_type not in {"mysql", "mariadb"}:
        raise ValueError("unsupported semantic-model data source type")
    table = _validated_identifier(table_name, label="表名")
    column = _validated_identifier(column_name, label="字段名")
    port = int(source.get("port"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid semantic-model data source port")
    host = str(source.get("host") or "").strip()
    username = str(source.get("username") or "").strip()
    database = str(source.get("db_name") or "").strip()
    if not host or not username or not database:
        raise ValueError("incomplete semantic-model data source configuration")

    normalized_value = normalize_catalog_text(value)
    conn = pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=source.get("password") or "",
        database=database,
        charset=MYSQL_CHARSET,
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        read_timeout=MYSQL_READ_TIMEOUT,
        write_timeout=MYSQL_READ_TIMEOUT,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            expression = f"TRIM(CAST(`{column}` AS CHAR))"
            normalized_expression = _normalized_catalog_sql_expression(expression)
            subsequence_pattern = "%" + "%".join(normalized_value) + "%"
            cur.execute(
                f"SELECT DISTINCT {expression} FROM `{table}` "
                f"WHERE `{column}` IS NOT NULL AND {expression} <> '' "
                f"AND ({normalized_expression} = %s "
                f"OR {normalized_expression} LIKE CONCAT('%%', %s, '%%') "
                f"OR %s LIKE CONCAT('%%', {normalized_expression}, '%%') "
                f"OR {normalized_expression} LIKE %s) "
                f"ORDER BY ({normalized_expression} = %s) DESC, CHAR_LENGTH({expression}) "
                "LIMIT 3",
                (
                    normalized_value,
                    normalized_value,
                    normalized_value,
                    subsequence_pattern,
                    normalized_value,
                ),
            )
            return [str(row[0]).strip() for row in cur.fetchall() if row and row[0]]
    finally:
        conn.close()


def resolve_entity_attribute_catalog_matches(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    candidates: list[dict] | tuple[dict, ...],
    value: str,
) -> list[dict]:
    """Resolve a mention to source-backed canonical values on authorized fields.

    Exact matches are distinguished from unique canonical values containing the
    mention.  The caller remains responsible for rejecting equal-rank multiple
    matches; this function never guesses among them.
    """
    mention = normalize_catalog_text(value)
    if len(mention) < 2 or len(mention) > 100:
        return []
    normalized, rows = _authorized_entity_field_rows(
        semantic_model_id,
        business_domain_id,
        candidates,
        require_unique_main=False,
        max_candidates=32,
    )
    approved = set(normalized)
    results: list[dict] = []
    checked: set[tuple[int, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("entity_code") or ""),
            row.get("business_domain_id"),
            str(row.get("mapping_table") or ""),
            str(row.get("mapping_column") or ""),
        )
        if key not in approved:
            continue
        table, column = key[2], key[3]
        source_key = (int(row.get("data_source_id")), table, column)
        if source_key in checked:
            continue
        checked.add(source_key)
        for canonical_value in _data_source_catalog_matches(
            row, table, column, mention
        ):
            normalized_canonical = normalize_catalog_text(canonical_value)
            canonical_iter = iter(normalized_canonical.casefold())
            ordered_subsequence = all(
                character in canonical_iter for character in mention.casefold()
            )
            if normalized_canonical.casefold() == mention.casefold():
                match_type = "EXACT"
            elif mention.casefold() in normalized_canonical.casefold():
                match_type = "CANONICAL_CONTAINS_MENTION"
            elif normalized_canonical.casefold() in mention.casefold():
                match_type = "MENTION_CONTAINS_CANONICAL"
            elif ordered_subsequence:
                match_type = "ORDERED_SUBSEQUENCE"
            else:
                continue
            results.append({
                "entity_code": key[0],
                "field": f"{table}.{column}",
                "canonical_value": canonical_value,
                "match_type": match_type,
                "is_main_attribute": bool(row.get("is_main_attribute")),
            })
    return results


def load_published_entity_attribute_candidates(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    *,
    limit: int = 512,
) -> list[dict]:
    """Return current published source fields eligible for entity grounding.

    Vector retrieval is useful for prompt construction but is not an authority
    boundary: a valid entity value may live on an attribute that did not enter
    top-k recall.  This metadata-only fallback enumerates active fields from the
    caller's semantic model/domain so value resolution can consult the current
    published catalogue without hard-coded business dictionaries.  As in the
    authorization path, model ownership comes from the attribute's entity and
    business domain; a stale redundant model id on the attribute itself is not
    allowed to hide a currently published field.
    """

    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        return []
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError("limit must be between 1 and 2000")
    domain_ids = _normalized_domain_ids(business_domain_id)
    params: list = [semantic_model_id]
    domain_clause = ""
    if domain_ids:
        placeholders = ", ".join(["%s"] * len(domain_ids))
        domain_clause = f" AND e.business_domain_id IN ({placeholders})"
        params.extend(domain_ids)
    params.append(limit)
    rows = _query(
        """
        SELECT DISTINCT
               e.code AS entity_code,
               e.business_domain_id,
               a.mapping_table,
               a.mapping_column,
               a.is_main_attribute
        FROM semantic_model m
        JOIN semantic_model_business_domain b
          ON b.semantic_model_id = m.id
         AND COALESCE(b.is_deleted, 0) = 0
        JOIN semantic_model_entity_type e
          ON e.business_domain_id = b.id
         AND (e.semantic_model_id = m.id OR e.semantic_model_id IS NULL)
         AND COALESCE(e.is_deleted, 0) = 0
         AND e.status = 1
        JOIN semantic_model_attribute_config a
          ON a.entity_type_id = e.id
         AND COALESCE(a.is_deleted, '0') = '0'
         AND a.mapping_table IS NOT NULL
         AND a.mapping_column IS NOT NULL
        JOIN semantic_model_data_source ds
          ON ds.id = e.data_source_id
         AND ds.semantic_model_id = m.id
         AND COALESCE(ds.is_deleted, 0) = 0
         AND ds.status = 1
        JOIN semantic_model_table t
          ON t.semantic_model_id = m.id
         AND t.data_source_id = ds.id
         AND t.name = a.mapping_table
         AND COALESCE(t.is_deleted, 0) = 0
        JOIN semantic_model_field f
          ON f.semantic_model_id = m.id
         AND f.data_source_id = ds.id
         AND f.table_id = t.id
         AND f.name = a.mapping_column
         AND COALESCE(f.is_deleted, 0) = 0
        WHERE m.id = %s
          AND COALESCE(m.is_deleted, 0) = 0
        """
        + domain_clause
        + " ORDER BY COALESCE(a.is_main_attribute, 0) DESC, e.code, "
          "a.mapping_table, a.mapping_column LIMIT %s",
        tuple(params),
    )
    return [
        {
            "entity_code": str(row.get("entity_code") or ""),
            "business_domain_id": row.get("business_domain_id"),
            "field": (
                f"{row.get('mapping_table')}.{row.get('mapping_column')}"
            ),
            "is_main_attribute": bool(row.get("is_main_attribute")),
        }
        for row in rows
        if row.get("entity_code")
        and row.get("mapping_table")
        and row.get("mapping_column")
    ]


def resolve_exact_entity_value_fields(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    candidates: list[dict] | tuple[dict, ...],
    value: str,
) -> list[str]:
    """Resolve a literal to the entity's unique active primary-name field."""
    return _resolve_exact_entity_fields(
        semantic_model_id,
        business_domain_id,
        candidates,
        value,
        require_unique_main=True,
        max_candidates=16,
    )


def resolve_exact_entity_attribute_value_fields(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    candidates: list[dict] | tuple[dict, ...],
    value: str,
) -> list[str]:
    """Resolve a literal against explicitly authorized entity attributes.

    Unlike :func:`resolve_exact_entity_value_fields`, this resolver is not
    limited to an entity's primary display name.  It is used for structured
    business attributes such as parent brand, standard name or country.  Every
    candidate still has to be an active attribute in the requested semantic
    model/domain and an active field of the same registered data source.  The
    physical lookup is exact and parameterized; callers must require one unique
    matched field before changing an ASL filter.
    """
    return _resolve_exact_entity_fields(
        semantic_model_id,
        business_domain_id,
        candidates,
        value,
        require_unique_main=False,
        max_candidates=32,
    )


def load_daily_vector_source(
    table_name: str,
    id_field: str,
    text_fields: tuple[str, ...] | list[str],
    metadata_fields: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """全量读取每日向量同步源表。

    表名和字段名暂时通过配置传入；数据端确定正式契约后无需修改 SQL 逻辑。
    本函数仅接受简单 MySQL 标识符，并对选择字段去重。
    """
    table = _validated_identifier(table_name, label="表名")
    primary_key = _validated_identifier(id_field, label="主键字段")
    requested = [primary_key, *text_fields, *metadata_fields]
    columns: list[str] = []
    for value in requested:
        column = _validated_identifier(value, label="字段名")
        if column not in columns:
            columns.append(column)
    if not text_fields:
        raise ValueError("DAILY_VECTOR_TEXT_FIELDS 至少需要一个向量化字段")

    quoted_columns = ", ".join(f"`{column}`" for column in columns)
    sql = f"SELECT {quoted_columns} FROM `{table}` ORDER BY `{primary_key}`"
    return list(_query(sql))


def load_entity_attribute_vector_source(
    semantic_model_id: int,
    business_domain_id: int,
) -> list[dict]:
    """读取指定语义模型/业务域的实体属性值向量源。

    同时校验业务域确实属于语义模型，避免仅凭两个独立 ID 造成跨作用域
    混用。该函数只读取已生效、未删除的行。
    """
    if not isinstance(semantic_model_id, int) or isinstance(semantic_model_id, bool) or semantic_model_id <= 0:
        raise ValueError("semantic_model_id 必须为正整数")
    if not isinstance(business_domain_id, int) or isinstance(business_domain_id, bool) or business_domain_id <= 0:
        raise ValueError("business_domain_id 必须为正整数")

    scope = _query(
        """
        SELECT b.id
        FROM semantic_model_business_domain b
        JOIN semantic_model m ON m.id=b.semantic_model_id
        WHERE b.id=%s AND b.semantic_model_id=%s
          AND COALESCE(b.is_deleted,0)=0 AND COALESCE(m.is_deleted,0)=0
        LIMIT 1
        """,
        (business_domain_id, semantic_model_id),
    )
    if not scope:
        raise ValueError("业务域不属于指定语义模型，或作用域已删除")

    return list(_query(
        """
        SELECT id, semantic_model_id, business_domain_id,
               entity_name, entity_alias, entity_description,
               attr_name, attr_code, attr_description, attr_value,
               create_time, update_time
        FROM semantic_model_entity_att
        WHERE semantic_model_id=%s AND business_domain_id=%s
          AND COALESCE(is_deleted,0)=0
        ORDER BY id
        """,
        (semantic_model_id, business_domain_id),
    ))


_UNICODE_DASH_TRANSLATION = str.maketrans({
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2212": "-",  # minus sign
    "\ufe58": "-",  # small em dash
    "\ufe63": "-",  # small hyphen-minus
    "\uff0d": "-",  # full-width hyphen-minus
})


def normalize_catalog_text(value: object) -> str:
    """Normalize presentation-equivalent punctuation for catalogue matching."""
    return str(value or "").strip().translate(_UNICODE_DASH_TRANSLATION)


def _normalized_catalog_sql_expression(expression: str) -> str:
    """Build a SQL expression equivalent to :func:`normalize_catalog_text`.

    ``expression`` is assembled only from already validated identifiers.  Values
    remain bound parameters; this helper never interpolates user input.
    """
    result = expression
    for source in ("‐", "‑", "‒", "–", "—", "−", "﹘", "﹣", "－"):
        result = f"REPLACE({result}, '{source}', '-')"
    return result


def entity_value_vectorization_decision(row: dict) -> tuple[bool, str]:
    """Return the governed vectorization decision and an auditable reason.

    ``is_main_attribute`` controls default presentation and is deliberately not
    a search-index switch.  A published attribute enters the semantic value
    index only when vectorization is explicitly enabled (or a future
    ``search_mode`` requests VECTOR/HYBRID).  Identifier-like fields are denied
    even if misconfigured, because similarity search is not an authoritative
    way to resolve business keys.
    """
    code = str(row.get("attr_code") or "").strip().casefold()
    name = str(row.get("attr_name") or "").strip()
    data_type = str(row.get("data_type") or "").casefold()
    mapping_column = str(row.get("mapping_column") or "").strip().casefold()
    semantic_role = str(
        row.get("attribute_role")
        or row.get("semantic_role")
        or ""
    ).strip().casefold()
    search_mode = str(row.get("search_mode") or "").strip().casefold()
    if not code or code == "id":
        return False, "MISSING_OR_GENERIC_ID"
    if (
        bool(row.get("is_identifier"))
        or bool(row.get("is_business_key"))
        or bool(row.get("exact_match_only"))
        or semantic_role in {
            "identifier", "business_key", "primary_key", "foreign_key",
        }
        or code.endswith(("_id", "_code", "_key"))
        or mapping_column.endswith(("_id", "_code", "_key"))
        or re.search(r"(?:ID|编码|编号|标识|订单号|单据号|流水号|序列号)$", name, re.I)
    ):
        return False, "IDENTIFIER_EXACT_ONLY"
    if any(token in data_type for token in (
        "int", "decimal", "float", "double", "date", "time", "bool",
    )):
        return False, "NON_TEXT_ATTRIBUTE"
    if search_mode:
        if search_mode in {"vector", "hybrid"}:
            return True, f"SEARCH_MODE_{search_mode.upper()}"
        return False, f"SEARCH_MODE_{search_mode.upper()}"
    if bool(row.get("vectorization")):
        return True, "EXPLICIT_VECTORIZATION"
    return False, "VECTORIZATION_DISABLED"


def _should_vectorize_entity_value(row: dict) -> bool:
    """Compatibility boolean wrapper for the governed policy decision."""

    return entity_value_vectorization_decision(row)[0]


def _entity_attribute_vector_definitions(
    semantic_model_id: int,
    business_domain_id: int,
) -> list[dict]:
    """Load active, physically valid attribute definitions for one scope."""

    return list(_query(
        """
        SELECT DISTINCT e.code AS entity_code, e.name AS entity_name,
               e.alias AS entity_alias, e.description AS entity_description,
               e.business_domain_id, e.data_source_id,
               a.code AS attr_code, a.attr_name, a.description AS attr_description,
               COALESCE(NULLIF(a.data_type, ''), f.type) AS data_type,
               a.mapping_table, a.mapping_column, a.vectorization,
               COALESCE(a.is_main_attribute, 0) AS is_main_attribute,
               ds.db_type, ds.host, ds.port, ds.username, ds.password, ds.db_name
        FROM semantic_model_entity_type e
        JOIN semantic_model_business_domain b
          ON b.id=e.business_domain_id AND b.semantic_model_id=%s
         AND COALESCE(b.is_deleted,0)=0
        JOIN semantic_model_attribute_config a
          ON a.entity_type_id=e.id AND COALESCE(a.is_deleted,'0')='0'
        JOIN semantic_model_data_source ds
          ON ds.id=e.data_source_id AND ds.semantic_model_id=%s
         AND COALESCE(ds.is_deleted,0)=0 AND ds.status=1
        JOIN semantic_model_table t
          ON t.semantic_model_id=%s AND t.data_source_id=ds.id
         AND t.name=a.mapping_table AND COALESCE(t.is_deleted,0)=0
        JOIN semantic_model_field f
          ON f.semantic_model_id=%s AND f.data_source_id=ds.id
         AND f.table_id=t.id AND f.name=a.mapping_column
         AND COALESCE(f.is_deleted,0)=0
        WHERE e.business_domain_id=%s
          AND COALESCE(e.is_deleted,0)=0 AND e.status=1
          AND e.code IS NOT NULL AND TRIM(e.code) <> ''
        ORDER BY e.code, a.code
        """,
        (
            semantic_model_id, semantic_model_id, semantic_model_id,
            semantic_model_id, business_domain_id,
        ),
    ))


def load_entity_attribute_vector_policy_audit(
    semantic_model_id: int,
    business_domain_id: int,
) -> dict:
    """Describe included/excluded attributes without exposing credentials."""

    definitions = _entity_attribute_vector_definitions(
        semantic_model_id, business_domain_id
    )
    included: list[dict] = []
    excluded: list[dict] = []
    for definition in definitions:
        enabled, reason = entity_value_vectorization_decision(definition)
        item = {
            "entity_code": str(definition.get("entity_code") or ""),
            "entity_name": str(definition.get("entity_name") or ""),
            "attr_code": str(definition.get("attr_code") or ""),
            "attr_name": str(definition.get("attr_name") or ""),
            "mapping_table": str(definition.get("mapping_table") or ""),
            "mapping_column": str(definition.get("mapping_column") or ""),
            "vectorization": bool(definition.get("vectorization")),
            "is_main_attribute": bool(definition.get("is_main_attribute")),
            "reason": reason,
        }
        (included if enabled else excluded).append(item)
    return {
        "included_attributes": included,
        "excluded_attributes": excluded,
    }


def load_complete_entity_attribute_vector_source(
    semantic_model_id: int,
    business_domain_id: int,
    *,
    per_attribute_limit: int = 100000,
) -> list[dict]:
    """Build a complete value snapshot from published physical mappings.

    ``semantic_model_entity_att`` is retained as a compatibility/audit source,
    but it is populated asynchronously by another service and can represent a
    partial single-attribute batch.  The authoritative rebuild therefore reads
    every eligible, published attribute directly from its governed data source.
    Table and column identifiers are accepted only after semantic/physical
    metadata joins and strict identifier validation.
    """
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        raise ValueError("semantic_model_id must be a positive integer")
    if type(business_domain_id) is not int or business_domain_id <= 0:
        raise ValueError("business_domain_id must be a positive integer")
    if type(per_attribute_limit) is not int or per_attribute_limit <= 0:
        raise ValueError("per_attribute_limit must be positive")

    definitions = _entity_attribute_vector_definitions(
        semantic_model_id, business_domain_id
    )
    definitions = [row for row in definitions if _should_vectorize_entity_value(row)]
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for definition in definitions:
        db_type = re.sub(r"[^a-z]", "", str(definition.get("db_type") or "").casefold())
        if db_type not in {"mysql", "mariadb"}:
            continue
        table = _validated_identifier(str(definition["mapping_table"]), label="table name")
        column = _validated_identifier(str(definition["mapping_column"]), label="column name")
        conn = pymysql.connect(
            host=str(definition.get("host") or "").strip(),
            port=int(definition.get("port")),
            user=str(definition.get("username") or "").strip(),
            password=definition.get("password") or "",
            database=str(definition.get("db_name") or "").strip(),
            charset=MYSQL_CHARSET,
            connect_timeout=MYSQL_CONNECT_TIMEOUT,
            read_timeout=MYSQL_READ_TIMEOUT,
            write_timeout=MYSQL_READ_TIMEOUT,
            autocommit=True,
        )
        try:
            with conn.cursor() as cursor:
                expression = f"TRIM(CAST(`{column}` AS CHAR))"
                cursor.execute(
                    f"SELECT DISTINCT {expression} AS canonical_value "
                    f"FROM `{table}` WHERE `{column}` IS NOT NULL "
                    f"AND {expression} <> '' ORDER BY canonical_value LIMIT %s",
                    (per_attribute_limit,),
                )
                values = [str(row[0]).strip() for row in cursor.fetchall() if row and row[0]]
        finally:
            conn.close()
        for value in values:
            normalized_value = normalize_catalog_text(value)
            identity = (
                str(definition["entity_code"]),
                str(definition["attr_code"]),
                normalized_value.casefold(),
            )
            if identity in seen:
                continue
            seen.add(identity)
            digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()
            result.append({
                "id": digest,
                "semantic_model_id": semantic_model_id,
                "business_domain_id": business_domain_id,
                "data_source_id": int(definition["data_source_id"]),
                "entity_code": definition["entity_code"],
                "entity_name": definition["entity_name"],
                "entity_alias": definition.get("entity_alias"),
                "entity_description": definition.get("entity_description"),
                "attr_code": definition["attr_code"],
                "attr_name": definition["attr_name"],
                "attr_description": definition.get("attr_description"),
                "attr_value": value,
                "source_table": table,
                "source_field": column,
            })
    return result


@contextmanager
def mysql_advisory_lock(lock_name: str, timeout_seconds: int = 0):
    """使用 MySQL GET_LOCK 实现跨进程同步互斥。"""
    if not isinstance(lock_name, str) or not lock_name or len(lock_name) > 64:
        raise ValueError("MySQL advisory lock 名称必须为1到64个字符")
    conn = _get_connection()
    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_seconds))
            row = cur.fetchone()
            acquired = bool(row and row[0] == 1)
        if not acquired:
            raise RuntimeError("同一资源正在由其他服务实例更新")
        yield
    finally:
        if acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                # 连接关闭时 MySQL 也会自动释放该会话锁。
                pass
        conn.close()


# ==================== 语义建模 / 业务域 ====================

def get_semantic_models():
    """获取所有语义建模（未删除）。"""
    sql = """
        SELECT id, project_id, name, code, description, icon
        FROM semantic_model
        WHERE is_deleted = 0
        ORDER BY id
    """
    return _query(sql)


def get_business_domains(semantic_model_id: int):
    """获取指定语义建模下的所有业务域。"""
    sql = """
        SELECT id, semantic_model_id, project_id, name, code, description, icon
        FROM semantic_model_business_domain
        WHERE is_deleted = 0 AND semantic_model_id = %s
        ORDER BY id
    """
    return _query(sql, (semantic_model_id,))


# ==================== 实体相关查询 ====================

def _row_to_entity_dict(row):
    """将单行 semantic_model_entity_type 记录转换为目标 JSON 结构。"""
    return {
        "entity_id": row.get("id"),
        "entity_code": row.get("code"),
        "entity_name": row.get("name"),
        "entity_alias": _parse_json(row.get("alias")) or [],
        "business_domain": row.get("business_domain_id"),
        "update_frequency": row.get("update_frequency"),
        "primary_key": None,
        "business_definition": {
            "description": row.get("description"),
        },
        "attributes": [],
        "relations": [],
        "bind_assets": {
            "bind_metrics": [],
            "bind_dimensions": [],
        },
    }


def _row_to_attr_dict(row):
    """将单行 semantic_model_attribute_config 记录转换为目标 JSON 结构。"""
    mapping_table = row.get("mapping_table")
    mapping_column = row.get("mapping_column")
    # 拼接为 表名.字段名 格式（如存在）
    if mapping_table and mapping_column:
        field_mapping = f"{mapping_table}.{mapping_column}"
    elif mapping_column:
        field_mapping = mapping_column
    else:
        field_mapping = None

    return {
        "attribute_id": row.get("id"),
        "attr_code": row.get("code"),
        "attr_name": row.get("attr_name"),
        "data_type": row.get("data_type"),
        "field_mapping": field_mapping,
        "description": row.get("description"),
        "is_main_attribute": bool(row.get("is_main_attribute")),
        "is_primary_key": bool(row.get("is_primary_key")),
        "is_unique": bool(row.get("is_unique")),
        "is_nullable": not bool(row.get("is_required")),
        "enum_values": [],
    }


def _row_to_relation_dict(row):
    """将单行 semantic_model_relation_config 记录转换为目标 JSON 结构。"""
    def physical_field(side: str):
        value = row.get(f"{side}_table_column_name")
        if isinstance(value, str) and value:
            # Some historical rows use "table-column" while newer rows use
            # "table.column". Normalize the former only once.
            if "." not in value and "-" in value:
                value = value.replace("-", ".", 1)
            return value
        return row.get(f"{side}_field") or row.get(f"{side}_field_name")

    return {
        "relation_code": row.get("code"),
        "relation_name": row.get("name"),
        "target_entity": row.get("target_entity_code") or row.get("target_entity_type_id"),
        "relation_semantic": row.get("description"),
        "relation_constraint": None,
        "join_key": {
            "source_field": physical_field("source"),
            "target_field": physical_field("target"),
        },
        "description": row.get("description"),
    }


def _row_to_bind_metric_dict(row):
    """将单行 semantic_model_entity_bind_indicator 记录转换为目标 JSON 结构。"""
    return {
        "metric_code": row.get("indicator_code"),
        # The binding table keeps a denormalized name for historical UI
        # compatibility.  It may lag behind a renamed indicator, so the
        # current indicator row is authoritative whenever it is available.
        "metric_name": row.get("canonical_indicator_name") or row.get("indicator_name"),
        "metric_logic": row.get("indicator_logic"),
    }


def _is_newer(row: dict, existing: dict) -> bool:
    """判断 row 是否比 existing 更新（基于 update_time/create_time，None 视为最旧）。"""
    def _ts(r):
        return r.get("update_time") or r.get("create_time")

    row_ts, exist_ts = _ts(row), _ts(existing)
    if row_ts is None:
        return False
    if exist_ts is None:
        return True
    return row_ts > exist_ts


def get_entity(business_domain_id: int):
    """获取指定业务域下的实体信息（聚合实体类型、属性、关系、绑定指标）。

    Args:
        business_domain_id: 业务域 ID（semantic_model_business_domain.id）
    """
    sql_entity = """
        SELECT id, code, name, description, business_domain_id,
               alias, update_frequency, main_table_name,
               update_time, create_time
        FROM semantic_model_entity_type
        WHERE is_deleted = 0 AND business_domain_id = %s
    """
    sql_attr = """
        SELECT id, entity_type_id, code, attr_name, description,
               mapping_table, mapping_column, data_type, is_main_attribute, is_primary_key,
               is_unique, is_required, prefix, suffix,
               update_time, create_time
        FROM semantic_model_attribute_config
        WHERE (is_deleted IS NULL OR is_deleted = 0)
          AND entity_type_id IN (
              SELECT id FROM semantic_model_entity_type
              WHERE is_deleted = 0 AND business_domain_id = %s
          )
    """
    sql_relation = """
        SELECT r.id, r.name, r.description,
               r.source_entity_type_id, r.target_entity_type_id,
               r.source_field, r.target_field,
               r.source_field_name, r.target_field_name,
               r.source_table_column_name, r.target_table_column_name,
               r.code, r.type, r.update_time, r.create_time
        FROM semantic_model_relation_config r
        WHERE r.is_deleted = 0
          AND r.source_entity_type_id IN (
              SELECT id FROM semantic_model_entity_type
              WHERE is_deleted = 0 AND business_domain_id = %s
          )
    """
    sql_bind_metric = """
        SELECT e.code AS entity_code, bi.indicator_code, bi.indicator_name,
               i.indicator_name AS canonical_indicator_name,
               bi.indicator_logic, bi.update_time, bi.create_time
        FROM semantic_model_entity_bind_indicator bi
        JOIN semantic_model_business_domain bd
          ON bd.id = %s
         AND COALESCE(bd.is_deleted, 0) = 0
         AND bd.semantic_model_id = bi.semantic_model_id
        JOIN semantic_model_entity_type e
          ON e.business_domain_id = bd.id
         AND COALESCE(e.is_deleted, 0) = 0
         AND e.status = 1
         AND (
              e.code = bi.entity_code
              OR CAST(e.id AS CHAR) = bi.entity_code
         )
        JOIN semantic_model_indicator i
          ON i.semantic_model_id = bi.semantic_model_id
         AND i.business_domain_id = bd.id
         AND i.indicator_code = bi.indicator_code
         AND COALESCE(i.is_deleted, 0) = 0
        WHERE COALESCE(bi.is_deleted, 0) = 0
    """

    try:
        conn = _get_connection()
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(sql_entity, (business_domain_id,))
                entity_rows = cur.fetchall()

                cur.execute(sql_attr, (business_domain_id,))
                attr_rows = cur.fetchall()

                cur.execute(sql_relation, (business_domain_id,))
                relation_rows = cur.fetchall()

                cur.execute(sql_bind_metric, (business_domain_id,))
                bind_metric_rows = cur.fetchall()
        finally:
            conn.close()

        # Versioned metadata tables can contain repeated physical rows with the
        # same semantic ID. Collapse them before assembling the graph. A bridge
        # entity with a missing code receives its validated governed table name
        # as deterministic code; dangling relations to deleted entities are
        # removed below instead of leaking stale paths into the vector snapshot.
        entity_map: dict[object, dict] = {}
        for raw_row in entity_rows:
            row = dict(raw_row)
            entity_id = row.get("id")
            existing = entity_map.get(entity_id)
            if existing is None or _is_newer(row, existing):
                entity_map[entity_id] = row
        for row in entity_map.values():
            if not str(row.get("code") or "").strip():
                table_code = str(row.get("main_table_name") or "").strip()
                if _SAFE_IDENTIFIER.fullmatch(table_code):
                    row["code"] = table_code
        entity_code_by_id = {
            entity_id: str(row.get("code") or "").strip()
            for entity_id, row in entity_map.items()
            if str(row.get("code") or "").strip()
        }

        attr_map = {}
        for row in attr_rows:
            entity_type_id = row.get("entity_type_id")
            attr_map.setdefault(entity_type_id, {})
            code = row.get("code") or row.get("id")
            existing = attr_map[entity_type_id].get(code)
            if existing is None:
                attr_map[entity_type_id][code] = row
            else:
                if _is_newer(row, existing):
                    attr_map[entity_type_id][code] = row

        relation_map = {}
        for row in relation_rows:
            source_entity_type_id = row.get("source_entity_type_id")
            target_entity_type_id = row.get("target_entity_type_id")
            if (
                source_entity_type_id not in entity_code_by_id
                or target_entity_type_id not in entity_code_by_id
            ):
                continue
            row = dict(row)
            row["target_entity_code"] = entity_code_by_id[target_entity_type_id]
            relation_map.setdefault(source_entity_type_id, {})
            code = row.get("code") or row.get("id")
            existing = relation_map[source_entity_type_id].get(code)
            if existing is None:
                relation_map[source_entity_type_id][code] = row
            else:
                if _is_newer(row, existing):
                    relation_map[source_entity_type_id][code] = row

        bind_metric_map = {}
        for row in bind_metric_rows:
            entity_code = row.get("entity_code")
            bind_metric_map.setdefault(entity_code, {})
            ind_code = row.get("indicator_code") or row.get("id")
            existing = bind_metric_map[entity_code].get(ind_code)
            if existing is None:
                bind_metric_map[entity_code][ind_code] = row
            else:
                if _is_newer(row, existing):
                    bind_metric_map[entity_code][ind_code] = row

        result = []
        for row in entity_map.values():
            if row.get("id") not in entity_code_by_id:
                continue
            entity = _row_to_entity_dict(row)
            entity_id = row.get("id")
            entity_code = row.get("code")

            attr_dict = attr_map.get(entity_id, {})
            rel_dict = relation_map.get(entity_id, {})
            bm_dict = bind_metric_map.get(entity_code, {})

            entity["attributes"] = [_row_to_attr_dict(a) for a in attr_dict.values()]
            entity["relations"] = [_row_to_relation_dict(r) for r in rel_dict.values()]
            entity["bind_assets"]["bind_metrics"] = [
                _row_to_bind_metric_dict(m) for m in bm_dict.values()
            ]

            result.append(entity)

        return result
    except Exception:
        raise


def get_registered_entity_attributes(
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
) -> list[dict]:
    """Return active entity attributes from the current semantic registry.

    Vector recall is intentionally bounded and can omit a required name field
    when a long literal dominates the query.  Intent-contract repair uses this
    small metadata-only view as a fail-closed fallback.  Rows remain isolated
    by semantic model and optional business domains; no source values or
    credentials are returned.
    """
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        return []
    domain_ids = _normalized_domain_ids(business_domain_id)
    clauses = [
        "b.semantic_model_id=%s",
        "COALESCE(b.is_deleted,0)=0",
        "COALESCE(e.is_deleted,0)=0",
        "e.status=1",
        "COALESCE(a.is_deleted,0)=0",
    ]
    params: list[object] = [semantic_model_id]
    if domain_ids:
        clauses.append(
            "e.business_domain_id IN ("
            + ",".join(["%s"] * len(domain_ids))
            + ")"
        )
        params.extend(domain_ids)
    rows = _query(
        """
        SELECT e.code AS entity_code, e.name AS entity_name,
               e.alias AS entity_alias, e.business_domain_id,
               e.data_source_id, a.code AS attr_code,
               a.attr_name, a.description, a.mapping_table,
               a.mapping_column, a.is_main_attribute,
               a.is_primary_key, a.is_unique
        FROM semantic_model_entity_type e
        JOIN semantic_model_business_domain b
          ON b.id=e.business_domain_id
        JOIN semantic_model_attribute_config a
          ON a.entity_type_id=e.id
        WHERE """
        + " AND ".join(clauses)
        + " ORDER BY e.business_domain_id, e.code, a.id",
        tuple(params),
    )
    result: list[dict] = []
    for row in rows:
        table = str(row.get("mapping_table") or "").strip()
        column = str(row.get("mapping_column") or "").strip()
        if not table or not column:
            continue
        result.append({
            "entity_code": row.get("entity_code"),
            "entity_name": row.get("entity_name"),
            "entity_alias": _parse_json(row.get("entity_alias")) or [],
            "business_domain_id": row.get("business_domain_id"),
            "data_source_id": row.get("data_source_id"),
            "attr_code": row.get("attr_code"),
            "attr_name": row.get("attr_name"),
            "description": row.get("description"),
            "field_mapping": f"{table}.{column}",
            "is_main_attribute": bool(row.get("is_main_attribute")),
            "is_primary_key": bool(row.get("is_primary_key")),
            "is_unique": bool(row.get("is_unique")),
        })
    return result


# ==================== 指标相关查询 ====================

def _row_to_metric_dict(row):
    """将单行 semantic_model_indicator 记录转换为目标 JSON 结构。"""
    return {
        "metric_code": row.get("indicator_code"),
        "metric_name": row.get("indicator_name"),
        "synonyms": _parse_json(row.get("synonyms")) or [],
        "business_domain": row.get("business_domain_id"),
        "metric_level": row.get("indicator_level"),
        "unit": row.get("unit"),
        "format_rule": row.get("format_rule"),
        "is_public": True,
        "business_definition": {
            "description": row.get("business_desc"),
            "application_scenes": _parse_json(row.get("applicable_scenarios")) or [],
        },
        "time_caliber": {
            "stat_cycle": None,
            "time_anchor": None,
            "special_rule": None,
        },
        "calculation_rule": {
            "calc_formula": row.get("calculation_formula"),
            "global_filters": _parse_json(row.get("global_filters")) or [],
        },
        "bind_dimensions": [],
        "source_dependency": {
            "bind_entity": [],
        },
        "permission_config": {
            "view_roles": [],
            "data_limit": None,
        },
    }


def get_metric(semantic_model_id: int, business_domain_id: int = None):
    """获取指标信息，按语义建模+业务域隔离。

    Args:
        semantic_model_id: 语义建模 ID（必填）
        business_domain_id: 业务域 ID（可选，None 表示取该语义建模下全部指标）
    """
    if business_domain_id is not None:
        sql = """
            SELECT id, project_id, semantic_model_id, indicator_code, business_domain_id,
                   indicator_name, indicator_level, unit, format_rule, business_desc,
                   synonyms, applicable_scenarios, dependence_atomic_indicator,
                   calculation_formula, global_filters, indicator_logic
            FROM semantic_model_indicator
            WHERE is_deleted = 0 AND semantic_model_id = %s AND business_domain_id = %s
        """
        args = (semantic_model_id, business_domain_id)
    else:
        sql = """
            SELECT id, project_id, semantic_model_id, indicator_code, business_domain_id,
                   indicator_name, indicator_level, unit, format_rule, business_desc,
                   synonyms, applicable_scenarios, dependence_atomic_indicator,
                   calculation_formula, global_filters, indicator_logic
            FROM semantic_model_indicator
            WHERE is_deleted = 0 AND semantic_model_id = %s
        """
        args = (semantic_model_id,)

    try:
        rows = _query(sql, args)
        # Entity rows are scoped through business domains because historical
        # semantic_model_entity_type rows may not populate semantic_model_id.
        entity_rows = _query(
            """
            SELECT e.code, e.main_table_name, e.business_domain_id
            FROM semantic_model_entity_type e
            JOIN semantic_model_business_domain b
              ON b.id=e.business_domain_id
             AND COALESCE(b.is_deleted,0)=0
            WHERE b.semantic_model_id=%s
              AND COALESCE(e.is_deleted,0)=0
              AND e.status=1
              AND e.code IS NOT NULL AND e.code<>''
              AND e.main_table_name IS NOT NULL AND e.main_table_name<>''
            """,
            (semantic_model_id,),
        )
        table_entities: dict[str, list[str]] = {}
        valid_entity_codes: set[str] = set()
        for entity in entity_rows:
            code = str(entity["code"])
            table = str(entity["main_table_name"])
            valid_entity_codes.add(code)
            table_entities.setdefault(table, [])
            if code not in table_entities[table]:
                table_entities[table].append(code)

        binding_rows = _query(
            """
            SELECT bi.indicator_code, e.code AS entity_code,
                   e.business_domain_id
            FROM semantic_model_entity_bind_indicator bi
            JOIN semantic_model_business_domain b
              ON b.semantic_model_id=bi.semantic_model_id
             AND COALESCE(b.is_deleted,0)=0
            JOIN semantic_model_indicator i
              ON i.semantic_model_id=bi.semantic_model_id
             AND i.business_domain_id=b.id
             AND i.indicator_code=bi.indicator_code
             AND COALESCE(i.is_deleted,0)=0
            JOIN semantic_model_entity_type e
              ON e.business_domain_id=b.id
             AND COALESCE(e.is_deleted,0)=0
             AND e.status=1
             AND (
                  e.code=bi.entity_code
                  OR CAST(e.id AS CHAR)=bi.entity_code
             )
            WHERE COALESCE(bi.is_deleted,0)=0
              AND b.semantic_model_id=%s
            """,
            (semantic_model_id,),
        )
        metric_entities: dict[tuple[int, str], list[str]] = {}
        for binding in binding_rows:
            metric_code = str(binding.get("indicator_code") or "")
            entity_code = str(binding.get("entity_code") or "")
            binding_domain_id = int(binding.get("business_domain_id") or 0)
            if (
                not metric_code
                or binding_domain_id <= 0
                or entity_code not in valid_entity_codes
            ):
                continue
            binding_key = (binding_domain_id, metric_code)
            metric_entities.setdefault(binding_key, [])
            if entity_code not in metric_entities[binding_key]:
                metric_entities[binding_key].append(entity_code)

        dimension_rows = _query(
            """
            SELECT dim_code, dim_type, date_list, entity_attribute, indicator
            FROM semantic_model_dimension
            WHERE is_deleted=0 AND semantic_model_id=%s
            """,
            (semantic_model_id,),
        )
        dimensions_by_metric: dict[str, list[dict]] = {}
        for dimension in dimension_rows:
            indicators = _parse_json(dimension.get("indicator")) or []
            if isinstance(indicators, str):
                indicators = [item.strip() for item in indicators.split(",") if item.strip()]
            for metric_code in indicators if isinstance(indicators, list) else []:
                dimensions_by_metric.setdefault(str(metric_code), []).append(dimension)

        result = []
        for row in rows:
            metric = _row_to_metric_dict(row)
            metric_code = str(metric.get("metric_code") or "")
            formula = str(row.get("calculation_formula") or "")
            formula_tables = list(dict.fromkeys(re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\b",
                formula,
            )))

            metric_domain_id = int(row.get("business_domain_id") or 0)
            bound_entities = list(metric_entities.get(
                (metric_domain_id, metric_code), []
            ))
            for table in formula_tables:
                for entity_code in table_entities.get(table, []):
                    if entity_code not in bound_entities:
                        bound_entities.append(entity_code)
            metric["source_dependency"]["bind_entity"] = bound_entities

            bound_dimensions = []
            time_anchors: list[str] = []
            for dimension in dimensions_by_metric.get(metric_code, []):
                dim_code = dimension.get("dim_code")
                if dim_code and dim_code not in bound_dimensions:
                    bound_dimensions.append(dim_code)
                dim_type = str(dimension.get("dim_type") or "").lower()
                date_list = _parse_json(dimension.get("date_list")) or []
                if not ("time" in dim_type or "date" in dim_type or "时间" in dim_type or date_list):
                    continue
                bindings = _parse_json(dimension.get("entity_attribute")) or []
                for binding in bindings if isinstance(bindings, list) else []:
                    if not isinstance(binding, dict):
                        continue
                    table = str(binding.get("mappingTable") or "")
                    column = str(binding.get("mappingColumn") or "")
                    if table in formula_tables and table and column:
                        anchor = f"{table}.{column}"
                        if anchor not in time_anchors:
                            time_anchors.append(anchor)
            metric["bind_dimensions"] = bound_dimensions
            if len(time_anchors) == 1:
                metric["time_caliber"]["time_anchor"] = time_anchors[0]
            result.append(metric)
        return result
    except Exception:
        raise


def get_metric_evidence(
    semantic_model_id: int,
    metric_codes: list[str] | tuple[str, ...],
    business_domain_ids: list[int] | tuple[int, ...] | None = None,
) -> list[dict]:
    """Read authoritative metric identity/formula rows for query evidence.

    This query is intentionally small and targeted: it runs only after the ASL
    validator has selected canonical metric codes from the vector snapshot.
    """
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        raise ValueError("semantic_model_id must be a positive integer")
    codes = list(dict.fromkeys(metric_codes or []))
    if any(not isinstance(code, str) or not code.strip() for code in codes):
        raise ValueError("metric_codes must contain non-empty strings")
    if not codes:
        return []
    from scope_contract import normalize_domains
    domains = normalize_domains(business_domain_ids=business_domain_ids)

    code_placeholders = ", ".join(["%s"] * len(codes))
    clauses = [
        "COALESCE(is_deleted, 0) = 0",
        "semantic_model_id = %s",
        f"indicator_code IN ({code_placeholders})",
    ]
    args: list = [semantic_model_id, *codes]
    if domains:
        domain_placeholders = ", ".join(["%s"] * len(domains))
        clauses.append(f"business_domain_id IN ({domain_placeholders})")
        args.extend(domains)

    return list(_query(
        f"""
        SELECT id, semantic_model_id, business_domain_id,
               indicator_code, indicator_name, calculation_formula,
               indicator_logic, business_desc, synonyms, global_filters,
               unit, format_rule
        FROM semantic_model_indicator
        WHERE {' AND '.join(clauses)}
        ORDER BY indicator_code, id
        """,
        tuple(args),
    ))


# ==================== 维度相关查询 ====================

def _row_to_dimension_dict(row):
    """将单行 semantic_model_dimension 记录转换为目标 JSON 结构。"""
    return {
        "dim_code": row.get("dim_code"),
        "dim_name": row.get("dim_name"),
        "synonyms": _parse_json(row.get("synonyms")) or [],
        "business_definition": {
            "description": row.get("dim_description"),
            "application_scenes": [],
        },
        "dim_type": row.get("dim_type"),
        "dim_hierarchy": _parse_json(row.get("level_list")),
        "granularity_support": _parse_json(row.get("date_list")) or [],
        "enum_list": _parse_json(row.get("enum_list")) or [],
        "special_rules": _parse_json(row.get("special_rules")) or [],
        "field_mapping": {},
        "bind_entities": _parse_json(row.get("entity_attribute")) or [],
        "bind_metrics": _parse_json(row.get("indicator")) or [],
    }


def get_dimension(semantic_model_id: int):
    """获取维度信息，按语义建模隔离（维度无 business_domain_id，跨业务域共享）。

    Args:
        semantic_model_id: 语义建模 ID
    """
    sql = """
        SELECT id, project_id, semantic_model_id, dim_code, dim_name,
               synonyms, dim_description, dim_type, special_rules,
               enum_list, level_list, date_list, entity_attribute, indicator
        FROM semantic_model_dimension
        WHERE is_deleted = 0 AND semantic_model_id = %s
    """
    try:
        rows = _query(sql, (semantic_model_id,))
        return [_row_to_dimension_dict(row) for row in rows]
    except Exception:
        raise


# ==================== 枚举相关查询 ====================

def _row_to_enum_dict(row):
    """将单行 semantic_model_enum 记录转换为目标 JSON 结构。"""
    return {
        "code": row.get("value"),
        "name": row.get("name"),
        "value": row.get("value"),
    }


def get_enum():
    """获取枚举信息（暂未按业务域隔离，保留接口）。"""
    sql = """
        SELECT id, field_id, name, value
        FROM semantic_model_enum
        WHERE is_deleted = 0
    """
    try:
        rows = _query(sql)
        return [_row_to_enum_dict(row) for row in rows]
    except Exception:
        raise


# ==================== 聚合查询（按作用域） ====================

def get_dsl_by_scope(semantic_model_id: int, business_domain_id: int = None) -> dict:
    """按语义建模 + 业务域作用域聚合 DSL 数据。

    数据隔离规则:
      - 实体：按 business_domain_id 过滤（必填）
      - 指标：按 semantic_model_id + business_domain_id 过滤
      - 维度：按 semantic_model_id 过滤（跨业务域共享）

    Args:
        semantic_model_id: 语义建模 ID（必填）
        business_domain_id: 业务域 ID（实体必填；为 None 时实体返回空、指标返回该语义建模下全部）

    Returns:
        {
            "semantic_model": {...},
            "business_domain": {...} | None,
            "entities": [...],
            "metrics": [...],
            "dimensions": [...],
        }
    """
    # 1. 校验语义建模存在
    sm_rows = _query(
        "SELECT id, name, code, description FROM semantic_model WHERE is_deleted = 0 AND id = %s",
        (semantic_model_id,),
    )
    if not sm_rows:
        raise ValueError(f"semantic_model id={semantic_model_id} 不存在或已删除")
    sm = sm_rows[0]

    # 2. 校验业务域（若指定）
    bd = None
    if business_domain_id is not None:
        bd_rows = _query(
            """SELECT id, semantic_model_id, name, code, description
               FROM semantic_model_business_domain
               WHERE is_deleted = 0 AND id = %s AND semantic_model_id = %s""",
            (business_domain_id, semantic_model_id),
        )
        if not bd_rows:
            raise ValueError(
                f"business_domain id={business_domain_id} 不存在或不属于 semantic_model id={semantic_model_id}"
            )
        bd = bd_rows[0]

    # 3. 拉取业务数据
    entities = get_entity(business_domain_id) if business_domain_id is not None else []
    metrics = get_metric(semantic_model_id, business_domain_id)
    dimensions = get_dimension(semantic_model_id)

    return {
        "semantic_model": {
            "id": sm["id"],
            "name": sm["name"],
            "code": sm["code"],
            "description": sm["description"],
        },
        "business_domain": (
            {
                "id": bd["id"],
                "name": bd["name"],
                "code": bd["code"],
                "description": bd["description"],
            }
            if bd
            else None
        ),
        "entities": entities,
        "metrics": metrics,
        "dimensions": dimensions,
    }


# ==================== 物理表 / 字段相关查询 ====================
#
# 作用域维度：data_source_id（数据源隔离）；semantic_model_id 可空，仅作为附加过滤。
# 与业务域 DSL 不同，物理表/字段没有 business_domain_id 维度。

def _row_to_table_dict(row):
    """将单行 semantic_model_table 记录转换为目标 JSON 结构。

    字段挂载到 `fields` 列表，由调用方填充。
    """
    return {
        "table_id": row.get("id"),
        "table_name": row.get("name"),
        "description": row.get("description"),
        "comment": row.get("comment"),
        "change_flag": row.get("change_flag"),
        "semantic_model_id": row.get("semantic_model_id"),
        "data_source_id": row.get("data_source_id"),
        "fields": [],
    }


def _row_to_field_dict(row):
    """将单行 semantic_model_field 记录转换为目标 JSON 结构。"""
    return {
        "field_id": row.get("id"),
        "field_name": row.get("name"),
        "data_type": row.get("type"),
        "enum_values": _parse_json(row.get("enum_values")) or [],
        "null_flag": row.get("null_flag"),
        "comment": row.get("comment"),
        "description": row.get("description"),
        "table_id": row.get("table_id"),
        "semantic_model_id": row.get("semantic_model_id"),
        "data_source_id": row.get("data_source_id"),
    }


def _data_source_scope_clause(
    semantic_model_id=None,
    data_source_id=None,
    alias=None,
):
    """构造表/字段查询的 where 子句与参数。

    Args:
        semantic_model_id: 可选，None 表示不按 sm 过滤（含 NULL 数据）
        data_source_id: 可选，None 表示不按 ds 过滤
        alias: 表别名（如 "t."），None 表示无别名

    Returns:
        (where_clause, args)
    """
    prefix = f"{alias}." if alias else ""
    parts = [f"{prefix}is_deleted = 0"]
    args = []
    if semantic_model_id is not None:
        parts.append(
            f"({prefix}semantic_model_id = %s OR {prefix}semantic_model_id IS NULL)"
        )
        args.append(semantic_model_id)
    if data_source_id is not None:
        parts.append(f"{prefix}data_source_id = %s")
        args.append(data_source_id)
    return " AND ".join(parts), args


def get_tables(semantic_model_id=None, data_source_id=None):
    """获取物理表列表，可按 semantic_model_id / data_source_id 过滤。

    Args:
        semantic_model_id: 可选，过滤绑定到该语义建模的表（NULL 数据不会被排除）
        data_source_id: 可选，过滤指定数据源的表
    """
    where, args = _data_source_scope_clause(semantic_model_id, data_source_id)
    sql = f"""
        SELECT id, semantic_model_id, data_source_id, name, change_flag,
               description, comment, create_time, update_time
        FROM semantic_model_table
        WHERE {where}
        ORDER BY data_source_id, id
    """
    try:
        rows = _query(sql, args)
        return [_row_to_table_dict(r) for r in rows]
    except Exception:
        raise


def get_fields(semantic_model_id=None, table_id=None, data_source_id=None):
    """获取物理字段列表，可按 semantic_model_id / table_id / data_source_id 过滤。

    Args:
        semantic_model_id: 可选，过滤绑定到该语义建模的字段
        table_id: 可选，过滤属于指定表的字段
        data_source_id: 可选，过滤指定数据源的字段
    """
    parts = ["is_deleted = 0"]
    args = []
    if semantic_model_id is not None:
        parts.append("(semantic_model_id = %s OR semantic_model_id IS NULL)")
        args.append(semantic_model_id)
    if table_id is not None:
        parts.append("table_id = %s")
        args.append(table_id)
    if data_source_id is not None:
        parts.append("data_source_id = %s")
        args.append(data_source_id)
    where = " AND ".join(parts)
    sql = f"""
        SELECT id, semantic_model_id, data_source_id, table_id, name, type,
               enum_values, null_flag, comment, description, create_time, update_time
        FROM semantic_model_field
        WHERE {where}
        ORDER BY table_id, id
    """
    try:
        rows = _query(sql, args)
        return [_row_to_field_dict(r) for r in rows]
    except Exception:
        raise


def get_table_field_by_scope(semantic_model_id=None, data_source_id=None, *, business_domain_id=None):
    """按作用域聚合查询：物理表 + 其下挂载的字段。

    作用域维度：data_source_id 为主隔离；semantic_model_id 为附加过滤。
    二者均可为 None，None 表示不按该字段过滤（含 NULL 数据）。

    Args:
        semantic_model_id: 可选
        data_source_id: 可选

    Returns:
        {
            "scope": {
                "semantic_model_id": int | None,
                "data_source_id": int | None,
            },
            "tables": [
                {
                    "table_id":..., "table_name":..., "description":..., "comment":...,
                    "change_flag":..., "semantic_model_id":..., "data_source_id":...,
                    "fields": [ {field_dict}, ... ]
                },
                ...
            ],
        }

    Note:
        - 一次查询所有表 + 一次查询所有字段，内存按 table_id 分组挂载，避免 N+1。
        - 字段按 (table_id, name, id) 去重保留最新（参考 get_entity 的 _is_newer 策略）。
    """
    where, args = _data_source_scope_clause(semantic_model_id, data_source_id)

    sql_tables = f"""
        SELECT id, semantic_model_id, data_source_id, name, change_flag,
               description, comment, create_time, update_time
        FROM semantic_model_table
        WHERE {where}
        ORDER BY data_source_id, id
    """
    sql_fields = f"""
        SELECT id, semantic_model_id, data_source_id, table_id, name, type,
               enum_values, null_flag, comment, description, create_time, update_time
        FROM semantic_model_field
        WHERE {where}
        ORDER BY table_id, id
    """

    if business_domain_id is not None:
        from scope_contract import normalize_domains, require_model_id
        require_model_id(semantic_model_id)
        domain_ids = normalize_domains(
            business_domain_ids=business_domain_id if isinstance(business_domain_id, (list, tuple)) else [business_domain_id]
        )
        if not domain_ids:
            raise ValueError('REQUEST_SCOPE_INVALID: explicit physical scope is empty')
        # Restrict the metadata query itself through published entities, rather
        # than fetching the model-wide registry and filtering it afterwards.
        scope_where = '''t.is_deleted=0 AND t.semantic_model_id=%s
            AND EXISTS (
                SELECT 1 FROM semantic_model_entity_type e
                JOIN semantic_model_business_domain b ON b.id=e.business_domain_id
                WHERE b.semantic_model_id=%s AND b.id=%s
                  AND COALESCE(b.is_deleted,0)=0
                  AND COALESCE(e.is_deleted,0)=0 AND e.status=1
                  AND e.main_table_name=t.name AND e.data_source_id=t.data_source_id
            )'''
        args = [semantic_model_id, semantic_model_id, domain_ids[0]]
        if data_source_id is not None:
            scope_where += ' AND t.data_source_id=%s'
            args.append(data_source_id)
        sql_tables = f'''SELECT t.* FROM semantic_model_table t
            WHERE {scope_where} ORDER BY t.data_source_id, t.id'''
        sql_fields = f'''SELECT f.* FROM semantic_model_field f
            JOIN semantic_model_table t ON t.id=f.table_id
            WHERE {scope_where} AND COALESCE(f.is_deleted,0)=0
              AND (f.semantic_model_id=t.semantic_model_id OR f.semantic_model_id IS NULL)
              AND f.data_source_id=t.data_source_id
            ORDER BY f.table_id, f.id'''

    try:
        conn = _get_connection()
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(sql_tables, args)
                table_rows = cur.fetchall()

                cur.execute(sql_fields, args)
                field_rows = cur.fetchall()
        finally:
            conn.close()

        # 按 (table_id, field_name) 去重，保留最新行
        field_map: dict[tuple, dict] = {}
        for row in field_rows:
            key = (row.get("table_id"), row.get("name"))
            existing = field_map.get(key)
            if existing is None or _is_newer(row, existing):
                field_map[key] = row

        # 基于去重后的 field_map 按 table_id 分组，避免依赖迭代顺序
        fields_by_table: dict[int, list[dict]] = {}
        for row in field_map.values():
            tid = row.get("table_id")
            if tid is None:
                continue
            fields_by_table.setdefault(tid, []).append(_row_to_field_dict(row))
        # 字段排序保证稳定（参考 SQL ORDER BY table_id, id）
        for tid in fields_by_table:
            fields_by_table[tid].sort(
                key=lambda f: (f.get("table_id") or 0, f.get("field_id") or 0)
            )

        tables = [_row_to_table_dict(r) for r in table_rows]
        for t in tables:
            t["fields"] = fields_by_table.get(t["table_id"], [])

        return {
            "scope": {
                "semantic_model_id": semantic_model_id,
                "data_source_id": data_source_id,
            },
            "tables": tables,
        }
    except Exception:
        raise


def load_all_table_fields():
    """从 MySQL 全量加载所有数据源的物理表 + 字段（用于一次性构建全量索引）。

    按 data_source_id 分组，每个数据源生成一份作用域文档。

    Returns:
        list[dict]: 每个元素是一个数据源作用域文档（结构见 get_table_field_by_scope）
    """
    sql = """
        SELECT DISTINCT data_source_id
        FROM semantic_model_table
        WHERE is_deleted = 0 AND data_source_id IS NOT NULL
        ORDER BY data_source_id
    """
    try:
        ds_rows = _query(sql)
        docs = []
        for r in ds_rows:
            ds_id = r["data_source_id"]
            doc = get_table_field_by_scope(data_source_id=ds_id)
            docs.append(doc)
        return docs
    except Exception:
        raise


if __name__ == "__main__":
    # 演示：列出所有语义建模 → 选择「商超业务数据」(id=6) → 列出业务域 → 选「销售交易域」(id=7) → 拉 DSL
    print("=" * 60)
    print("1. 所有语义建模")
    print("=" * 60)
    models = get_semantic_models()
    for m in models:
        print(f"  id={m['id']:3d}  name={m['name']:20s}  code={m['code']}")

    target_sm_id = 81  # 商超业务数据
    print(f"\n选择语义建模 id={target_sm_id}")

    print("\n" + "=" * 60)
    print(f"2. 语义建模 {target_sm_id} 下的业务域")
    print("=" * 60)
    domains = get_business_domains(target_sm_id)
    for d in domains:
        print(f"  id={d['id']:3d}  name={d['name']:20s}  code={d['code']}")

    target_bd_id = 205  # 销售交易域
    print(f"\n选择业务域 id={target_bd_id}")

    print("\n" + "=" * 60)
    print(f"3. 作用域 sm={target_sm_id}, bd={target_bd_id} 下的 DSL")
    print("=" * 60)
    dsl = get_dsl_by_scope(target_sm_id, target_bd_id)
    print(f"语义建模: {dsl['semantic_model']['name']}")
    print(f"业务域  : {dsl['business_domain']['name'] if dsl['business_domain'] else '(无)'}")
    print(f"实体数量: {len(dsl['entities'])}")
    print(f"指标数量: {len(dsl['metrics'])}")
    print(f"维度数量: {len(dsl['dimensions'])}")
    if dsl["entities"]:
        print("\n首个实体示例:")
        print(json.dumps(dsl["entities"][0], ensure_ascii=False, indent=2, default=str))
    if dsl["metrics"]:
        print("\n首个指标示例:")
        print(json.dumps(dsl["metrics"][0], ensure_ascii=False, indent=2, default=str))
    if dsl["dimensions"]:
        print("\n首个维度示例:")
        print(json.dumps(dsl["dimensions"][0], ensure_ascii=False, indent=2, default=str))
