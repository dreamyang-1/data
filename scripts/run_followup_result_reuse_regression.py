"""Run the conversation-aware time/result-reuse regression on port 8088."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.dependencies import build_container


async def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    conversation_id = f"followup-result-reuse-{stamp}"
    common = {
        "application_id": "data-analysis",
        "conversation_id": conversation_id,
        "semantic_model_id": 81,
    }
    turn_2_message = f"m2-{stamp}"
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8088", timeout=240
    ) as client:
        first_response = await client.post(
            "/agent_chat",
            json={
                **common,
                "message_id": f"m1-{stamp}",
                "question": "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势。",
            },
        )
        first_response.raise_for_status()
        second_response = await client.post(
            "/agent_chat",
            json={
                **common,
                "message_id": turn_2_message,
                "question": "11月较10月下降多少？",
            },
        )
        second_response.raise_for_status()

    container = build_container(get_settings())
    events = await container.event_store.list_events(
        "default-tenant",
        "default-user",
        "data-analysis",
        conversation_id,
        limit=200,
    )
    second_events = [
        {
            "event_type": event.event_type.value,
            "payload": event.payload,
        }
        for event in events
        if event.message_id == turn_2_message
        and event.event_type.value in {
            "TURN_ADMISSION",
            "CONTEXT_MERGE",
            "QUERY_RESOLUTION",
            "TOOL_CALL",
            "TOOL_RESULT",
            "FINAL_INSIGHT",
        }
    ]
    task_frame = await container.sessions.get_task_frame(
        "default-tenant", "default-user", "data-analysis", conversation_id
    )
    payload = {
        "conversation_id": conversation_id,
        "turn_1": first_response.json(),
        "turn_2": second_response.json(),
        "turn_2_sql_tool_calls": sum(
            event["event_type"] == "TOOL_CALL"
            and event["payload"].get("tool_name") == "intelligent_semantic_query"
            for event in second_events
        ),
        "active_task_frame": (
            task_frame.model_dump(mode="json") if task_frame else None
        ),
        "turn_2_events": second_events,
    }
    if "--summary" in sys.argv:
        resolution = next(
            (
                event["payload"] for event in second_events
                if event["event_type"] == "QUERY_RESOLUTION"
            ),
            {},
        )
        payload = {
            "conversation_id": conversation_id,
            "turn_1_status": first_response.json().get("status"),
            "turn_2_status": second_response.json().get("status"),
            "turn_2_intent": second_response.json().get("intent"),
            "turn_2_answer": second_response.json().get("answer"),
            "turn_2_sql_tool_calls": payload["turn_2_sql_tool_calls"],
            "query_resolution": resolution,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    redis = getattr(container.sessions, "redis", None)
    if redis is not None:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
