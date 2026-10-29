"""Full-file static inventories and same-node test comparison for the closure report."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT/'docs/phase25_1'
REQUIRED = [
    'app/intent/structured.py','app/intent/classifier.py','app/services/turn_admission.py',
    'app/services/question_rewriter.py','app/services/intent_asl_contract.py','app/services/orchestrator.py',
    'app/domain/models.py','app/stores/session.py','minio_followup_store.py',
    'tests/test_semantic_v2_contracts.py','tests/test_phase25_tools.py','tests/test_turn_admission.py',
    'tests/test_structured_intent.py','tests/test_clarification_flow.py','tests/test_pending_execution_transition.py',
    'tests/test_conversation_result_followup.py','Long-range Conversational Data Agent V2设计报告.md',
]


def write(name, data):
    (DOCS/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def source_inventory():
    paths = [ROOT/p for p in REQUIRED]
    paths += list((ROOT/'app/semantic_v2').glob('*.py'))
    paths += list((ROOT/'docs/phase25').glob('*')) + list((ROOT/'docs/phase2').glob('*'))
    paths += [ROOT.parent/'AGENTS.md', Path('E:/yy/AGENTS.md')]
    baseline = json.loads((DOCS/'preexisting_workspace_drift.json').read_text(encoding='utf-8'))
    initial = {r['path']: r for r in baseline['files']}
    inventory, magic, imports = [], [], []
    markers = ('DEFAULT_TIME_RANGE=', 'DEFAULT_TIME_GRANULARITY=', 'SORT_DIRECTION=', 'TIME_SCOPE=', 'SET_RELATIONSHIP_PROJECTION')
    for path in paths:
        text = path.read_text(encoding='utf-8-sig')
        relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        entry = dict(path=relative, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), lines=len(text.splitlines()),
                     coverage='FULL_FILE_STATIC_SCAN', initial_workspace_sha256=initial.get(relative,{}).get('workspace_sha256'))
        if path.suffix == '.py':
            tree = ast.parse(text)
            entry['definitions'] = [dict(name=n.name, start=n.lineno, end=n.end_lineno) for n in ast.walk(tree) if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))]
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for marker in markers:
                        if marker in node.value:
                            magic.append(dict(path=relative,line=node.lineno,marker=marker))
                if isinstance(node, ast.ImportFrom) and node.module and 'semantic_v2' in node.module:
                    imports.append(dict(path=relative,line=node.lineno,module=node.module))
        inventory.append(entry)
    write('source_inventory.json', inventory)
    write('magic_string_audit.json', magic)
    write('semantic_v2_import_audit.json', imports)
    return inventory, magic


def compare_tests(final_name='final_test_gate.json'):
    baseline = json.loads((DOCS/'baseline_test_gate.json').read_text(encoding='utf-8'))
    final = json.loads((DOCS/final_name).read_text(encoding='utf-8'))
    old, new = baseline['results'], final['results']
    groups = {key: [] for key in ('old_pass_still_pass','old_pass_now_fail','old_fail_now_pass','old_fail_still_fail','new_pass','new_fail')}
    for node, result in old.items():
        if result == 'passed':
            groups['old_pass_still_pass' if new.get(node) == 'passed' else 'old_pass_now_fail'].append(node)
        elif result == 'failed':
            groups['old_fail_now_pass' if new.get(node) == 'passed' else 'old_fail_still_fail'].append(node)
    for node in sorted(set(new)-set(old)):
        groups['new_pass' if new[node] == 'passed' else 'new_fail'].append(node)
    removed = sorted(set(baseline['collected'])-set(final['collected']))
    changed_old_test_files = [name for name, digest in baseline['test_source_sha256'].items() if final['test_source_sha256'].get(name) != digest]
    result = dict(counts={k:len(v) for k,v in groups.items()}, nodes=groups,
        collection_errors=final['collection_errors'], removed_old_nodes=removed, changed_old_test_files=changed_old_test_files,
        full_suite_counts=final['counts'], baseline_suite_counts=baseline['counts'], comparison_method='IDENTICAL_FROZEN_NODE_IDS',
        final_gate_file=final_name)
    write('test_gate_comparison.json', result)
    return result


if __name__ == '__main__':
    inventory, magic = source_inventory()
    result = compare_tests()
    print(json.dumps(dict(files_scanned=len(inventory), legacy_magic_occurrences=len(magic), tests=result['counts'])))
