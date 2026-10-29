"""Run Oagnet tests without connecting to deployed services or loading .env."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import socket
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args, selected = parser.parse_known_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['ANONYMIZED_TELEMETRY'] = 'False'
    sys.dont_write_bytecode = True
    pair_state = threading.local()
    original_pair = socket.socketpair

    def socketpair(*a, **kw):
        pair_state.active = True
        try:
            return original_pair(*a, **kw)
        finally:
            pair_state.active = False

    socket.socketpair = socketpair

    def audit(event, _args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'} and not getattr(pair_state, 'active', False):
            raise OSError('OFFLINE_NETWORK_DENIED')

    sys.addaudithook(audit)
    import dotenv
    dotenv.load_dotenv = lambda *a, **kw: False
    import vector_store

    class UnavailableStore:
        """A test must explicitly supply its own fake before accessing a store."""
        def search(self, *a, **kw):
            raise RuntimeError('OFFLINE_VECTOR_STORE_REQUIRES_TEST_DOUBLE')

        find_exact = search
        get_by_where = search
        count = search

    factory = vector_store.create_vector_store
    vector_store.create_vector_store = lambda: UnavailableStore()
    try:
        import agent
        import api
    finally:
        vector_store.create_vector_store = factory
    # Existing tests replace create_deep_agent, so model construction is also
    # a transport seam. A real agent cannot execute with this sentinel model.
    agent._get_chat_model = lambda: object()
    # gRPC can open native sockets without a Python socket audit event.
    # Block its production client constructor; tests use their own fake clients.
    import pymilvus

    def deny_milvus(*a, **kw):
        raise RuntimeError('OFFLINE_MILVUS_REQUIRES_TEST_DOUBLE')

    pymilvus.MilvusClient = deny_milvus
    import pytest

    class Evidence:
        def __init__(self):
            self.results = {}
            self.failures = {}
            self.collection_errors = []

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_errors.append(str(report.longrepr))

        def pytest_runtest_logreport(self, report):
            if report.when == 'call' or report.failed or report.skipped:
                if self.results.get(report.nodeid) != 'failed':
                    self.results[report.nodeid] = report.outcome
            if report.failed:
                self.failures[report.nodeid] = str(report.longrepr)

    evidence = Evidence()
    exit_code = pytest.main(['-c', os.devnull, '--rootdir', str(ROOT), '-p', 'no:cacheprovider',
                            *(selected or ['tests', '-q', '--tb=short'])], plugins=[evidence])
    data = dict(results=evidence.results, failures=evidence.failures,
                collection_errors=evidence.collection_errors,
                counts=dict(Counter(evidence.results.values())),
                collected=len(evidence.results), pytest_exit_code=int(exit_code),
                real_model_calls=0, production_external_writes=0,
                mode='OFFLINE_NETWORK_DENIED; import-time vector client replaced; native Milvus client disabled')
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return int(exit_code)


if __name__ == '__main__':
    raise SystemExit(main())
