"""Bounded Round 5.11 persisted scalar API acceptance runner.

Raw SQL, parameters, result values and model exchanges are written only to an
explicit private directory.  Every live phase uses the existing /agent_chat or
/agent_chat/stream route in an isolated ASGI app and a run-specific Redis
namespace.  Existing listening services are never changed.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic
from unittest.mock import patch

from dotenv import dotenv_values
from fastapi.testclient import TestClient
import pymysql
from pymysql.cursors import DictCursor
from pydantic import SecretStr
from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = ROOT.parent / "Oagnet"
SQL_ROOT = ROOT.parent / "sql-translator"
sys.path[:0] = [str(ROOT), str(SERVICE_ROOT), str(SQL_ROOT)]

from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity
from app.main import create_app
from app.semantic_v2.authorized_contract import contract_digest
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.persisted_scalar_api import (
    PersistedScalarApiHandler,
    PersistedScalarPlan,
    RedisScalarSessionStore,
    exact_result_payload,
    restore_exact_result,
)
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.time_storage import TimeStorageContract
from app.semantic_v2.isolated_execution import prepare_execution
from bound_sql import statement_fingerprint, validate_bound_sql
from pinned_catalog import translate_pinned_catalog
from semantic_scope import RequestScope
from sql_translator_prod import SQLTranslatorProd
from tools.cutover import live_followup_round5_1 as native
from tools.cutover.round53_live_recheck import sources


CLOCK = "2026-09-09T09:00:00+08:00"
SCOPE = {"semantic_model_id": 81, "business_domain_ids": [205]}
APPLICATION = "round511-isolated-api"
IDENTITY = TrustedIdentity(tenant_id="evaluation", user_id="evaluation")
TOKEN = "round511-isolated-backend-token"
LIMITS = {"model": 12, "sql": 12, "metadata": 8}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode()).hexdigest()


def atomic_write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
                         encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if digest(loaded) != digest(value):
        raise ValueError("EVIDENCE_ATOMIC_WRITE_VERIFICATION_FAILED")
    return sha256(path.read_bytes()).hexdigest()


def redis_config():
    values = dotenv_values(ROOT.parent / ".env")
    required = ("REDIS_HOST", "REDIS_PORT", "REDIS_DB")
    if any(values.get(key) in (None, "") for key in required):
        raise ValueError("REDIS_CONFIGURATION_UNAVAILABLE")
    return {
        "host": values["REDIS_HOST"],
        "port": int(values["REDIS_PORT"]),
        "db": int(str(values["REDIS_DB"]).split()[0]),
        "password": values.get("REDIS_PASSWORD") or None,
        "decode_responses": True,
        "socket_timeout": 10,
        "socket_connect_timeout": 10,
        "protocol": 2,
    }


class SharedBudget:
    SCRIPT = """
