"""Offline Legacy evidence runner with a fixed business clock and failure text."""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--node')
    opts, remaining = parser.parse_known_args()
    if opts.output.exists():
        raise SystemExit('Refusing to overwrite frozen evidence')
    os.chdir(ROOT)
    os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'
    os.environ['DATA_AGENT_BUSINESS_QUESTION_COLLECTION_ENABLED'] = 'false'
    from app.config import Settings
    Settings.model_config['env_file'] = None
    pair_active = False
    original = socket.socketpair

    def socketpair(*a, **kw):
        nonlocal pair_active
        pair_active = True
        try:
            return original(*a, **kw)
        finally:
            pair_active = False

    socket.socketpair = socketpair

    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo'} and not pair_active:
            raise OSError('PHASE0_OFFLINE_NETWORK_DENIED')

    sys.addaudithook(audit)
    import pytest
    from tools.phase25_1.run_gate import GateRecorder

    class Evidence(GateRecorder):
        def __init__(self, output):
            super().__init__(output)
            self.failures = {}
            self.clarification_coverage = {'public_clarification_responses': 0, 'with_reason_trace': 0, 'untraced': 0, 'unsafe_asks': 0}

        @pytest.fixture(autouse=True)
        def business_clock(self, monkeypatch):
            import app.intent.classifier as classifier

            class FixedDate(date):
                @classmethod
                def today(cls):
                    return cls(2026, 9, 7)

            monkeypatch.setattr(classifier, 'date', FixedDate)
            from app.services.orchestrator import DataAnalysisOrchestrator
            delegate = DataAnalysisOrchestrator._ensure_clarification_trace
            async def record_trace(agent, response, chat):
                await delegate(agent, response, chat)
                if response.status == 'NEEDS_CLARIFICATION' or response.clarification_questions:
                    counts = self.clarification_coverage
                    counts['public_clarification_responses'] += 1
                    traces = response.clarification_decision_traces
                    counts['with_reason_trace' if traces and all(t.reason_type for t in traces) else 'untraced'] += 1
                    asks = [t for t in traces if t.decision == 'ASK']
                    if not asks or any(not t.is_user_ambiguity or t.already_asked or t.safe_default_available or t.system_repair_possible for t in asks):
                        counts['unsafe_asks'] += 1
            monkeypatch.setattr(DataAnalysisOrchestrator, '_ensure_clarification_trace', record_trace)

        def pytest_runtest_logreport(self, report):
            super().pytest_runtest_logreport(report)
            if report.failed:
                self.failures[report.nodeid] = str(report.longrepr)

        def pytest_sessionfinish(self, session, exitstatus):
            super().pytest_sessionfinish(session, exitstatus)
            d = json.loads(self.output.read_text(encoding='utf-8'))
            d.update(failures=self.failures, business_clock='2026-09-07 Asia/Shanghai; classifier.date monkeypatch seam',
                     real_model_calls=0, production_external_writes=0, clarification_coverage=self.clarification_coverage)
            self.output.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')

    return pytest.main(([opts.node] if opts.node else []) + (remaining or ['-q']), plugins=[Evidence(opts.output)])


if __name__ == '__main__':
    raise SystemExit(main())
