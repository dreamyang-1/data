"""Verify the imported Oagnet snapshot and run its small, isolated offline suite.

Run from any directory:
    python docs/oagnet_import/verify_snapshot.py --output verification.json
No services, models, database migrations or firewall scripts are started.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    project = root / 'Oagnet'
    manifest = json.loads((Path(__file__).parent / 'source_manifest.json').read_text(encoding='utf-8'))
    compiled = 0
    for item in manifest['files']:
        data = (root / item['repository_path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != item['published_sha256']:
            raise RuntimeError('Snapshot hash mismatch: ' + item['repository_path'])
        if item['repository_path'].endswith('.py'):
            compile(data, item['repository_path'], 'exec', dont_inherit=True)
            compiled += 1

    # Install the guard before importing pytest, config or application modules.
    network_attempts = []

    def deny_network(event, _arguments):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.sendto'}:
            network_attempts.append(event)
            raise RuntimeError('Network access is disabled for snapshot verification')

    sys.addaudithook(deny_network)
    sys.dont_write_bytecode = True
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    for name in list(os.environ):
        if name.startswith(('OAGNET_', 'MYSQL_', 'REDIS_', 'MILVUS_', 'LLM_',
                            'EMBEDDING_', 'DASHSCOPE_', 'DAILY_JOB_')):
            os.environ.pop(name)
    sys.path.insert(0, str(project))
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    import pytest

    class Results:
        def __init__(self):
            self.passed = 0
            self.failed = 0
            self.skipped = 0
            self.collection_errors = 0
            self.setup_teardown_errors = 0

        def pytest_runtest_logreport(self, report):
            if report.when == 'call':
                self.passed += int(report.passed)
                self.failed += int(report.failed)
                self.skipped += int(report.skipped)
            elif report.failed:
                self.setup_teardown_errors += 1

        def pytest_collectreport(self, report):
            self.collection_errors += int(report.failed)

    results = Results()
    selected = ['tests/test_capacity_control.py', 'tests/test_daily_job_store.py']
    exit_code = pytest.main([
        '-q', '-c', os.devnull, '--rootdir', str(project), '-p', 'no:cacheprovider',
        *[str(project / name) for name in selected],
    ], plugins=[results])
    report = {
        'snapshot_hashes_checked': len(manifest['files']),
        'python_files_compiled': compiled,
        'selected_test_files': selected,
        'passed': results.passed,
        'failed': results.failed,
        'skipped': results.skipped,
        'collection_errors': results.collection_errors,
        'setup_teardown_errors': results.setup_teardown_errors,
        'network_attempts': len(network_attempts),
        'real_model_calls': 0,
        'external_production_writes': 0,
        'pytest_exit_code': int(exit_code),
        'python_version': sys.version.split()[0],
        'pytest_version': pytest.__version__,
        'scope': 'Import integrity, syntax and selected offline unit tests; not full integration acceptance',
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8', newline='\n')
    print(encoded)
    return int(exit_code) or int(bool(network_attempts))


if __name__ == '__main__':
    raise SystemExit(main())
