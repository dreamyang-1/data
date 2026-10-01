"""Offline regression: allow only ephemeral HTTP servers created by tests."""
import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import socket
import sys
import threading
import types

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    opts, selected = parser.parse_known_args()
    if opts.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    sys.dont_write_bytecode = True
    listeners = []
    state = threading.local()
    original_pair = socket.socketpair

    def pair(*args, **kwargs):
        state.socketpair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            state.socketpair = False

    socket.socketpair = pair

    def allowed(address):
        for listener in listeners:
            try:
                if address[:2] == listener.getsockname()[:2]:
                    return True
            except OSError:
                pass
        return False

    def audit(event, args):
        if getattr(state, 'socketpair', False):
            return
        if event == 'socket.bind':
            address = args[1]
            if isinstance(address, tuple) and address[:2] == ('127.0.0.1', 0):
                listeners.append(args[0])
            else:
                raise OSError('OFFLINE_NON_TEST_BIND_DENIED')
        if event == 'socket.connect' and not allowed(args[1]):
            raise OSError('OFFLINE_NETWORK_DENIED')
        if event == 'socket.getaddrinfo' and not allowed(args[:2]):
            raise OSError('OFFLINE_DNS_DENIED')

    sys.addaudithook(audit)
    # runtime_config loads the workspace .env at import. Keep its helper
    # definitions, but suppress that import-time call and subsequent loads.
    config_path = ROOT / 'runtime_config.py'
    tree = ast.parse(config_path.read_bytes(), filename=str(config_path))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'load_workspace_env')]
    config = types.ModuleType('runtime_config')
    config.__file__ = str(config_path)
    exec(compile(tree, str(config_path), 'exec'), config.__dict__)
    config.load_workspace_env = lambda: None
    sys.modules['runtime_config'] = config
    import pytest

    class Evidence:
        def __init__(self):
            self.results, self.failures, self.collection_errors = {}, {}, []

        def pytest_runtest_logreport(self, report):
            if report.when == 'call' or report.failed or report.skipped:
                if self.results.get(report.nodeid) != 'failed':
                    self.results[report.nodeid] = report.outcome
            if report.failed:
                self.failures[report.nodeid] = str(report.longrepr)

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_errors.append(str(report.longrepr))

    evidence = Evidence()
    status = pytest.main(['-c', os.devnull, '--rootdir', str(ROOT), '-p', 'no:cacheprovider',
                         *(selected or [str(p) for p in sorted(ROOT.glob('test_*.py'))]),
                         '-q', '--tb=short'], plugins=[evidence])
    data = dict(results=evidence.results, failures=evidence.failures,
                collection_errors=evidence.collection_errors,
                counts=dict(Counter(evidence.results.values())),
                pytest_exit_code=int(status), real_model_calls=0, production_external_writes=0,
                mode='Only ephemeral loopback HTTP servers created by this test process are allowed; workspace .env loading disabled')
    opts.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    return int(status)


if __name__ == '__main__':
    raise SystemExit(main())
