"""Explicit file reconciliation and byte-level commit reproducibility evidence."""
from __future__ import annotations

import csv
import hashlib
import json
import io
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = Path('E:/yy')
OUT = ROOT / 'docs/phase0a'
START = '6c1c6e5242a4b42b302e141f66b5886632aa21ec'
SECRET = re.compile(rb'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args])


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def write_csv(path, rows):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})


def tracked():
    return git('ls-files', '-z').decode('utf-8').strip('\0').split('\0')


def commit_files(commit=START):
    with zipfile.ZipFile(io.BytesIO(git('-c', 'core.autocrlf=false', 'archive', '--format=zip', commit))) as archive:
        return {p: archive.read(p) for p in archive.namelist() if not p.endswith('/')}


def initialize():
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / 'preexisting_drift_inventory.json'
    if target.exists():
        raise SystemExit('Frozen inventory already exists')
    historical = json.loads((ROOT / 'docs/phase25_1/preexisting_workspace_drift.json').read_text(encoding='utf-8'))
    history = {r['path']: r for r in historical['files']}
    rows = []
    blobs = commit_files()
    for p in tracked():
        wh, gh = sha(ROOT / p), sha(REPO / p)
        blob = hashlib.sha256(blobs[p]).hexdigest()
        category = ('MISSING_IN_WORKSPACE' if wh is None else 'MISSING_IN_GIT' if gh is None
                    else 'MATCH' if wh == gh else 'GIT_ONLY_DIFFERENCE' if wh == blob else 'WORKSPACE_ONLY_DIFFERENCE')
        rows.append(dict(path=p, classification=category, workspace_hash=wh, git_hash=gh,
                         commit_blob_hash=blob, historical_classification=history.get(p, {}).get('classification')))
    write_json(target, dict(starting_commit=START, counts=dict(Counter(r['classification'] for r in rows)), files=rows))
    write_csv(OUT / 'preexisting_drift_inventory.csv', rows)
    summaries = {
        'app/adapters/semantic_query.py': 'Existing internal semantic context snapshot attached before retrieval.',
        'app/analysis/synthesis.py': 'Existing optional bounded context argument used by already tracked callers.',
        'app/domain/models.py': 'Existing evidence/runtime model definitions required by already tracked services and tests.',
        'app/stores/session.py': 'Existing scoped validated-query store methods; no method behavior edited in Phase 0A.',
        'AGENTS.md': 'Repository instructions absent in workspace; parent instructions also apply.',
        'CODE_REVIEW_ISSUES.md': 'Workspace retained a credential in a quotation; Git version already redacts it.',
        '实际业务问题.md': 'Append-only runtime business-question collector output with conversation/message identifiers.',
    }
    disposition = []
    for r in rows:
        if r['classification'] == 'MATCH':
            continue
        p = r['path']
        source = p.endswith('.py')
        runtime = p == '实际业务问题.md'
        disposition.append(dict(path=p, workspace_hash=r['workspace_hash'], git_hash=r['git_hash'],
            disposition='SOURCE_CODE_SHOULD_SYNC' if source else 'RUNTIME_FILE_SHOULD_NOT_TRACK' if runtime else 'FORMAL_DOC_SHOULD_SYNC',
            difference_summary=summaries[p], origin='PREEXISTING_WORKSPACE_DRIFT recorded before Phase 2.5.1; original author/time unknown',
            authoritative_version='WORKSPACE_FROZEN_TESTED_BYTES' if source else 'LOCAL_RUNTIME_ONLY' if runtime else START,
            reason='Preserve tested development behavior and make its dependencies reproducible.' if source else summaries[p],
            should_be_tracked=not runtime,
            action='SYNC_EXACT_WORKSPACE_BYTES' if source else 'REMOVE_INDEX_ONLY_AND_IGNORE' if runtime else 'COPY_REVIEWED_GIT_DOCUMENT_TO_WORKSPACE',
            evidence=['docs/phase25_1/preexisting_workspace_drift.json', 'docs/phase25_1/phase25_1_closure_report.md', 'docs/phase0a/test_gate.json'],
            status='PLANNED'))
    write_json(OUT / 'drift_disposition.json', disposition)
    write_csv(OUT / 'drift_disposition.csv', disposition)
    requests = ['preexisting_workspace_drift.json', 'workspace_git_hash_verification.json', 'git_baseline.json',
                'git_commit_manifest.json', 'change_manifest.json', 'rollback_manifest.json', 'phase25_1_closure_report.md']
    equivalents = {
        'workspace_git_hash_verification.json': ['final_workspace_git_drift.json'],
        'git_baseline.json': ['preexisting_workspace_drift.json', 'current_code_baseline_audit.md'],
        'git_commit_manifest.json': ['change_manifest_implementation.json', 'change_manifest_closure.json'],
        'change_manifest.json': ['change_manifest_implementation.json', 'change_manifest_closure.json'],
        'rollback_manifest.json': ['contract_boundaries_and_rollback.md'],
    }
    evidence = []
    for name in requests:
        p = ROOT / 'docs/phase25_1' / name
        files = [name] if p.exists() else equivalents[name]
        # Read complete artifacts; never substitute historical counts for current verification.
        for f in files:
            raw = (p.parent / f).read_text(encoding='utf-8')
            if f.endswith('.json'):
                json.loads(raw)
        evidence.append(dict(requested=name, status='READ' if p.exists() else 'MISSING_IN_STARTING_COMMIT_AND_WORKSPACE',
                             available_evidence=files))
    write_json(OUT / 'phase25_1_evidence_index.json', evidence)
    print(json.dumps({'tracked': len(rows), 'counts': dict(Counter(r['classification'] for r in rows))}))


