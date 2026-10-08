"""One-time explicit scope migration for previously passing state fixtures."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
before = json.loads((ROOT/'docs/phase0b/final_verified_test_gate.json').read_text(encoding='utf8'))
current = json.loads((ROOT/'docs/phase0c/initial_test_gate.json').read_text(encoding='utf8'))
targets = {n.split('::')[1].split('[')[0] for n in current['failures'] if n not in before['failures']}
changes = []
for name in ['test_orchestrator.py', 'test_question_rewriter.py']:
    path = ROOT/'tests'/name
    source = path.read_bytes()
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1]+len(line))
    edits = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name not in targets:
            continue
        scope = 81
        domains = []
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        for call in calls:
            if isinstance(call.func, ast.Attribute) and call.func.attr in {'rewrite', '_apply_context'}:
                fields = {k.arg:k.value for k in call.keywords}
                if isinstance(fields.get('semantic_model_id'), ast.Constant):
                    scope = fields['semantic_model_id'].value
                if 'business_domain_ids' in fields:
                    domains = ast.literal_eval(fields['business_domain_ids']) or []
                elif isinstance(fields.get('business_domain_id'), ast.Constant):
                    domains = [fields['business_domain_id'].value] if fields['business_domain_id'].value else []
                break
        for call in calls:
            if not isinstance(call.func, ast.Name) or call.func.id != 'CanonicalAnalysisRequest':
                continue
            fields = {k.arg for k in call.keywords}
            additions = []
            if 'semantic_model_id' not in fields:
                additions.append(f'semantic_model_id={scope},')
            if domains and 'business_domain_ids' not in fields:
                additions.append(f'business_domain_ids={domains!r},')
            if additions:
                position = starts[call.lineno-1]+call.col_offset+len('CanonicalAnalysisRequest(')
                edits.append((position, ' '.join(additions).encode()))
                changes.append({'path': 'tests/'+name, 'test': fn.name, 'explicit_fields': additions,
                                'reason': 'Fixture state belongs to the current backend model/domain; business assertions unchanged.'})
    for position, insertion in sorted(edits, reverse=True):
        source = source[:position]+insertion+source[position:]
    path.write_bytes(source)
(ROOT/'docs/phase0c/state_fixture_migration.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
print('Migrated',len(changes),'state fixtures')
