"""Freeze and compare identical offline pytest node IDs without live services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class GateRecorder:
    def __init__(self, output: Path):
        self.output = output
        self.collected = []
        self.results = {}
        self.collection_errors = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed or report.skipped:
            if self.results.get(report.nodeid) != 'failed':
                self.results[report.nodeid] = report.outcome

    def pytest_sessionfinish(self, session, exitstatus):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(dict(
            mode='OFFLINE_MOCK_ONLY', environment='dotenv disabled; network denied',
            exit_code=int(exitstatus), collected=self.collected, results=self.results,
            collection_errors=self.collection_errors, counts=dict(Counter(self.results.values())),
            test_source_sha256={str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((ROOT / 'tests').glob('test_*.py'))},
        ), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('pytest_args', nargs='*')
    options, remainder = parser.parse_known_args()
    if options.output.exists():
        raise SystemExit('Refusing to overwrite frozen gate result')
    os.chdir(ROOT)
    os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'
    os.environ['DATA_AGENT_BUSINESS_QUESTION_COLLECTION_ENABLED'] = 'false'
    # Disable credential discovery, including aliases, before importing tests.
    from app.config import Settings
    Settings.model_config['env_file'] = None
    socketpair_active = False
    original_pair = socket.socketpair

    def pair(*args, **kwargs):
        nonlocal socketpair_active
        socketpair_active = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            socketpair_active = False

    socket.socketpair = pair

    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo'} and not socketpair_active:
            raise OSError('PHASE25_1_OFFLINE_NETWORK_DENIED')

    sys.addaudithook(audit)
    import pytest
    return pytest.main([*options.pytest_args, *remainder], plugins=[GateRecorder(options.output)])


if __name__ == '__main__':
    raise SystemExit(main())
