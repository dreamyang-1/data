"""Run the canonical new-topic/follow-up leakage regression against port 8088.

The script is intentionally read-only with respect to business data.  It sends
two chat turns, then reads the bounded session event log and active task frame
to print one auditable JSON record.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.dependencies import build_container


async def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    conversation_id = f"context-leakage-regression-{stamp}"
    common = {
        "application_id": "data-analysis",
        "conversation_id": conversation_id,
        "semantic_model_id": 81,
    }
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8088", timeout=240
    ) as client:
        turn_1 = await client.post(
            "/agent_chat",
            json={
                **common,
                "message_id": f"m1-{stamp}",
                "question": "查询空心纤维血液透析器产品合作的经销商名单。",
            },
        )
        turn_1.raise_for_status()
        turn_2_message = f"m2-{stamp}"
        turn_2 = await client.post(
            "/agent_chat",
            json={
                **common,
                "message_id": turn_2_message,
                "question": "按月分析外周插管中心静脉导管的销售趋势。",
            },
        )
        turn_2.raise_for_status()

    first = turn_1.json()
    second = turn_2.json()
    container = build_container(get_settings())
    events = await container.events.list_events(
        "default-tenant",
        "default-user",
        "data-analysis",
        conversation_id,
        limit=200,
    )
    selected_events = [
        {
            "event_type": event.event_type.value,
            "payload": event.payload,
        }
        for event in events
        if event.message_id == turn_2_message
        and event.event_type.value
        in {
            "TURN_ADMISSION",
            "CONTEXT_MERGE",
            "SEMANTIC_CONTEXT",
            "SEMANTIC_PLAN",
            "VALIDATION_RESULT",
            "TOOL_RESULT",
            "FINAL_INSIGHT",
        }
    ]
    task_frame = await container.sessions.get_task_frame(
        "default-tenant", "default-user", "data-analysis", conversation_id
    )
    last_request = await container.sessions.get_last_request(
        "default-tenant", "default-user", "data-analysis", conversation_id
    )
    result = {
        "conversation_id": conversation_id,
        "turn_1": {
            "status": first.get("status"),
            "intent": first.get("intent"),
            "answer": first.get("answer"),
        },
        "turn_2": {
            "status": second.get("status"),
            "intent": second.get("intent"),
            "answer": second.get("answer"),
            "skills_used": second.get("skills_used"),
            "reliability": second.get("reliability"),
        },
        "task_frame": (
            task_frame.model_dump(mode="json") if task_frame is not None else None
        ),
        "last_request": (
            last_request.model_dump(mode="json")
            if last_request is not None
            else None
        ),
        "turn_2_events": selected_events,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    redis = getattr(container.sessions, "redis", None)
    if redis is not None:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
