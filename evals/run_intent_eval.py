from __future__ import annotations

import asyncio
import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from app.config import Settings
from app.domain.models import TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier
from app.intent.structured import StructuredIntentModelClient
from app.intent.structured import HybridIntentClassifier


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("model", "rule", "hybrid"), default="model")
    parser.add_argument(
        "--dataset",
        nargs="+",
        default=["intent_smoke.json"],
        help="one or more JSON datasets under evals/, or absolute paths",
    )
    args = parser.parse_args()
    dataset_paths: list[Path] = []
    cases: list[dict] = []
    for raw_path in args.dataset:
        dataset_path = Path(raw_path)
        if not dataset_path.is_absolute():
            dataset_path = Path(__file__).parent / dataset_path
        dataset_paths.append(dataset_path)
        loaded = json.loads(dataset_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(f"evaluation dataset must be a JSON array: {dataset_path}")
        cases.extend(loaded)
    if not cases:
        raise ValueError("evaluation dataset must not be empty")
    required = {"question", "expected"}
    if any(not required.issubset(case) for case in cases):
        raise ValueError("every evaluation case must contain question and expected")
    case_ids = [case.get("id") for case in cases]
    if any(not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("every evaluation case must have a unique, non-empty id")
    settings = Settings()
    client = StructuredIntentModelClient(settings)
    rules = RuleBasedIntentClassifier()
    hybrid = HybridIntentClassifier(settings, rules=rules, model_client=client)
    semaphore = asyncio.Semaphore(4)

    async def run(case: dict) -> dict:
        queued_at = time.perf_counter()
        try:
            if args.mode == "rule":
                started = time.perf_counter()
                output = rules.classify(
                    case["question"], TrustedIdentity(tenant_id="eval", user_id="eval"), "eval"
                )
                predicted = output.primary_intent.value
                confidence = 1.0
                intent_source = "RULE"
                assumptions: list[str] = []
            else:
                async with semaphore:
                    started = time.perf_counter()
                    if args.mode == "model":
                        output = await client.classify(case["question"])
                        confidence = output.confidence
                        intent_source = "STRUCTURED_MODEL"
                        assumptions = []
                    else:
                        output = await hybrid.classify(
                            case["question"],
                            TrustedIdentity(tenant_id="eval", user_id="eval"),
                            f"eval-{case['id']}",
                        )
                        confidence = output.intent_confidence
                        intent_source = output.intent_source
                        assumptions = list(output.assumptions)
                predicted = output.primary_intent.value
            completed_at = time.perf_counter()
            return {
                **case,
                "predicted": predicted,
                "confidence": confidence,
                "intent_source": intent_source,
                "assumptions": assumptions,
                "schema_valid": True,
                "latency_ms": round((completed_at - started) * 1000, 1),
                "queue_ms": round((started - queued_at) * 1000, 1),
                "correct": predicted == case["expected"],
                "error": None,
            }
        except Exception as exc:  # evaluation records provider/schema failures by type only
            completed_at = time.perf_counter()
            started = locals().get("started", queued_at)
            return {
                **case,
                "predicted": None,
                "confidence": None,
                "intent_source": None,
                "assumptions": [],
                "schema_valid": False,
                "latency_ms": round((completed_at - started) * 1000, 1),
                "queue_ms": round((started - queued_at) * 1000, 1),
                "correct": False,
                "error": type(exc).__name__,
            }

    results = await asyncio.gather(*(run(case) for case in cases))
    latencies = [item["latency_ms"] for item in results]
    sorted_latencies = sorted(latencies)
    p95_index = min(len(sorted_latencies) - 1, int(len(sorted_latencies) * 0.95))
    correct = sum(item["correct"] for item in results)
    valid = sum(item["schema_valid"] for item in results)
    error_counts = Counter(item["error"] for item in results if item["error"])
    routing_counts = Counter(
        item["intent_source"] for item in results if item["intent_source"]
    )
    fallback_counts = Counter(
        assumption
        for item in results
        for assumption in item["assumptions"]
        if "FALLBACK" in assumption or "UNAVAILABLE" in assumption
    )
    confusion = Counter(
        (item["expected"], item["predicted"])
        for item in results
        if item["expected"] != item["predicted"]
    )
    expected_counts = Counter(item["expected"] for item in results)
    correct_counts = Counter(
        item["expected"] for item in results if item["correct"]
    )
    predicted_counts = Counter(
        item["predicted"] for item in results if item["predicted"] is not None
    )
    true_positive = Counter(
        item["expected"]
        for item in results
        if item["correct"] and item["predicted"] is not None
    )
    per_intent = []
    for intent in sorted(expected_counts):
        recall = correct_counts[intent] / expected_counts[intent]
        precision = (
            true_positive[intent] / predicted_counts[intent]
            if predicted_counts[intent]
            else 0.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_intent.append(
            {
                "intent": intent,
                "support": expected_counts[intent],
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            }
        )
    summary = {
        "mode": args.mode,
        "model": settings.intent_model_name if args.mode != "rule" else "rule-based",
        "datasets": [str(path) for path in dataset_paths],
        "total": len(results),
        # When no provider call produced a valid schema there was no model
        # evaluation.  Report null instead of the misleading "0% accuracy".
        "execution_valid": valid > 0,
        "accuracy": round(correct / len(results), 4) if valid else None,
        "macro_f1": (
            round(sum(item["f1"] for item in per_intent) / len(per_intent), 4)
            if valid
            else None
        ),
        "schema_valid_rate": round(valid / len(results), 4),
        "error_counts": dict(sorted(error_counts.items())),
        "routing_counts": dict(sorted(routing_counts.items())),
        "fallback_counts": dict(sorted(fallback_counts.items())),
        "latency_ms_p50": round(statistics.median(latencies), 1),
        "latency_ms_p95": sorted_latencies[p95_index],
        "latency_ms_max": max(latencies),
        "confusions": [
            {"expected": expected, "predicted": predicted, "count": count}
            for (expected, predicted), count in confusion.items()
        ],
        "per_intent": per_intent,
        "failures": [item for item in results if not item["correct"]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
