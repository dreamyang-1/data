from __future__ import annotations

import json
from uuid import uuid4

import httpx


def main() -> None:
    conversation_id = f"dealer-followup-{uuid4().hex[:8]}"
    questions = [
        "查询上海市江苏苏云品牌低值耗材的经销商清单，并显示各经销商含税销售总额。",
        "那上海米财灵商贸有限公司的最近一个月的含税销售总额是多少",
        "那2025年12月份的含税销售总额是多少",
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
                "http_status": response.status_code,
                "status": payload.get("status"),
                "intent": payload.get("intent"),
                "missing_slots": payload.get("missing_slots"),
                "answer": payload.get("answer"),
                "understood_slots": payload.get("understood_slots"),
                "analysis_process": payload.get("analysis_process"),
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