local raw = redis.call('GET', KEYS[1])
local value = raw and cjson.decode(raw) or {model=0,sql=0,metadata=0}
local name = ARGV[1]
local limit = tonumber(ARGV[2])
if tonumber(value[name]) >= limit then return -1 end
value[name] = tonumber(value[name]) + 1
redis.call('SET', KEYS[1], cjson.encode(value), 'EX', ARGV[3])
return tonumber(value[name])
"""

    def __init__(self, redis, key, ttl=7200):
        self.redis, self.key, self.ttl = redis, key, ttl

    def initialize(self):
        if not self.redis.set(self.key, json.dumps({"model": 0, "sql": 0, "metadata": 0}), ex=self.ttl, nx=True):
            raise ValueError("ROUND511_BUDGET_ALREADY_EXISTS")

    def reserve(self, name):
        if name not in LIMITS:
            raise ValueError("ROUND511_BUDGET_KIND_INVALID")
        result = int(self.redis.eval(self.SCRIPT, 1, self.key, name, LIMITS[name], self.ttl))
        if result < 0:
            raise ValueError(f"ROUND511_{name.upper()}_BUDGET_EXHAUSTED")
        return result

    def snapshot(self):
        raw = self.redis.get(self.key)
        return json.loads(raw) if raw else None


class BudgetedModel:
    def __init__(self, delegate, budget):
        self.delegate, self.budget = delegate, budget

    def begin_turn(self, *args, **kwargs):
        return self.delegate.begin_turn(*args, **kwargs)

    async def complete(self, *args, **kwargs):
        await asyncio.to_thread(self.budget.reserve, "model")
        return await self.delegate.complete(*args, **kwargs)

    @property
    def calls(self):
        return self.delegate.calls

    @property
    def exchanges(self):
        return self.delegate.exchanges

    @property
    def upstream(self):
        return self.delegate.upstream


def sql_planner(pin, scope, asl, **policy):
    request_scope = RequestScope.from_request({
        "authorized_semantic_scope": scope.model_dump(mode="json")
    })
    return translate_pinned_catalog(pin, request_scope, asl, **policy)


def load_frozen(run_activation):
    catalog = native.read(ROOT / "docs/cutover/evaluation_gates/frozen_catalog.json")
    raw_path = ROOT / ".eval_private/harness-20260909T083000Z/catalog_snapshot.json"
    raw = raw_path.read_bytes()
    # These are the exact frozen source artifacts recorded by Round 5.10.
    source_root = ROOT / ".eval_private/grounding-round5-3-20260910T052350Z"
    snapshot_path = source_root / "live_catalog_snapshot.json"
    observations_path = source_root / "source_observations.json"
    snapshot = native.read(snapshot_path)
    observations = native.read(observations_path)
    if sha256(raw).hexdigest() != catalog["source_snapshot_sha256"] or digest(snapshot) != digest(json.loads(raw)):
        raise ValueError("FROZEN_CURRENT_CATALOG_MISMATCH")
    denied = []
    publication = native.frozen_publication(
        raw,
        catalog,
        denied,
        native_source_values=True,
        recorded_activation_id=run_activation,
    )
    return catalog, raw, snapshot, observations, publication, denied, snapshot_path, observations_path


def time_evidence(session, plan):
    if plan.payload.time is None:
        return None
    fields = [
        field
        for table in session._pin.snapshot["physical_catalog"]["tables"]
        for field in table["fields"]
        if field["field_id"] == 24400
    ]
    if len(fields) != 1:
        raise ValueError("CURRENT_TIME_FIELD_EVIDENCE_MISMATCH")
    return TimeStorageContract(
        evidence_version="round59-user-declaration-beijing-v1",
        provenance="DECLARED",
        evidence_reference="USER_DECLARATION_ROUND59_BEIJING",
        context=session.context,
        field_canonical_id=plan.payload.time.anchor.canonical_id,
        field_mapping="sales_order.created_date",
        physical_field_id=24400,
        table_id=1880,
        data_source_id=58,
        physical_field_digest=digest(fields[0]),
        storage_timezone="Asia/Shanghai",
        storage_semantics="LOCAL_WALL_DATETIME",
        fractional_seconds_precision=0,
        applicability=plan.payload.time.range,
    )


def expected_case(question):
    if question == "查询去年江苏省订单笔数":
        return "COUNT_2025"
    if question == "换今年":
        return "COUNT_2026"
    if question == "查询含税销售总额":
        return "AMOUNT_ALL_TIME"
    raise ValueError("ROUND511_QUESTION_NOT_AUTHORIZED")


def validate_plan(kind, plan, prepared):
    sql = prepared.sql_receipt
    if str(sql.get("data_source_id")) != "58":
        raise ValueError("UNAUTHORIZED_DATA_SOURCE")
    SQLTranslatorProd.validate_read_only_sql(sql["sql"])
    if not sql["sql"].rstrip().endswith("LIMIT 1"):
        raise ValueError("SCALAR_LIMIT_CONTRACT_MISSING")
    metrics = [value.canonical_code for value in plan.payload.measures]
    if kind == "AMOUNT_ALL_TIME":
        if metrics != ["sales_total_including_tax"] or sql.get("sql_parameters") not in (None, {}):
            raise ValueError("AMOUNT_PLAN_MISMATCH")
        if SQLTranslatorProd._sql_involved_tables(sql["sql"]) != {"sales_order"}:
            raise ValueError("AMOUNT_TABLE_SCOPE_MISMATCH")
        if "SUM(sales_order.amount_with_tax)" not in sql["sql"]:
            raise ValueError("AMOUNT_FORMULA_MISMATCH")
    else:
        year = 2025 if kind == "COUNT_2025" else 2026
        if metrics != ["order_count"]:
            raise ValueError("COUNT_METRIC_MISMATCH")
        if SQLTranslatorProd._sql_involved_tables(sql["sql"]) != {"sales_order", "hospital", "dim_province"}:
            raise ValueError("COUNT_TABLE_SCOPE_MISMATCH")
        values = list((sql.get("sql_parameters") or {}).values())
        if values != ["江苏省", f"{year}-01-01 00:00:00", f"{year + 1}-01-01 00:00:00"]:
            raise ValueError("COUNT_PARAMETER_MISMATCH")
        if "COUNT(DISTINCT sales_order.order_key)" not in sql["sql"]:
            raise ValueError("COUNT_FORMULA_MISMATCH")
    return {
        "kind": kind,
        "task_id": plan.task_id,
        "task_version": plan.task_version,
        "plan_id": plan.plan_id,
        "sql_digest": digest(sql["sql"]),
        "parameter_digest": digest(sql.get("sql_parameters")),
    }


def reference_query(kind):
    if kind == "AMOUNT_ALL_TIME":
        return (
            "SELECT SUM(so.amount_with_tax) AS reference_value FROM sales_order AS so",
            None,
        )
    year = 2025 if kind == "COUNT_2025" else 2026
    return (
        "SELECT COUNT(DISTINCT so.order_key) AS reference_value FROM sales_order AS so "
        "INNER JOIN hospital AS h ON h.hospital_id = so.hospital_id "
        "INNER JOIN dim_province AS p ON p.province_id = h.province_id "
        "WHERE p.province_name = %(v2_p0)s AND so.created_date >= %(v2_p1)s "
        "AND so.created_date < %(v2_p2)s",
        {
            "v2_p0": "江苏省",
            "v2_p1": f"{year}-01-01 00:00:00",
            "v2_p2": f"{year + 1}-01-01 00:00:00",
        },
    )


class SameSnapshotTransport:
    def __init__(self, *, source, budget, output, kind):
        self.source, self.budget, self.output, self.kind = source, budget, output, kind
        self.calls = 0

    def _run(self, request):
        self.calls += 1
        self.budget.reserve("sql")
        self.budget.reserve("sql")
        main_sql = SQLTranslatorProd.validate_read_only_sql(request.sql)
        main_parameters = dict(request.parameters) if request.parameters is not None else None
        if main_parameters is not None or request.parameter_fingerprint is not None:
            validate_bound_sql(main_sql, main_parameters)
            if request.parameter_fingerprint != statement_fingerprint(main_sql, main_parameters):
                raise ValueError("SQL_PARAMETER_CONTRACT_MISMATCH")
        reference_sql, reference_parameters = reference_query(self.kind)
        reference_sql = SQLTranslatorProd.validate_read_only_sql(reference_sql)
        if reference_parameters is not None:
            validate_bound_sql(reference_sql, reference_parameters)
        connection = None
        main_submitted = reference_submitted = False
        started = monotonic()
        try:
            connection = pymysql.connect(
                host=self.source.get("host", "").split(":")[0],
                port=int(self.source.get("port") or 3306),
                user=self.source.get("username", ""),
                password=self.source.get("password", ""),
                database=self.source.get("db_name", ""),
                charset="utf8mb4",
                cursorclass=DictCursor,
                connect_timeout=10,
                read_timeout=35,
                write_timeout=10,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = 30000")
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                cursor.execute("SELECT UTC_TIMESTAMP(6) AS data_as_of")
                snapshot_time = (cursor.fetchone() or {}).get("data_as_of")
                if not isinstance(snapshot_time, datetime):
                    raise ValueError("READ_ONLY_SNAPSHOT_REQUIRED")
                main_submitted = True
                cursor.execute(main_sql, main_parameters) if main_parameters is not None else cursor.execute(main_sql)
                main_rows = cursor.fetchall()
                main_columns = [item[0] for item in cursor.description]
                reference_submitted = True
                cursor.execute(reference_sql, reference_parameters) if reference_parameters is not None else cursor.execute(reference_sql)
                reference_rows = cursor.fetchall()
                if len(main_rows) != 1 or len(reference_rows) != 1 or len(main_columns) != 1:
                    raise ValueError("INDEPENDENT_RESULT_GRAIN_MISMATCH")
                main_rows = SQLTranslatorProd._convert_types(main_rows, preserve_decimal=True)
                reference_rows = SQLTranslatorProd._convert_types(reference_rows, preserve_decimal=True)
                main_value = next(iter(main_rows[0].values()))
                reference_value = reference_rows[0].get("reference_value")
                if type(main_value) is not type(reference_value) or main_value != reference_value:
                    raise ValueError("INDEPENDENT_VALUE_MISMATCH")
                data_as_of = snapshot_time.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                result = {
                    "success": True,
                    "data": main_rows,
                    "columns": main_columns,
                    "row_count": 1,
                    "business_query_submitted": True,
                    "snapshot_id": "mysql-consistent:" + digest({
                        "source": [self.source.get("id"), self.source.get("semantic_model_id")],
                        "main": [main_sql, main_parameters, exact_result_payload({"columns": main_columns, "data": main_rows})],
                        "reference": [reference_sql, reference_parameters, str(reference_value)],
                        "data_as_of": data_as_of,
                    }),
                    "data_as_of": data_as_of,
                    "quality_status": "PASS",
                    "quality_checks": {
                        "consistent_snapshot": True,
                        "read_only_transaction": True,
                        "column_contract_valid": True,
                        "row_contract_valid": True,
                        "row_count_reconciled": True,
                        "statement_timeout_enforced": True,
                    },
                }
                evidence = {
                    "status": "PASS",
                    "kind": self.kind,
                    "request_fingerprint": request.fingerprint,
                    "main_sql": main_sql,
                    "main_parameters": main_parameters,
                    "reference_sql": reference_sql,
                    "reference_parameters": reference_parameters,
                    "same_connection": True,
                    "same_transaction_snapshot": True,
                    "snapshot_time": data_as_of,
                    "main_result": exact_result_payload({"columns": main_columns, "data": main_rows}),
                    "reference_result": {
                        "type": type(reference_value).__name__, "value": str(reference_value)
                    },
                    "exact_match": True,
                    "seconds": monotonic() - started,
                    "main_submitted": main_submitted,
                    "reference_submitted": reference_submitted,
                }
                atomic_write(self.output, evidence)
                return {
                    "request_fingerprint": request.fingerprint,
                    "prepared_fingerprint": request.prepared_fingerprint,
                    "context_fingerprint": request.context.fingerprint(),
                    "data_source_id": request.data_source_id,
                    "provenance": "LIVE_READ_ONLY",
                    "submitted": True,
                    "result": result,
                }
        except Exception as exc:
            atomic_write(self.output, {
                "status": "FAILED",
                "kind": self.kind,
                "error_type": type(exc).__name__,
                "reason": str(exc) if str(exc).startswith(("INDEPENDENT_", "READ_ONLY_", "SQL_")) else "EXECUTION_FAILED",
                "main_submitted": main_submitted,
                "reference_submitted": reference_submitted,
                "seconds": monotonic() - started,
            })
            raise
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()

    async def __call__(self, request):
        return await asyncio.to_thread(self._run, request)


def append_resources(output, keys):
    path = output / "redis_resources.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"keys": []}
    current["keys"] = sorted(set(current["keys"]) | set(keys))
    current["prefix"] = keys[0].split(":budget", 1)[0] if keys and ":budget" in keys[0] else current.get("prefix")
    atomic_write(path, current)


def build_runtime(args, question, conversation, phase):
    config = redis_config()
    sync_redis = SyncRedis(**config)
    prefix = f"youo:data-analysis:v2:isolated:round5-11:{args.run_id}"
    budget = SharedBudget(sync_redis, prefix + ":budget")
    budget.reserve("metadata")
    source_translator = SQLTranslatorProd(config)
    source = source_translator.fetch_data_source("81", "58")
    if not source or str(source.get("id")) != "58" or str(source.get("semantic_model_id")) != "81":
        raise ValueError("SOURCE_58_SCOPE_RESOLUTION_FAILED")
    catalog, raw, snapshot, observations, publication, denied, snapshot_path, observations_path = load_frozen(args.activation)
    base_settings = Settings().model_copy(update={
        "intent_model_max_retries": 0,
        "intent_model_timeout_seconds": 60,
    })
    if base_settings.intent_model_name != "qwen3.7-max" or base_settings.intent_model_enable_thinking is not False:
        raise ValueError("FROZEN_MODEL_CONFIG_MISMATCH")
    recorder = native.ModelRecorder(base_settings, max_calls=12, capture_exchanges=True)
    model = BudgetedModel(recorder, budget)
    source_observer = native.FrozenSourceValues(
        observations,
        catalog=catalog,
        snapshot=snapshot,
        expected_hash=observations["artifact_hash"],
        allow_synthetic=False,
    )
    engine = RawTurnPlanner(model, publication, clock=lambda: datetime.fromisoformat(CLOCK))
    planning_records = []

    def context_resolver(chat, identity):
        return ScopedPlanSession(chat, identity, publication).context

    async def plan_turn(chat, identity, state, plans):
        kind = expected_case(chat.question)
        started_calls = len(model.calls)
        model.begin_turn(f"R511-{phase}", 0)
        with native.network_guard(), sources(snapshot_path.parent), ExitStack() as stack:
            stack.enter_context(patch.object(sys, "path", [*sys.path, str(SERVICE_ROOT), str(SERVICE_ROOT / "tests")]))
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources, "observe", source_observer.observe))
            stack.enter_context(patch.object(catalog_value_sources, "observe_probe", source_observer.observe_probe))
            result = await asyncio.wait_for(
                engine.run(chat, identity, state=state, plans=plans), timeout=125
            )
        if denied:
            raise ValueError("UNRECORDED_SOURCE_READ")
        if result.plan is None:
            raise ValueError("V2_PLAN_REQUIRED")
        session = ScopedPlanSession(chat, identity, publication)
        session.restore(result.next_state, kind="CONVERSATION")
        plan = AuthorizedLogicalPlan.model_validate(
            session.restore(result.plan_state, kind="LAST_REQUEST")
        )
        evidence = time_evidence(session, plan)
        options = {} if evidence is None else {
            "time_storage": evidence,
            "time_evidence_digest": evidence.fingerprint,
        }
        prepared = prepare_execution(session, plan, sql_planner=sql_planner, **options)
        check = validate_plan(kind, plan, prepared)
        planning_records.append({
            "phase": phase,
            "kind": kind,
            "check": check,
            "recognition": result.model_dump(mode="json"),
            "model_calls": deepcopy(model.calls[started_calls:]),
            "model_exchanges": deepcopy(model.exchanges),
            "time_evidence": evidence.model_dump(mode="json") if evidence else None,
            "lowering": asdict(prepared.lowering),
            "sql_receipt": prepared.sql_receipt,
        })
        atomic_write(args.output / f"{phase}_planning.json", planning_records[-1])
        return PersistedScalarPlan(result.next_state, result.plan_state, prepared)

    kind = expected_case(question)
    transport = SameSnapshotTransport(
        source=source,
        budget=budget,
        output=args.output / f"{phase}_same_snapshot_verification.json",
        kind=kind,
    )
    async_redis = AsyncRedis(**config)
    store = RedisScalarSessionStore(
        async_redis,
        prefix=prefix,
        run_id=args.run_id,
        ttl_seconds=7200,
    )

    class LiveHandler(PersistedScalarApiHandler):
        async def aclose(self):
            await model.upstream.aclose()
            await super().aclose()

    handler = LiveHandler(
        store=store,
        context_resolver=context_resolver,
        planner=plan_turn,
        transport=transport,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
    )
    app_settings = base_settings.model_copy(update={
        "env": "development",
        "adapter_mode": "mock",
        "session_store_mode": "redis",
        "redis_url": base_settings.effective_redis_url(),
        "session_key_prefix": prefix,
        "long_term_memory_mode": "disabled",
        "business_question_collection_enabled": False,
        "langfuse_enabled": False,
        "intent_model_enabled": False,
        "trusted_backend_token": SecretStr(TOKEN),
        "request_timeout_seconds": 180,
    })
    app = create_app(app_settings, isolated_chat_handler=handler)
    chat = ChatRequest(
        **SCOPE,
        application_id=APPLICATION,
        conversation_id=conversation,
        message_id=f"{args.run_id}-{phase}",
        question=question,
    )
    context = context_resolver(chat, IDENTITY)
    state_identity = handler._identity(chat, IDENTITY)
    conversation_key = store._key(context, state_identity)
    return app, chat, handler, transport, model, budget, sync_redis, conversation_key, prefix


def headers():
    return {
        "Authorization": "Bearer " + TOKEN,
        "X-Tenant-Id": IDENTITY.tenant_id,
        "X-User-Id": IDENTITY.user_id,
        "X-Application-Id": APPLICATION,
    }


def execute_api_phase(args, *, question, conversation, phase, stream=False, repeat=False):
    app, chat, handler, transport, model, budget, sync_redis, conversation_key, prefix = build_runtime(
        args, question, conversation, phase
    )
    started = monotonic()
    with TestClient(app, headers=headers()) as client:
        route = "/agent_chat/stream" if stream else "/agent_chat"
        response = client.post(route, json=chat.model_dump(mode="json"))
        body = response.text if stream else response.json()
        if response.status_code != 200:
            raise ValueError("ORIGINAL_API_REQUEST_FAILED")
        events = []
        if stream:
            for block in response.text.split("\n\n"):
                if block.startswith("data: "):
                    events.append(json.loads(block[6:]))
            complete = [event for event in events if event.get("type") == "complete"]
            errors = [event for event in events if event.get("type") == "error"]
            if len(complete) != 1 or errors or complete[0].get("status") != "COMPLETED":
                raise ValueError("SSE_TERMINAL_CONTRACT_FAILED")
        else:
            if body.get("status") != "COMPLETED":
                raise ValueError("JSON_TERMINAL_CONTRACT_FAILED")
        before_repeat = budget.snapshot()
        repeated_body = None
        if repeat:
            repeated = client.post("/agent_chat", json=chat.model_dump(mode="json"))
            repeated_body = repeated.json()
            if repeated.status_code != 200 or repeated_body.get("status") != "COMPLETED":
                raise ValueError("IDEMPOTENT_API_RETRY_FAILED")
            if budget.snapshot() != before_repeat:
                raise ValueError("IDEMPOTENT_RETRY_CONSUMED_LIVE_BUDGET")
    # Re-open with a fresh Redis client after app shutdown to inspect durable state.
    async def inspect():
        redis = AsyncRedis(**redis_config())
        store = RedisScalarSessionStore(redis, prefix=prefix, run_id=args.run_id, ttl_seconds=7200)
        snapshot = await store.load(handler.context_resolver(chat, IDENTITY), handler._identity(chat, IDENTITY))
        message = snapshot.message(chat.message_id)
        exact = restore_exact_result(
            __import__("app.semantic_v2.authorized_contract", fromlist=["ScopedArtifact"]).ScopedArtifact.model_validate(message["result"]),
            handler.context_resolver(chat, IDENTITY),
        )
        ttl = await redis.ttl(conversation_key)
        await redis.aclose()
        return snapshot, message, exact, ttl
    snapshot, message, exact, ttl = asyncio.run(inspect())
    if message["status"] != "SUCCEEDED" or ttl <= 0:
        raise ValueError("PERSISTED_RESULT_RESTORE_FAILED")
    record = {
        "phase": phase,
        "status": "PASS",
        "route": route,
        "response_status": response.status_code,
        "response": body,
        "sse_events": events,
        "repeated_response": repeated_body,
        "model_calls": len(model.calls),
        "transport_calls": transport.calls,
        "budget": budget.snapshot(),
        "conversation_key_digest": digest(conversation_key),
        "redis_ttl": ttl,
        "persisted_state_version": snapshot.state_version,
        "persisted_message_status": message["status"],
        "result_encoding": message["result"]["payload"]["encoding_version"],
        "result_digest": message["result"]["payload_digest"],
        "restored_value_types": [type(value).__name__ for value in exact["data"][0].values()],
        "seconds": monotonic() - started,
    }
    atomic_write(args.output / f"{phase}_api_receipt.json", record)
    api_fingerprint_key = None
    if stream:
        # chat_stream reserves the original SessionStore response fingerprint before streaming.
        temp_settings = app.state.container.settings
        sessions = app.state.container.sessions
        api_fingerprint_key = sessions._key(
            "response-fingerprint", IDENTITY.tenant_id, IDENTITY.user_id,
            APPLICATION, conversation, chat.message_id,
        )
    append_resources(args.output, [prefix + ":budget", conversation_key] + ([api_fingerprint_key] if api_fingerprint_key else []))
    sync_redis.close()
    print(json.dumps({key: record[key] for key in (
        "phase", "status", "route", "model_calls", "transport_calls", "budget",
        "persisted_state_version", "result_encoding", "restored_value_types"
    )}, ensure_ascii=False))


def unsupported_phase(args):
    config = redis_config()
    sync_redis = SyncRedis(**config)
    prefix = f"youo:data-analysis:v2:isolated:round5-11:{args.run_id}"
    budget = SharedBudget(sync_redis, prefix + ":budget")
    before = budget.snapshot()
    catalog, raw, snapshot, observations, publication, denied, _, _ = load_frozen(args.activation)
    chat = ChatRequest(
        **SCOPE,
        application_id=APPLICATION,
        conversation_id=f"{args.run_id}-unsupported",
        message_id=f"{args.run_id}-unsupported",
        question="按城市统计去年江苏省订单笔数",
    )
    context = ScopedPlanSession(chat, IDENTITY, publication).context
    async_redis = AsyncRedis(**config)
    store = RedisScalarSessionStore(async_redis, prefix=prefix, run_id=args.run_id, ttl_seconds=7200)
    transport_calls = []

    async def planner(*unused):
        raise ValueError("EXECUTION_PAYLOAD_UNSUPPORTED")

    async def transport(request):
        transport_calls.append(request)
        raise AssertionError("unsupported shape must not execute")

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: context,
        planner=planner,
        transport=transport,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
    )
    settings = Settings().model_copy(update={
        "env": "development", "adapter_mode": "mock", "session_store_mode": "redis",
        "redis_url": Settings().effective_redis_url(), "session_key_prefix": prefix,
        "long_term_memory_mode": "disabled", "business_question_collection_enabled": False,
        "langfuse_enabled": False, "intent_model_enabled": False,
        "trusted_backend_token": SecretStr(TOKEN),
    })
    with TestClient(create_app(settings, isolated_chat_handler=handler), headers=headers()) as client:
        response = client.post("/agent_chat", json=chat.model_dump(mode="json"))
    body = response.json()
    after = budget.snapshot()
    if response.status_code != 200 or body.get("status") != "SAFE_FALLBACK" or body.get("error_code") != "EXECUTION_PAYLOAD_UNSUPPORTED":
        raise ValueError("UNSUPPORTED_API_CONTRACT_FAILED")
    if transport_calls or before != after:
        raise ValueError("UNSUPPORTED_QUERY_REACHED_LIVE_DEPENDENCY")
    atomic_write(args.output / "unsupported_api_receipt.json", {
        "status": "PASS", "controlled_recognition": True,
        "route": "/agent_chat", "response": body,
        "sql_submissions": 0, "model_calls": 0, "budget_before": before, "budget_after": after,
    })
    sync_redis.close()
    print(json.dumps({"phase": "unsupported", "status": "PASS", "sql_submissions": 0}, ensure_ascii=False))


def initialize(args):
    if args.output.exists():
        raise ValueError("ROUND511_OUTPUT_ALREADY_EXISTS")
    args.output.mkdir(parents=True)
    config = redis_config()
    redis = SyncRedis(**config)
    if not redis.ping():
        raise ValueError("REDIS_UNAVAILABLE")
    prefix = f"youo:data-analysis:v2:isolated:round5-11:{args.run_id}"
    budget = SharedBudget(redis, prefix + ":budget")
    budget.initialize()
    isolation = {
        "run_id": args.run_id,
        "prefix": prefix,
        "production_prefix": "youo:data-analysis:v2",
        "overlaps_production_prefix_exactly": prefix == "youo:data-analysis:v2",
        "redis": {"db": config["db"], "port": config["port"], "password_present": bool(config.get("password"))},
        "ttl_seconds": 7200,
        "budget_limits": LIMITS,
        "budget": budget.snapshot(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deadline": args.deadline,
        "scope": SCOPE,
        "data_source_id": 58,
        "as_of": CLOCK,
        "model": {"name": "qwen3.7-max", "thinking": False, "temperature": 0, "retry": 0},
    }
    atomic_write(args.output / "isolation_and_budget.json", isolation)
    append_resources(args.output, [prefix + ":budget"])
    budget_snapshot = budget.snapshot()
    redis.close()
    print(json.dumps({"status": "INITIALIZED", "run_id": args.run_id, "budget": budget_snapshot}, ensure_ascii=False))


def inspect_and_cleanup(args):
    config = redis_config()
    redis = SyncRedis(**config)
    resource_path = args.output / "redis_resources.json"
    resources = json.loads(resource_path.read_text(encoding="utf-8"))
    keys = resources["keys"]
    before = []
    for key in keys:
        before.append({"key_digest": digest(key), "exists": bool(redis.exists(key)), "ttl": redis.ttl(key)})
    budget = SharedBudget(redis, f"youo:data-analysis:v2:isolated:round5-11:{args.run_id}:budget").snapshot()
    deleted = redis.delete(*keys) if keys else 0
    after = [bool(redis.exists(key)) for key in keys]
    receipt = {
        "status": "PASS" if not any(after) else "FAILED",
        "exact_resource_count": len(keys),
        "resources_before": before,
        "budget_final": budget,
        "deleted": deleted,
        "all_exact_resources_absent_after": not any(after),
        "flushdb": False,
        "flushall": False,
        "scan_used": False,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(args.output / "redis_cleanup_receipt.json", receipt)
    redis.close()
    print(json.dumps({"status": receipt["status"], "budget": budget,
                      "exact_resources": len(keys), "deleted": deleted}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["init", "p1", "p2", "amount", "amount_recheck", "amount_final", "unsupported", "cleanup"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--activation", required=True)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--allow-read-only-source-58", action="store_true")
    parser.add_argument("--allow-isolated-redis", action="store_true")
    args = parser.parse_args()
    if datetime.now(timezone.utc) >= datetime.fromisoformat(args.deadline):
        raise ValueError("ROUND511_DEADLINE_EXCEEDED")
    if not args.allow_isolated_redis:
        raise ValueError("ISOLATED_REDIS_OPT_IN_REQUIRED")
    if args.phase == "init":
        initialize(args)
    elif args.phase == "cleanup":
        inspect_and_cleanup(args)
    elif args.phase == "unsupported":
        unsupported_phase(args)
    else:
        if not (args.allow_model_calls and args.allow_read_only_source_58):
            raise ValueError("LIVE_MODEL_AND_SOURCE_OPT_IN_REQUIRED")
        conversation = f"{args.run_id}-count" if args.phase in {"p1", "p2"} else f"{args.run_id}-{args.phase.replace('_', '-')}"
        execute_api_phase(
            args,
            question={"p1": "查询去年江苏省订单笔数", "p2": "换今年", "amount": "查询含税销售总额", "amount_recheck": "查询含税销售总额", "amount_final": "查询含税销售总额"}[args.phase],
            conversation=conversation,
            phase=args.phase,
            stream=args.phase == "p2",
            repeat=args.phase == "p2",
        )


if __name__ == "__main__":
    main()
