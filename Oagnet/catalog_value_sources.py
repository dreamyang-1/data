"""Private source evidence for pinned, read-only entity-value lookup.

Semantic mappings and connection *configuration* belong to the catalog version.
Mutable business values do not: an observation is checked again at acceptance,
and is never a published vector row, permission grant or execution snapshot.
No public API, vector rebuild or model-selected connection is introduced.
"""
from copy import deepcopy
from datetime import datetime, timezone

from catalog_release import CatalogEvidenceError, catalog_scope, digest
from dimension_scope import normalize_governed_id

CONTRACT = "catalog-entity-value-sources-v1"


def _positive(value):
    if type(value) is not int or value <= 0:
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_ID_INVALID")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_MAPPING_INVALID")
    return value


def _governed(value):
    result = normalize_governed_id(value)
    if result is None:
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_ID_INVALID")
    return result


def _port(value):
    # The authoritative data-source table stores port as VARCHAR. This is
    # metadata normalization, never coercion of a request's model/domain IDs.
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        value = int(value)
    if type(value) is not int or not 1 <= value <= 65535:
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_ROUTE_INVALID")
    return value


def route_identity(row):
    """Credentials are excluded; changing a locator invalidates the mapping pin."""
    port = _port(row.get("port"))
    return {"semantic_model_id": _positive(row.get("semantic_model_id")),
        "data_source_id": _positive(row.get("data_source_id")),
        "db_type": _text(row.get("db_type")).strip().casefold(),
        "locator_hash": digest({"host": _text(row.get("host")), "port": port,
                                "database": _text(row.get("db_name"))})}


def _definition_rows(scope, *, credentials=False, attribute_id=None):
    """All identifiers and membership joins originate in governed metadata.

    The capture reads no credentials; only an explicitly invoked value lookup
    requests credentials for its one selected current attribute. No cache table
    or legacy vector row participates in this authority query.
    """
    import mysql_tool as mysql
    sql = """SELECT DISTINCT b.semantic_model_id, e.business_domain_id,
        e.id AS entity_id, e.code AS entity_code,
        a.id AS attribute_id, a.code AS attr_code, a.attr_name, a.data_type,
        a.mapping_table, a.mapping_column, a.vectorization, a.is_main_attribute,
        t.id AS table_id, f.id AS field_id, ds.id AS data_source_id,
        ds.db_type, ds.host, ds.port, ds.db_name
    """
    if credentials:
        sql += ", ds.username, ds.password "
    sql += """FROM semantic_model m
        JOIN semantic_model_business_domain b ON b.semantic_model_id=m.id
            AND COALESCE(b.is_deleted,0)=0
        JOIN semantic_model_entity_type e ON e.business_domain_id=b.id
            AND (e.semantic_model_id=m.id OR e.semantic_model_id IS NULL)
            AND COALESCE(e.is_deleted,0)=0 AND e.status=1
        JOIN semantic_model_attribute_config a ON a.entity_type_id=e.id
            AND a.semantic_model_id=m.id AND COALESCE(a.is_deleted,0)=0
        JOIN semantic_model_data_source ds ON ds.id=e.data_source_id
            AND ds.semantic_model_id=m.id AND COALESCE(ds.is_deleted,0)=0 AND ds.status=1
        JOIN semantic_model_table t ON t.semantic_model_id=m.id AND t.data_source_id=ds.id
            AND t.name=a.mapping_table AND COALESCE(t.is_deleted,0)=0
        JOIN semantic_model_field f ON f.semantic_model_id=m.id AND f.data_source_id=ds.id
            AND f.table_id=t.id AND f.name=a.mapping_column AND COALESCE(f.is_deleted,0)=0
        WHERE m.id=%s AND COALESCE(m.is_deleted,0)=0
    """
    params = [scope["semantic_model_id"]]
    if scope["business_domain_ids"]:
        sql += " AND b.id=%s"
        params.append(scope["business_domain_ids"][0])
    if attribute_id is not None:
        sql += " AND a.id=%s"
        params.append(_governed(attribute_id))
    sql += " ORDER BY e.business_domain_id,e.id,a.id,t.id,f.id"
    return mysql._query(sql, tuple(params))


def field_identity(row, scope):
    import mysql_tool as mysql
    if (_positive(row.get("semantic_model_id")) != scope["semantic_model_id"]
            or (scope["business_domain_ids"] and row.get("business_domain_id") not in scope["business_domain_ids"])):
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_SCOPE_MISMATCH")
    field = {key: _positive(row.get(key)) for key in (
        "semantic_model_id", "business_domain_id",
        "table_id", "field_id", "data_source_id")}
    field.update({key: _governed(row.get(key)) for key in ("entity_id", "attribute_id")})
    field.update({key: _text(row.get(key)) for key in (
        "entity_code", "attr_code", "mapping_table", "mapping_column")})
    # Keep declared policy, not a guessed default. is_main_attribute alone does
    # not authorize fuzzy/vector lookup; exact lookup explicitly names a field.
    field["vectorization"] = mysql._parse_json(row.get("vectorization"))
    field["data_type"] = row.get("data_type")
    field["route"] = route_identity(row)
    field["mapping_hash"] = digest(field)
    return field


def capture_value_sources(scope):
    """Called inside capture_catalog's single read-only metadata transaction."""
    scope = catalog_scope(scope["semantic_model_id"], scope["business_domain_ids"])
    fields, seen = [], set()
    for row in _definition_rows(scope):
        field = field_identity(row, scope)
        key = field["business_domain_id"], field["entity_id"], field["attribute_id"]
        if key in seen:
            # Duplicated physical registrations are not first-wins authority.
            raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_MAPPING_AMBIGUOUS")
        seen.add(key)
        fields.append(field)
    fields.sort(key=lambda f: (f["business_domain_id"], f["entity_id"], f["attribute_id"]))
    return {"contract": CONTRACT, "scope": deepcopy(scope), "fields": fields}


