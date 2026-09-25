"""Sync only an explicit reviewed batch, preserving initial drift and unrelated edits."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = Path('E:/yy')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sync(paths, manifest_name):
    initial = json.loads((ROOT / 'docs/phase25_1/preexisting_workspace_drift.json').read_text(encoding='utf-8'))
    baseline = {r['path']: r for r in initial['files']}
    planned = []
    for relative in paths:
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('relative workspace path required')
        if any(re.search(r'(^\.env|credential|secret|__pycache__|\.git$|cache|backup|\.log$)', p, re.I) for p in path.parts):
            raise ValueError('sensitive or excluded file path')
        source, target = ROOT / path, REPO / path
        source.resolve().relative_to(ROOT.resolve())
        target.resolve().relative_to(REPO.resolve())
        if not source.is_file():
            raise ValueError('deletions require a separately reviewed explicit operation')
        if relative in baseline and baseline[relative]['classification'] != 'MATCH':
            raise ValueError('PREEXISTING_WORKSPACE_DRIFT: refusing to overwrite ' + relative)
        git_status = subprocess.check_output(['git', '-C', str(REPO), 'status', '--porcelain', '--', relative]).decode()
        if git_status and sha(source) != sha(target):
            raise ValueError('uncommitted repository edits on ' + relative)
        if relative not in baseline and target.exists():
            tracked = subprocess.run(['git', '-C', str(REPO), 'ls-files', '--error-unmatch', '--', relative], capture_output=True)
            if tracked.returncode and sha(source) != sha(target):
                raise ValueError('unrelated untracked repository file')
        content = source.read_text(encoding='utf-8')
        if re.search(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)', content):
            raise ValueError('possible credential content in ' + relative)
        planned.append(dict(path=relative, sha256=sha(source), previous_git_sha256=sha(target),
                            change='changed' if target.exists() else 'added'))
    for entry in planned:
        target = REPO / entry['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry['path'], target)
        if sha(target) != entry['sha256']:
            raise RuntimeError('post-copy SHA-256 mismatch')
    manifest = dict(changed_files=[e['path'] for e in planned if e['change'] == 'changed'],
                    added_files=[e['path'] for e in planned if e['change'] == 'added'], deleted_files=[],
                    files=planned, sensitive_scan='PASS', preexisting_drift_preserved=True)
    manifest_path = ROOT / 'docs/phase25_1' / manifest_name
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    destination = REPO / manifest_path.relative_to(ROOT)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_path, destination)
    print(json.dumps({'files_synced': len(planned), 'manifest': str(manifest_path.relative_to(ROOT))}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--files', type=Path, required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    sync(json.loads(args.files.read_text(encoding='utf-8')), args.manifest)
