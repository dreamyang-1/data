"""Run the complete suite in serial processes when host memory is constrained.

Each child uses the established offline runner and fixed clock. No node is
skipped; the aggregate records the exact partition and all outcome evidence.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = ROOT / args.output
    if out.exists():
        raise SystemExit('Refusing to overwrite evidence')
    out.mkdir(parents=True)
    files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'tests').glob('test_*.py'))
    # Six modules bound assertion rewriting and imported test fixture memory.
    batches = [files[i:i+6] for i in range(0,len(files),6)]
    aggregate = dict(mode='SERIAL_MODULE_BATCHES', reason='Host MemoryError during full-process pytest; no tests deselected',
        batches=[], results={}, failures={}, collection_errors=[], counts={}, real_model_calls=0,production_external_writes=0)
    for index, modules in enumerate(batches,1):
        evidence = out / f'batch_{index:03}.json'
        command = [sys.executable, str(ROOT/'tools/phase0/run_offline.py'),'--output',str(evidence),*modules,'-q','--tb=no']
        run = subprocess.run(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        if run.returncode not in {0,1} or not evidence.exists():
            (out/f'aborted_{index:03}.txt').write_bytes(run.stdout)
            raise SystemExit(f'Batch {index} incomplete; exit {run.returncode}; no PASS aggregate written')
        data = json.loads(evidence.read_text(encoding='utf8'))
        duplicate = aggregate['results'].keys() & data['results'].keys()
        if duplicate:
            raise SystemExit(f'Duplicate node IDs: {duplicate}')
        aggregate['results'].update(data['results'])
        aggregate['failures'].update(data['failures'])
        aggregate['collection_errors'].extend(data['collection_errors'])
        aggregate['batches'].append(dict(modules=modules,evidence=evidence.relative_to(ROOT).as_posix(),
            sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),exit_code=run.returncode,counts=data['counts']))
        print(f'Batch {index}/{len(batches)}: {data["counts"]}',flush=True)
    aggregate['counts'] = dict(Counter(aggregate['results'].values()))
    aggregate['collected'] = len(aggregate['results'])
    (out/'aggregate.json').write_text(json.dumps(aggregate,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    print('Complete:',aggregate['counts'],flush=True)


if __name__ == '__main__':
    main()
