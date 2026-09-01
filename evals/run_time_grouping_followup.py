from __future__ import annotations

import json
from uuid import uuid4

import httpx


def main() -> None:
    conversation_id = f"time-grouping-{uuid4().hex[:8]}"
    questions = [
        "查询最近一年上海地区的含税销售总额。",
        "按月统计",
        "改成按季度。",
    ]
    with httpx.Client(timeout=300) as client:
        for turn, question in enumerate(questions, 1):
            response = client.post(
                "http://127.0.0.1:8088/agent_chat",
                json={
                    "application_id": "27",
                    "conversation_id": conversation_id,
                    "message_id": f"m{turn}-{uuid4().hex[:8]}",
                    "question": question,
                    "semantic_model_id": 81,
                    "business_domain_ids": [],
                    "knowledge_base_names": [],
                    "history": [],
                    "use_longterm_memory": True,
                },
            )
            payload = response.json()
            print(json.dumps({
                "turn": turn,
                "status": payload.get("status"),
                "intent": payload.get("intent"),
                "missing_slots": payload.get("missing_slots"),
                "answer": payload.get("answer"),
                "chart_count": len(payload.get("chart_specs") or []),
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
