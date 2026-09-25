"""Read-only repository audit; never synchronize or overwrite existing files."""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
REPOSITORY = Path('E:/yy')


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git(*args: str) -> str:
    return subprocess.check_output(['git', '-C', str(REPOSITORY), *args]).decode('utf-8')


def audit() -> dict:
    entries = []
    tracked = git('ls-files', '-z').split('\0')
    for relative in filter(None, tracked):
        workspace_hash, git_hash = digest(WORKSPACE / relative), digest(REPOSITORY / relative)
        baseline = subprocess.check_output(['git', '-C', str(REPOSITORY), 'show', f'HEAD:{relative}'])
        baseline_hash = hashlib.sha256(baseline).hexdigest()
        if workspace_hash is None:
            classification = 'MISSING_IN_WORKSPACE'
        elif git_hash is None:
            classification = 'MISSING_IN_GIT'
        elif workspace_hash == git_hash:
            classification = 'MATCH'
        elif git_hash == baseline_hash:
            classification = 'WORKSPACE_ONLY_DIFFERENCE'
        elif workspace_hash == baseline_hash:
            classification = 'GIT_ONLY_DIFFERENCE'
        else:
            # Git checkout line endings may differ from blob bytes. Record both hashes.
            classification = 'WORKSPACE_ONLY_DIFFERENCE' if not git('status', '--porcelain', '--', relative) else 'GIT_ONLY_DIFFERENCE'
        entries.append(dict(path=relative, classification=classification,
                            marker='PREEXISTING_WORKSPACE_DRIFT' if classification != 'MATCH' else None,
                            workspace_sha256=workspace_hash, git_sha256=git_hash, head_blob_sha256=baseline_hash))
    return dict(baseline_commit=git('rev-parse', 'HEAD').strip(), branch=git('branch', '--show-current').strip(),
                remote=git('remote', 'get-url', 'origin').strip(), initial_git_status=git('status', '--porcelain'),
                counts=dict(Counter(e['classification'] for e in entries)), files=entries)


if __name__ == '__main__':
    target = WORKSPACE / 'docs/phase25_1/preexisting_workspace_drift.json'
    if target.exists():
        raise SystemExit('Baseline already exists; refusing to replace it')
    report = audit()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'baseline': report['baseline_commit'], 'counts': report['counts'],
                      'drift': [e['path'] for e in report['files'] if e['classification'] != 'MATCH']}, ensure_ascii=False))
