"""Real-Redis, no-model/no-SQL lifecycle check for Round 5.12.

The parent starts two fresh Python processes against one explicit test
deployment namespace, then deletes only the two exact keys it created.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from redis.asyncio import Redis

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.domain.semantic_scope import AuthorizedSemanticScope
from app.semantic_v2.authorized_contract import (
    AuthorizedScopeContext,
    CatalogPinIdentity,
    ScopedArtifact,
    contract_digest,
)
from app.semantic_v2 import models as semantic_models
from app.semantic_v2.persisted_scalar_api import RedisScalarSessionStore
from app.semantic_v2.state_machine import ConversationState, TaskState, TaskVersion, TopicState


def context(run_id: str) -> AuthorizedScopeContext:
    return AuthorizedScopeContext(
        authorized_scope=AuthorizedSemanticScope(
            semantic_model_id=81,
            business_domain_ids=[205],
            scope_mode="EXPLICIT_DOMAINS",
        ),
        state_namespace=contract_digest(["round512", run_id]),
        catalog_pin=CatalogPinIdentity(
            catalog_version="round512-test-catalog",
            vector_index_version="round512-test-vector",
            catalog_publish_id="round512-test-publication",
            activation_id="round512-test-activation",
            target_identity_hash="round512-test-target",
        ),
    )


def identity(run_id: str) -> dict:
    return {
        "tenant_id": "round512-test",
        "user_id": "round512-test",
        "application_id": "round512-test",
        "conversation_id": "round512-" + run_id,
    }


def store(settings: Settings, run_id: str, redis) -> RedisScalarSessionStore:
    deployment_id = "round512test-" + run_id
    return RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2-limited-scalar:" + deployment_id,
        deployment_id=deployment_id,
        ttl_seconds=300,
        idempotency_ttl_seconds=600,
    )


async def stage(name: str, run_id: str) -> dict:
    settings = Settings()
    url = settings.effective_redis_url()
    if not url:
        raise RuntimeError("REDIS_CONFIGURATION_UNAVAILABLE")
    redis = Redis.from_url(url, decode_responses=True, socket_timeout=10,
                           socket_connect_timeout=10)
    target = store(settings, run_id, redis)
    ctx, state_identity = context(run_id), identity(run_id)
    try:
        snapshot = await target.load(ctx, state_identity)
        if name == "write":
            now = datetime.now(timezone.utc)
            task_id, topic_id = "round512-task", "round512-topic"
            state = ConversationState(
                **state_identity,
                state_version=1,
                active_topic_id=topic_id,
                topic_stack=[topic_id],
                recent_turn_ids=["round512-message"],
                topics={topic_id: TopicState(
                    topic_id=topic_id, title="Round 5.12 lifecycle check",
                    active_task_id=task_id, task_ids=[task_id], last_accessed_at=now,
                )},
                tasks={task_id: TaskState(
                    task_id=task_id, topic_id=topic_id, active_version=1,
                    status="RESOLVED", versions=[TaskVersion(
                        version=1, status="RESOLVED", plan_id="round512-plan",
                        created_at=now,
                    )],
                )},
            )
            state_payload = state.model_dump(mode="json")
            planned = ScopedArtifact(
                kind="CONVERSATION", context=ctx, payload=state_payload,
                payload_digest=contract_digest(state_payload),
            )
            plan_payload = {"plan_id": "round512-plan", "test_only": True}
            plan = ScopedArtifact(
                kind="LAST_REQUEST", context=ctx, payload=plan_payload,
                payload_digest=contract_digest(plan_payload),
            )
            attempt = semantic_models.ExecutionAttemptRecord(
                execution_id="round512-test-running", task_id=task_id,
                task_version=1, attempt_number=1, status="RUNNING",
                started_at=now, execution_backend="SEMANTIC_QUERY",
                snapshot_id="round512-test-pending",
                catalog_version=ctx.catalog_pin.catalog_version,
                vector_index_version=ctx.catalog_pin.vector_index_version,
                semantic_model_version=ctx.catalog_pin.catalog_version,
                policy_version="round512-test", asl_digest="round512-asl",
                sql_digest="round512-sql",
            )
            snapshot = await target.begin(
                snapshot, planned_state=planned, plan_state=plan, attempt=attempt,
                message_id="round512-message",
                request_fingerprint=contract_digest([run_id, "request"]),
                context=ctx, state_identity=state_identity, started_at=now,
            )
            guard_key = target._guard_key(snapshot.key, "round512-message")
            return {"stage": name, "state_version": snapshot.state_version,
                    "message_status": snapshot.message("round512-message")["status"],
                    "key_hashes": [contract_digest(snapshot.key), contract_digest(guard_key)]}
        if name == "read":
            record = snapshot.message("round512-message")
            if not record or record["status"] != "RUNNING":
                raise RuntimeError("CROSS_PROCESS_RESTORE_FAILED")
            snapshot = await target.require_operator_review(
                snapshot, message_id="round512-message",
                request_fingerprint=record["request_fingerprint"],
                reason="process exited before SQL submission in lifecycle test",
                operator="round512-test-runner", context=ctx,
                state_identity=state_identity, observed_at=datetime.now(timezone.utc),
            )
            record = snapshot.message("round512-message")
            return {"stage": name, "state_version": snapshot.state_version,
                    "message_status": record["status"], "schema": target.schema_version}
        state_key = target._key(ctx, state_identity)
        guard_key = target._guard_key(state_key, "round512-message")
        if name == "verify":
            record = snapshot.message("round512-message")
            if not record or record["status"] != "REVIEW_REQUIRED":
                raise RuntimeError("REVIEW_STATE_RESTORE_FAILED")
            updated = await target.require_operator_review(
                snapshot, message_id="round512-message",
                request_fingerprint=record["request_fingerprint"],
                reason="confirm revision CAS in lifecycle test",
                operator="round512-test-runner", context=ctx,
                state_identity=state_identity, observed_at=datetime.now(timezone.utc),
            )
            cas_rejected = False
            try:
                await target.require_operator_review(
                    snapshot, message_id="round512-message",
                    request_fingerprint=record["request_fingerprint"],
                    reason="stale snapshot must not overwrite",
                    operator="round512-test-runner", context=ctx,
                    state_identity=state_identity, observed_at=datetime.now(timezone.utc),
                )
            except Exception as exc:
                cas_rejected = "changed concurrently" in str(exc)
            if not cas_rejected:
                raise RuntimeError("STALE_ENVELOPE_REVISION_ACCEPTED")
            guard_before = await target.idempotency_record(updated, "round512-message")
            deleted_state = int(await redis.delete(state_key))
            expired = await target.load(ctx, state_identity)
            guard_after = await target.idempotency_record(expired, "round512-message")
            if not guard_before or not guard_after:
                raise RuntimeError("IDEMPOTENCY_GUARD_DID_NOT_OUTLIVE_SESSION")

            incompatible_identity = {**state_identity,
                                     "conversation_id": state_identity["conversation_id"] + "-incompatible"}
            incompatible_key = target._key(ctx, incompatible_identity)
            await redis.set(incompatible_key, json.dumps({
                "schema_version": "future-session-v9", "state_version": 0,
                "revision": 0,
            }), ex=300)
            incompatible_rejected = False
            try:
                await target.load(ctx, incompatible_identity)
            except ValueError as exc:
                incompatible_rejected = str(exc) == "PERSISTED_SESSION_CONTRACT_INVALID"
            deleted_incompatible = int(await redis.delete(incompatible_key))
            if not incompatible_rejected:
                raise RuntimeError("INCOMPATIBLE_ENVELOPE_ACCEPTED")
            return {
                "stage": name,
                "revision_after_review": updated.revision,
                "stale_cas_rejected": cas_rejected,
                "guard_survived_session_expiry": True,
                "incompatible_envelope_rejected": True,
                "exact_keys_deleted": deleted_state + deleted_incompatible,
                "scan_used": False,
                "flush_used": False,
            }
        before = [bool(await redis.exists(key)) for key in (state_key, guard_key)]
        deleted = await redis.delete(state_key, guard_key)
        after = [bool(await redis.exists(key)) for key in (state_key, guard_key)]
        return {"stage": name, "resources_before": sum(before),
                "deleted": int(deleted), "resources_after": sum(after),
                "scan_used": False, "flush_used": False}
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", choices=("all", "write", "read", "verify", "cleanup"),
                        default="all")
    args = parser.parse_args()
    if args.stage != "all":
        print(json.dumps(asyncio.run(stage(args.stage, args.run_id)), ensure_ascii=False))
        return
    receipts = []
    try:
        for child_stage in ("write", "read"):
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--run-id", args.run_id,
                 "--stage", child_stage], cwd=ROOT, capture_output=True, text=True,
                timeout=30, check=True,
            )
            receipts.append(json.loads(completed.stdout))
        receipts.append(asyncio.run(stage("verify", args.run_id)))
    finally:
        receipts.append(asyncio.run(stage("cleanup", args.run_id)))
    print(json.dumps({"status": "PASS", "processes": 2, "receipts": receipts,
                      "model_calls": 0, "business_sql_calls": 0},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
