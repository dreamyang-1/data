"""Repeat each failing node in independent offline processes before edits."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/phase0b'
baseline = json.loads((ROOT / 'docs/phase0a/test_gate.json').read_text(encoding='utf-8'))
nodes = [n for n, v in baseline['results'].items() if v == 'failed']
rows = []
for i, node in enumerate(nodes):
    outcomes = []
    evidence = []
    for run in range(1, 4):
        if run == 3 and len(set(outcomes)) == 1:
            break
        path = OUT / 'independent_runs' / f'failure-{i+1:02d}-run-{run}.json'
        if not path.exists():
            subprocess.run([sys.executable, '-X', 'utf8', str(ROOT/'tools/phase0/run_offline.py'),
                            '--output', str(path), '--node', node, '-q'], cwd=ROOT, capture_output=True)
        result = json.loads(path.read_text(encoding='utf-8'))
        outcomes.append(result['results'].get(node, 'collection_error'))
        evidence.append(path.relative_to(ROOT).as_posix())
    rows.append(dict(nodeid=node, outcomes=outcomes, repeatability='DETERMINISTIC' if len(set(outcomes))==1 else 'FLAKY', evidence=evidence))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/'repeatability.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n',encoding='utf-8',newline='\n')
    print(f'{i+1}/{len(nodes)} {rows[-1]["repeatability"]}',flush=True)
