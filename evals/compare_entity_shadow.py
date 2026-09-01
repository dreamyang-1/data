from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any


def turns(artifact: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (scenario["name"], int(turn["turn"])): turn
        for scenario in artifact["scenarios"]
        for turn in scenario["turns"]
    }


def normalized_answer(value: str) -> str:
    value = re.sub(r"https?://[^)\s]+", "<DOWNLOAD_URL>", value)
    return re.sub(r"\s+", " ", value).strip()


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * ratio) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("shadow", type=Path)
    parser.add_argument("off", type=Path)
    args = parser.parse_args()
    shadow = turns(json.loads(args.shadow.read_text(encoding="utf-8")))
    off = turns(json.loads(args.off.read_text(encoding="utf-8")))
    keys = sorted(shadow.keys() & off.keys())
    deltas = [
        float(shadow[key]["elapsed_seconds"]) - float(off[key]["elapsed_seconds"])
        for key in keys
    ]
    status_changes = [
        {
            "scenario": key[0], "turn": key[1], "question": shadow[key]["question"],
            "off": off[key]["status"], "shadow": shadow[key]["status"],
        }
        for key in keys if shadow[key]["status"] != off[key]["status"]
    ]
    answer_changes = [
        {
            "scenario": key[0], "turn": key[1], "question": shadow[key]["question"],
            "same_status": shadow[key]["status"] == off[key]["status"],
        }
        for key in keys
        if normalized_answer(str(shadow[key]["answer"]))
        != normalized_answer(str(off[key]["answer"]))
    ]
    result = {
        "paired_turns": len(keys),
        "http_status_equal": sum(
            shadow[key]["http_status"] == off[key]["http_status"] for key in keys
        ),
        "status_equal": sum(shadow[key]["status"] == off[key]["status"] for key in keys),
        "intent_equal": sum(shadow[key]["intent"] == off[key]["intent"] for key in keys),
        "missing_slots_equal": sum(
            shadow[key]["missing_slots"] == off[key]["missing_slots"] for key in keys
        ),
        "normalized_answer_equal": len(keys) - len(answer_changes),
        "shadow_minus_off_seconds": {
            "mean": round(statistics.mean(deltas), 3),
            "median": round(statistics.median(deltas), 3),
            "p95": round(percentile(deltas, 0.95), 3),
            "faster_count": sum(delta < 0 for delta in deltas),
            "slower_count": sum(delta > 0 for delta in deltas),
        },
        "status_changes": status_changes,
        "answer_change_count": len(answer_changes),
        "answer_changes": answer_changes,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
