"""Indexed candidates for one pinned source field, never canonical authority.

Reuse the source reader's governed mapping and connection. Prefix retrieval is
only discovery: no suffix rules, inferred aliases, automatic top-one binding,
new index or unbounded field enumeration. An unavailable range access fails
closed. The existing model choice and exact source binder remain mandatory.
"""
from copy import deepcopy
from datetime import datetime, timezone

from catalog_release import CatalogEvidenceError, digest
from catalog_value_sources import _definition_rows, _port, field_identity

TARGETED_LIMIT = 8  # Same small budget as the existing exact-value reader.


def query_candidates(scope, field, value, limit):
    import mysql_tool as mysql
    try:
        definitions = _definition_rows(scope, credentials=True, attribute_id=field['attribute_id'])
        if len(definitions) != 1 or field_identity(definitions[0], scope) != field:
            raise CatalogEvidenceError('CATALOG_VALUE_SOURCE_CHANGED')
        source = definitions[0]
        if field['route']['db_type'] not in ('mysql', 'mariadb'):
            raise CatalogEvidenceError('CATALOG_VALUE_SOURCE_DRIVER_UNSUPPORTED')
        table = mysql._validated_identifier(field['mapping_table'], label='table')
        column = mysql._validated_identifier(field['mapping_column'], label='column')
        pattern = value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        connection = mysql.pymysql.connect(host=source['host'], port=_port(source['port']),
            user=source['username'], password=source.get('password') or '', database=source['db_name'],
            charset=mysql.MYSQL_CHARSET, connect_timeout=8, read_timeout=20, write_timeout=8, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute('START TRANSACTION READ ONLY')
                cursor.execute(f'SHOW INDEX FROM `{table}`')
                indexes = cursor.fetchall()
                eligible = {r[2] for r in indexes if len(r) > 10 and r[3] == 1
                    and r[4] == column and r[10] == 'BTREE'}
                if not eligible:
                    raise CatalogEvidenceError('CATALOG_VALUE_TARGETED_INDEX_REQUIRED')
                # Preserve different real spellings even when the database
                # collation considers them equal. LIKE only retrieves candidates.
                statement = (f'SELECT /*+ MAX_EXECUTION_TIME(15000) */ DISTINCT CAST(`{column}` AS BINARY) FROM `{table}` '
                    f"WHERE `{column}` LIKE %s ESCAPE '!' ORDER BY CAST(`{column}` AS BINARY) LIMIT %s")
                args = (pattern, limit + 1)
                cursor.execute('EXPLAIN ' + statement, args)
                plans = cursor.fetchall()
                if (len(plans) != 1 or len(plans[0]) < 7 or plans[0][4] not in ('range', 'ref', 'const')
                        or plans[0][6] not in eligible):
                    raise CatalogEvidenceError('CATALOG_VALUE_TARGETED_RANGE_REQUIRED')
                if field['route']['db_type'] == 'mariadb':
                    statement = 'SET STATEMENT max_statement_time=15 FOR ' + statement
                cursor.execute(statement, args)
                return [r[0].decode('utf-8') if isinstance(r[0], bytes) else r[0] for r in cursor.fetchall()]
        finally:
            try: connection.rollback()
            finally: connection.close()
    except CatalogEvidenceError:
        raise
    except Exception:
        raise CatalogEvidenceError('CATALOG_VALUE_TARGETED_LOOKUP_FAILED') from None


def observe_candidates(scope, field, value, limit=TARGETED_LIMIT):
    import mysql_tool as mysql
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 256
            or type(limit) is not int or not 1 <= limit <= TARGETED_LIMIT):
        raise CatalogEvidenceError('CATALOG_VALUE_TARGETED_QUERY_INVALID')
    value = mysql.normalize_catalog_text(value)
    rows = query_candidates(scope, field, value, limit)
    if (not isinstance(rows, list) or len(rows) > limit + 1
            or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in rows)
            or len(rows) != len(set(rows))):
        raise CatalogEvidenceError('CATALOG_VALUE_TARGETED_RESULT_INVALID')
    complete = len(rows) <= limit  # Completeness of this query, not of the field.
    material = dict(source='VERIFIED_SOURCE_TARGETED_CANDIDATES', scope=deepcopy(scope), field=deepcopy(field),
        match_mode='PREFIX_CANDIDATE_DISCOVERY', query_hash=digest(value),
        values=sorted(rows) if complete else [], complete=complete)
    return {**material, 'observation_hash': digest(material), 'observed_at': datetime.now(timezone.utc).isoformat()}