def bound_field(snapshot, attribute, *, data_source_id=None):
    """Tie one verified catalog ATTRIBUTE row to its exact owner and source."""
    source = snapshot.get("physical_catalog", {}).get("entity_value_sources")
    scope = snapshot["scope"]
    if not isinstance(source, dict) or source.get("contract") != CONTRACT or source.get("scope") != scope:
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCES_NOT_CAPTURED")
    metadata = attribute.metadata
    if metadata.get("type") != "attribute":
        raise CatalogEvidenceError("CATALOG_VALUE_ATTRIBUTE_REQUIRED")
    fields = [f for f in source["fields"] if (
        f["semantic_model_id"] == metadata.get("semantic_model_id")
        and f["business_domain_id"] == metadata.get("business_domain_id")
        and f["attribute_id"] == normalize_governed_id(metadata.get("attribute_id"))
        and f["attr_code"] == metadata.get("attr_code")
        and f["entity_code"] == metadata.get("parent")
        and f'{f["mapping_table"]}.{f["mapping_column"]}' == metadata.get("field_mapping"))]
    if len(fields) != 1:
        raise CatalogEvidenceError("CATALOG_VALUE_FIELD_UNVERIFIED")
    field = fields[0]
    # A duplicate logical entity code cannot substitute for its captured ID.
    owners = [e for d in snapshot["documents"] for e in d["entities"]
              if e.get("entity_code") == field["entity_code"] and e.get("business_domain") == field["business_domain_id"]]
    if len(owners) != 1 or normalize_governed_id(owners[0].get("entity_id")) != field["entity_id"]:
        raise CatalogEvidenceError("CATALOG_VALUE_OWNER_UNVERIFIED")
    if data_source_id is not None and _positive(data_source_id) != field["data_source_id"]:
        raise CatalogEvidenceError("CATALOG_VALUE_DATA_SOURCE_MISMATCH")
    return deepcopy(field)


def query_values(scope, field, value, limit):
    """Fresh exact source SELECT with bounded, collation-independent candidates.

    This function is an internal trusted seam, not a model tool. Its caller
    supplies a field proved by bound_field. Connection credentials never leave
    this function, and failures contain bounded codes only.
    """
    import mysql_tool as mysql
    try:
        rows = _definition_rows(scope, credentials=True, attribute_id=field["attribute_id"])
        if len(rows) != 1 or field_identity(rows[0], scope) != field:
            raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_CHANGED")
        source = rows[0]
        if field["route"]["db_type"] not in ("mysql", "mariadb"):
            raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_DRIVER_UNSUPPORTED")
        table = mysql._validated_identifier(field["mapping_table"], label="table")
        column = mysql._validated_identifier(field["mapping_column"], label="column")
        username = _text(source.get("username"))
        connection = mysql.pymysql.connect(host=source["host"], port=_port(source["port"]),
            user=username, password=source.get("password") or "", database=source["db_name"],
            charset=mysql.MYSQL_CHARSET, connect_timeout=mysql.MYSQL_CONNECT_TIMEOUT,
            read_timeout=mysql.MYSQL_READ_TIMEOUT, write_timeout=mysql.MYSQL_READ_TIMEOUT,
            autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                expression = f"TRIM(CAST(`{column}` AS CHAR))"
                normalized = mysql._normalized_catalog_sql_expression(expression)
                cursor.execute(f"SELECT DISTINCT CAST({expression} AS BINARY) FROM `{table}` "
                    f"WHERE `{column}` IS NOT NULL AND CAST({normalized} AS BINARY)=CAST(%s AS BINARY) "
                    f"ORDER BY CAST({expression} AS BINARY) LIMIT %s", (value, limit + 1))
                result = cursor.fetchall()
                return [r[0].decode("utf-8") if isinstance(r[0], bytes) else r[0] for r in result]
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()
    except CatalogEvidenceError:
        raise
    except Exception:
        raise CatalogEvidenceError("CATALOG_VALUE_LOOKUP_FAILED") from None


def observe(scope, field, value, limit):
    import mysql_tool as mysql
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256 or type(limit) is not int or not 1 <= limit <= 32:
        raise CatalogEvidenceError("CATALOG_VALUE_QUERY_INVALID")
    value = mysql.normalize_catalog_text(value)
    rows = query_values(scope, field, value, limit)
    # Validate the result boundary too; neither transport nor a vector candidate
    # can silently supply a different canonical value or claim completeness.
    if (not isinstance(rows, list) or len(rows) > limit + 1
            or any(not isinstance(v, str) or not v.strip() or len(v) > 1024
                   or mysql.normalize_catalog_text(v) != value for v in rows)
            or len(set(rows)) != len(rows)):
        raise CatalogEvidenceError("CATALOG_VALUE_SOURCE_RESULT_INVALID")
    values = sorted(rows)
    material = {"source": "VERIFIED_SOURCE_EXACT_LOOKUP", "scope": deepcopy(scope),
        "field": deepcopy(field), "match_mode": "EXACT_NORMALIZED", "query_hash": digest(value),
        "values": values[:limit], "complete": len(values) <= limit}
    return {**material, "observation_hash": digest(material),
            "observed_at": datetime.now(timezone.utc).isoformat()}