def align():
    rows = json.loads((OUT / 'drift_disposition.json').read_text(encoding='utf-8'))
    for r in rows:
        p = r['path']
        if sha(ROOT / p) != r['workspace_hash'] or sha(REPO / p) != r['git_hash']:
            raise ValueError('Concurrent edit: ' + p)
        if r['action'] == 'COPY_REVIEWED_GIT_DOCUMENT_TO_WORKSPACE':
            (ROOT / p).write_bytes((REPO / p).read_bytes())
    # The old Git checkout used CRLF while commit blobs use LF. Pin checkout
    # attributes and align only files whose difference is proven to be CRLF.
    normalized = []
    blobs = commit_files()
    for p in tracked():
        if p == '实际业务问题.md':
            continue
        b = (ROOT / p).read_bytes()
        blob = blobs[p]
        if b != blob and b.replace(b'\r\n', b'\n') == blob:
            (ROOT / p).write_bytes(blob)
            normalized.append(p)
    write_json(OUT / 'line_ending_alignment.json', dict(policy='* text=auto eol=lf', paths=normalized,
        evidence='Each listed workspace file differed from the starting commit only by CRLF -> LF; semantic content unchanged.'))
    print(json.dumps({'reviewed_drift': len(rows), 'line_endings_aligned': len(normalized)}))


def sync_manifest(paths, manifest_path):
    """Only reviewed explicit paths; reject unrelated Git edits and credentials."""
    if len(paths) != len(set(paths)):
        raise ValueError('Duplicate path')
    plan = []
    dirty = {entry[3:] for entry in git('status', '--porcelain', '-z', '--untracked-files=all').decode('utf-8').split('\0') if entry}
    for p in paths:
        rel = Path(p)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Invalid relative path')
        source, target = ROOT / p, REPO / p
        source.resolve().relative_to(ROOT.resolve())
        target.resolve().relative_to(REPO.resolve())
        if (rel.name == '实际业务问题.md' or any(part.startswith('.env') and part not in {'.env.example', '.env.template'} for part in rel.parts)
                or any(part in {'.git', '__pycache__', '_backups', 'logs'} for part in rel.parts) or rel.suffix in {'.log', '.bak'}):
            raise ValueError('Excluded path: ' + p)
        b = source.read_bytes()
        if SECRET.search(b):
            raise ValueError('Credential pattern: ' + p)
        if p in dirty and target.exists() and sha(source) != sha(target):
            raise ValueError('Unrelated Git edit: ' + p)
        plan.append(dict(path=p, workspace_hash=sha(source), previous_git_hash=sha(target)))
    for r in plan:
        target = REPO / r['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / r['path']).read_bytes())
        assert sha(target) == r['workspace_hash']
    write_json(manifest_path, dict(files=plan, credential_scan='PASS', synchronization='EXPLICIT_PATHS_ONLY'))
    print(json.dumps({'synced': len(plan)}))


if __name__ == '__main__':
    import sys
    {'initialize': initialize, 'align': align}[sys.argv[1]]()
