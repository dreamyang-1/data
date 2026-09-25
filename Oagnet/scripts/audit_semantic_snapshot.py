"""Read a local Chroma snapshot without clients, credentials or writes.

Only aggregates and a content hash are returned. A local snapshot is not proof
of a deployed Milvus/MySQL version. Never use this tool to claim deployment PASS.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3


def audit_snapshot(path):
    path = Path(path).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        rows = conn.execute('SELECT id,key,string_value,int_value,float_value FROM embedding_metadata ORDER BY id,key').fetchall()
    records = {}
    for ident, key, string, integer, number in rows:
        records.setdefault(ident, {})[key] = string if string is not None else integer if integer is not None else number
    shared = [r for r in records.values() if r.get('business_domain_id') == -1]
    dimensions = [r for r in shared if r.get('type') == 'dimension']
    entities = [r for r in records.values() if r.get('type') == 'entity']
    projections = [r for r in records.values() if r.get('type') == 'scoped_dimension']
    return dict(
        evidence_kind='LOCAL_CHROMA_SNAPSHOT_NOT_DEPLOYED_MILVUS',
        read_only=True, snapshot_rows_sha256=hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',', ':')).encode()).hexdigest(),
        record_count=len(records), shared_record_type_counts=dict(Counter(r.get('type') for r in shared)),
        explicit_dimension_projection_count=len(projections),
        entity_records_with_governed_id=sum(r.get('entity_id') is not None for r in entities),
        entity_records_total=len(entities),
        dimensions_with_opaque_rules=sum(bool(json.loads(r.get('special_rules') or '[]')) for r in dimensions),
        dimensions_with_hierarchy=sum(bool(json.loads(r.get('dim_hierarchy') or '[]')) for r in dimensions),
        requires_catalog_republication=bool(dimensions and not projections),
        deployment_gate='UNKNOWN', sensitive_records_exported=0,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_snapshot(args.snapshot), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
