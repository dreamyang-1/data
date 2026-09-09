"""Opt-in exact reads for already selected catalog fields. Private output only.

No scan, suffix guessing, publication or business writes. Each source SELECT is
parameterized, bounded and rolled back by the existing Oagnet reader. This tool
captures source evidence; it does not label a query or certify production data.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

from tools.cutover.build_evaluation_gold import freeze
from tools.cutover.frozen_source_values import seal, FrozenSourceValues


def capture(raw, catalog, requests, *, service_root, allow_source_reads=False):
    if not allow_source_reads:
        raise ValueError('EXPLICIT_READ_ONLY_SOURCE_OPT_IN_REQUIRED')
    snapshot = json.loads(raw)
    if freeze(snapshot, hashlib.sha256(raw).hexdigest(), catalog['observed_at']) != catalog:
        raise ValueError('FROZEN_SNAPSHOT_PROJECTION_MISMATCH')
    if not isinstance(requests, list) or not 1 <= len(requests) <= 20:
        raise ValueError('SOURCE_CAPTURE_BUDGET_INVALID')
    fields = snapshot['physical_catalog']['entity_value_sources']['fields']
    selected = []
    seen = set()
    for request in requests:
        if not isinstance(request, dict) or set(request) != {'attribute_id','query','limit'}:
            raise ValueError('SOURCE_CAPTURE_REQUEST_INVALID')
        field = [f for f in fields if f['attribute_id'] == request['attribute_id']]
        query, limit = request['query'], request['limit']
        if (len(field) != 1 or not isinstance(query, str) or not 1 <= len(query.strip()) <= 256
                or type(limit) is not int or not 1 <= limit <= 32):
            raise ValueError('SOURCE_CAPTURE_REQUEST_INVALID')
        key = (request['attribute_id'], query, limit)
        if key in seen: raise ValueError('SOURCE_CAPTURE_DUPLICATE_REQUEST')
        seen.add(key); selected.append((field[0], query, limit))
    previous_logging = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with patch.object(sys, 'path', [*sys.path, str(service_root)]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            import mysql_tool as mysql
            from catalog_release import capture_catalog
            from catalog_value_sources import observe
            if capture_catalog(*[catalog['scope'][k] for k in ('semantic_model_id','business_domain_ids')])['catalog_version'] != catalog['catalog_version']:
                raise ValueError('SOURCE_CAPTURE_CATALOG_DRIFT')
            entries = []
            with patch.object(mysql, 'MYSQL_CONNECT_TIMEOUT', 8), patch.object(mysql, 'MYSQL_READ_TIMEOUT', 20):
                for field, query, limit in selected:
                    observation = observe(catalog['scope'], field, query, limit)
                    entries.append(dict(query=mysql.normalize_catalog_text(query), limit=limit, observation=observation))
            if capture_catalog(*[catalog['scope'][k] for k in ('semantic_model_id','business_domain_ids')])['catalog_version'] != catalog['catalog_version']:
                raise ValueError('SOURCE_CAPTURE_CATALOG_DRIFT')
            bundle = seal(entries, catalog=catalog, capture_mode='READ_ONLY_SOURCE')
            FrozenSourceValues(bundle, catalog=catalog, snapshot=snapshot, expected_hash=bundle['artifact_hash'])
        return bundle
    finally:
        logging.disable(previous_logging)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('snapshot','catalog','requests','service-root','private-output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--allow-source-reads', action='store_true', required=True)
    args = parser.parse_args()
    if args.private_output.exists(): parser.error('Refusing to replace an existing capture')
    result = capture(args.snapshot.read_bytes(), json.loads(args.catalog.read_text(encoding='utf-8')),
        json.loads(args.requests.read_text(encoding='utf-8')), service_root=args.service_root,
        allow_source_reads=args.allow_source_reads)
    with args.private_output.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(dict(artifact_hash=result['artifact_hash'], requests=len(result['entries']),
        observations=[dict(attribute_id=e['observation']['field']['attribute_id'],
            query_hash=e['observation']['query_hash'], result_count=len(e['observation']['values']),
            complete=e['observation']['complete']) for e in result['entries']], production_writes=0)))
