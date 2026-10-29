"""Deterministic, per-axis offline scoring. Missing predictions are failures.

Gold can be complete for an axis while SQL/result/identity labels remain open.
Never turn a partial-axis score into end-to-end replacement acceptance.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
from tools.cutover.evaluation_contract import digest


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def validate_gold(rows, catalog):
    material = {k:v for k,v in catalog.items() if k != 'artifact_hash'}
    if catalog.get('artifact_hash') != digest(material):
        raise ValueError('EVALUATION_CATALOG_HASH_INVALID')
    ids = set(); facts = {f['fact_id']:f for f in catalog['facts']}
    for row in rows:
        if row['case_id'] in ids:
            raise ValueError('DUPLICATE_CASE_ID')
        ids.add(row['case_id'])
        if row['catalog_ref'] != catalog['artifact_hash'] or row['scope'] != catalog['scope']:
            raise ValueError('GOLD_SCOPE_OR_CATALOG_MISMATCH')
        if not row.get('labels') or row.get('label_status') != 'REVIEWED_FOR_LISTED_AXES':
            raise ValueError('GOLD_LABEL_EVIDENCE_REQUIRED')
        if any(ref not in facts for ref in row['catalog_evidence']):
            raise ValueError('GOLD_CATALOG_FACT_MISSING')
        for target in row['labels'].get('mentions', []):
            start, end = target['start'], target['end']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(row['current_utterance']):
                raise ValueError('GOLD_SPAN_INVALID')
            if row['current_utterance'][start:end] != target['surface']:
                raise ValueError('GOLD_SURFACE_MISMATCH')
            if not target['roles']:
                raise ValueError('GOLD_ROLE_LABEL_REQUIRED')
    return ids


def evaluate(rows, predictions, catalog):
    ids = validate_gold(rows, catalog)
    supplied = {}
    for p in predictions:
        if p['case_id'] not in ids or p['case_id'] in supplied:
            raise ValueError('UNKNOWN_OR_DUPLICATE_PREDICTION')
        if p['catalog_ref'] != catalog['artifact_hash'] or p['scope'] != catalog['scope']:
            raise ValueError('PREDICTION_SCOPE_OR_CATALOG_MISMATCH')
        supplied[p['case_id']] = p
    totals = Counter(); passed = Counter(); details = []
    missing = 0
    for row in rows:
        p = supplied.get(row['case_id'])
        valid = bool(p and p.get('status') == 'OK')
        if not valid:
            missing += 1
        actual = p.get('prediction', {}) if valid else {}
        axes = {}
        for axis, expected in row['labels'].items():
            if axis == 'mentions':
                # This corpus labels selected critical mentions, not every word.
                # Explicitly report recall/role coverage; do not invent precision.
                actual_mentions = actual.get('mentions', [])
                found = roles = pure = 0
                for target in expected:
                    matches = [m for m in actual_mentions if m.get('start') == target['start'] and m.get('end') == target['end']]
                    found += bool(matches)
                    roles += any(set(m.get('roles', [])) & set(target['roles']) for m in matches)
                    pure += bool(matches) and all(m.get('roles') and set(m['roles']) <= set(target['roles']) for m in matches)
                for name, count in [('critical_mention_recall', found), ('critical_role_recall', roles),('critical_role_purity',pure)]:
                    totals[name] += len(expected); passed[name] += count
                axes['mentions'] = found == len(expected) and roles == len(expected)
            else:
                totals[axis] += 1
                axes[axis] = valid and actual.get(axis) == expected
                passed[axis] += axes[axis]
        details.append({'case_id': row['case_id'], 'axes': axes,
            'prediction_status': p.get('status', 'MISSING') if p else 'MISSING'})
    return {'contract': 'semantic-axis-evaluation-v1', 'cases':len(rows),
        'catalog_version':catalog['catalog_version'], 'catalog_ref':catalog['artifact_hash'], 'scope':catalog['scope'],
        'missing_or_failed_predictions':missing,
        'metrics':{axis:{'passed':passed[axis], 'denominator':n, 'value':passed[axis]/n if n else None} for axis,n in sorted(totals.items())},
        'unlabeled_axes':['SQL_EXECUTION', 'RESULT_ACCURACY', 'FULL_STATE_MUTATION', 'CANONICAL_BINDING', 'EXACT_ENTITY_COUNT'],
        'critical_mention_precision':'NOT_LABELED', 'end_to_end_acceptance':'NOT_EVALUATED',
        'production_cutover_pass':False, 'details':details}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    for name in ('gold','predictions','catalog','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an evaluation receipt')
    result=evaluate(read_jsonl(args.gold),read_jsonl(args.predictions),json.loads(args.catalog.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='details'},ensure_ascii=False))
