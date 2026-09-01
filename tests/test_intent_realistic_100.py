from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.models import PrimaryIntent, TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier


DATASET = Path(__file__).parents[1] / "evals" / "intent_realistic_100.json"
CASES = json.loads(DATASET.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_realistic_primary_intent_regression(case: dict[str, str]) -> None:
    request = RuleBasedIntentClassifier().classify(
        case["question"],
        TrustedIdentity(tenant_id="intent-eval", user_id="intent-eval"),
        conversation_id=f"intent-eval-{case['id']}",
    )

    assert request.primary_intent == PrimaryIntent(case["expected"])

