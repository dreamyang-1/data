"""Exact, non-public inspection/quarantine command for limited-scalar sessions.

The default action is read-only.  The only mutation marks an unresolved
message for operator review; it never submits SQL or manufactures success.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json

from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.semantic_v2.limited_scalar_runtime import build_limited_scalar_handler


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--action", choices=("status", "require-review"), default="status")
    result.add_argument("--tenant-id", required=True)
    result.add_argument("--user-id", required=True)
    result.add_argument("--application-id", required=True)
    result.add_argument("--conversation-id", required=True)
    result.add_argument("--message-id", required=True)
    result.add_argument("--semantic-model-id", required=True, type=int)
    result.add_argument("--business-domain-id", required=True, type=int, action="append")
    result.add_argument("--authorize-mutation", action="store_true")
    result.add_argument("--reason")
    result.add_argument("--operator")
    return result


def _bounded_record(record: dict | None) -> dict:
    if record is None:
        return {"found": False}
    return {
        "found": True,
        "status": record.get("status"),
        "execution_stage": record.get("execution_stage"),
        "started_at": record.get("started_at"),
        "last_observed_at": record.get("last_observed_at"),
        "execution_id": record.get("execution_id"),
        "has_response": record.get("response") is not None,
        "has_validated_result": record.get("result") is not None,
        "review": record.get("review"),
    }


async def run(args) -> dict:
    settings = Settings()
    if (args.semantic_model_id != settings.limited_scalar_semantic_model_id
            or sorted(args.business_domain_id)
                != sorted(settings.limited_scalar_business_domain_ids)):
        raise SystemExit("requested scope does not match the configured deployment scope")
    handler = build_limited_scalar_handler(settings)
    try:
        chat = ChatRequest(
            semantic_model_id=args.semantic_model_id,
            business_domain_ids=args.business_domain_id,
            application_id=args.application_id,
            conversation_id=args.conversation_id,
            message_id=args.message_id,
            question="operator session status lookup",
        )
        identity = TrustedIdentity(tenant_id=args.tenant_id, user_id=args.user_id)
        context = handler.context_resolver(chat, identity)
        state_identity = handler._identity(chat, identity)
        snapshot = await handler.store.load(context, state_identity)
        record = snapshot.message(args.message_id)
        if args.action == "require-review":
            if not args.authorize_mutation:
                raise SystemExit("--authorize-mutation is required")
            if not args.reason or not args.operator:
                raise SystemExit("--reason and --operator are required")
            if record is None:
                raise SystemExit("message not found")
            snapshot = await handler.store.require_operator_review(
                snapshot,
                message_id=args.message_id,
                request_fingerprint=record["request_fingerprint"],
                reason=args.reason,
                operator=args.operator,
                context=context,
                state_identity=state_identity,
                observed_at=datetime.now(timezone.utc),
            )
            record = snapshot.message(args.message_id)
        return {
            "action": args.action,
            "scope": {
                "semantic_model_id": args.semantic_model_id,
                "business_domain_ids": sorted(args.business_domain_id),
            },
            "identity": {
                "tenant_id": args.tenant_id,
                "user_id": args.user_id,
                "application_id": args.application_id,
                "conversation_id": args.conversation_id,
                "message_id": args.message_id,
            },
            "store_schema": handler.store.schema_version,
            "state_version": snapshot.state_version,
            "message": _bounded_record(record),
            "sql_submitted_by_command": False,
        }
    finally:
        await handler.aclose()


def main() -> None:
    args = parser().parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
