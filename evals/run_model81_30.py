from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOC = ROOT / "语义模型81查询测试问题.md"


def load_questions(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    questions = [
        (int(match.group(1)), match.group(2).strip())
        for match in re.finditer(r"(?m)^(\d+)\.\s+(.+?)\s*$", text)
    ]
    questions = [item for item in questions if 1 <= item[0] <= 30]
    if [number for number, _ in questions] != list(range(1, 31)):
        raise RuntimeError("测试文档必须包含连续编号1至30的测试问题")
    return questions


def validate(payload: Any, status_code: int | None, error: str | None) -> list[str]:
    issues: list[str] = []
    if error:
        return [error]
    if status_code != 200:
        issues.append(f"HTTP_{status_code}")
    if not isinstance(payload, dict):
        return [*issues, "响应不是JSON对象"]
    if payload.get("status") != "COMPLETED":
        issues.append(f"状态不是COMPLETED:{payload.get('status')}")
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        issues.append("回答为空")
    evidence = payload.get("evidence") or []
    kinds = {
        str(item.get("kind") or "")
        for item in evidence
        if isinstance(item, dict)
    }
    if not any(kind in kinds for kind in ("QUERY_RESULT", "DATA_QUERY_RESULT", "DATASET")):
        issues.append("缺少QUERY_RESULT证据")
    if payload.get("missing_slots"):
        issues.append("仍有missing_slots")
    if any(marker in answer for marker in (
        "智能体配置错误", "ASL 转 SQL 服务未能生成", "需要补充", "请确认",
    )):
        issues.append("回答包含失败或澄清话术")
    return issues


async def run_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    number: int,
    question: str,
) -> dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        payload: Any = None
        status_code: int | None = None
        error: str | None = None
        try:
            response = await client.post(
                f"{base_url.rstrip('/')}/agent_chat",
                json={
                    "application_id": "27",
                    "conversation_id": f"model81-q{number:02d}-{uuid4().hex[:8]}",
                    "message_id": f"q{number:02d}-{uuid4().hex[:10]}",
                    "question": question,
                    "semantic_model_id": 81,
                    "business_domain_ids": [],
                    "knowledge_base_names": [],
                    "history": [],
                    "use_longterm_memory": True,
                },
            )
            status_code = response.status_code
            payload = response.json()
        except Exception as exc:  # pragma: no cover - live harness
            error = f"{type(exc).__name__}: {exc}"
        elapsed = round(time.perf_counter() - started, 3)
        issues = validate(payload, status_code, error)
        result = {
            "number": number,
            "question": question,
            "http_status": status_code,
            "status": payload.get("status") if isinstance(payload, dict) else None,
            "intent": payload.get("intent") if isinstance(payload, dict) else None,
            "missing_slots": payload.get("missing_slots", []) if isinstance(payload, dict) else [],
            "answer": str(payload.get("answer") or "") if isinstance(payload, dict) else "",
            "evidence_kinds": [
                item.get("kind") for item in (payload.get("evidence") or [])
                if isinstance(item, dict)
            ] if isinstance(payload, dict) else [],
            "files": payload.get("files", []) if isinstance(payload, dict) else [],
            "elapsed_seconds": elapsed,
            "issues": issues,
        }
        print(json.dumps({
            "number": number,
            "status": result["status"],
            "intent": result["intent"],
            "elapsed": elapsed,
            "issues": issues,
        }, ensure_ascii=False), flush=True)
        return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--only", default="", help="逗号分隔的问题编号")
    args = parser.parse_args()
    questions = load_questions(args.doc)
    if args.only:
        selected = {int(value) for value in args.only.split(",") if value.strip()}
        questions = [item for item in questions if item[0] in selected]
    timeout = httpx.Timeout(300.0, connect=10.0)
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(*(
            run_one(client, semaphore, args.base_url, number, question)
            for number, question in questions
        ))
    elapsed = [item["elapsed_seconds"] for item in results]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "question_count": len(results),
        "passed": sum(not item["issues"] for item in results),
        "failed": sum(bool(item["issues"]) for item in results),
        "completed": sum(item["status"] == "COMPLETED" for item in results),
        "latency_mean": round(statistics.mean(elapsed), 3) if elapsed else 0,
        "latency_max": max(elapsed, default=0),
    }
    artifact = {"summary": summary, "results": results}
    output = args.output or ROOT / "evals" / "results" / f"model81-30-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
