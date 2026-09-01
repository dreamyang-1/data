from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx


LABELS = [
    "商品名称", "商品分类", "品牌", "生产厂家", "经销商",
    "供应商", "医院", "科室", "地区", "公司",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8031/entities")
    args = parser.parse_args()
    artifact: dict[str, Any] = json.loads(args.artifact.read_text(encoding="utf-8"))
    questions = [
        (scenario["name"], turn["turn"], turn["question"])
        for scenario in artifact["scenarios"] for turn in scenario["turns"]
    ]
    results = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for scenario, turn, question in questions:
            response = await client.post(args.url, json={
                "text": question, "labels": LABELS, "threshold": 0.55,
            })
            response.raise_for_status()
            entities = response.json()["entities"]
            results.append({
                "scenario": scenario, "turn": turn, "question": question,
                "entities": entities,
            })
    labels = Counter(
        entity["label"] for result in results for entity in result["entities"]
    )
    with_candidates = [result for result in results if result["entities"]]
    print(json.dumps({
        "question_count": len(results),
        "questions_with_candidates": len(with_candidates),
        "question_coverage": round(len(with_candidates) / len(results), 4),
        "candidate_count": sum(len(result["entities"]) for result in results),
        "label_counts": labels,
        "examples": with_candidates[:12],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
