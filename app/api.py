from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator

from app.domain.models import (
    AgentResponse,
    ChatRequest,
    PrimaryIntent,
    StrictModel,
    TrustedIdentity,
)
from app.stores import MessageIdReuseConflictError
from app.observability.langfuse_client import (
    StageSpanTracker,
    end_structural_spans,
    hash_identifier,
    observe_span,
    register_structural_span,
    summarize_value,
    text_metadata,
    trace_attributes,
)
from app.observability.bridge_profile import (
    BRIDGE_RUNTIME_MODE,
    bridge_capability_profile,
)
from app.observability.call_timing import (
    RequestTimingTracker,
    log_performance_trace,
    timing_scope,
)
from app.services.file_ingestion import FileImportError
from app.services.upload_file_resolver import (
    NO_PARSE_MARKER,
    UploadReferenceResolutionError,
)
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services import semantic_recommend
from app.services.progress import _progress_callback, progress_scope
from app.logging_config import set_log_trace_id, reset_log_trace_id
from minio_followup_store import DatasetScope
from app.security import trusted_backend, require_application_namespace, resolve_conversation_identity


router = APIRouter(tags=["data-analysis"], dependencies=[Depends(trusted_backend)])
# 语义建模推荐接口为只读元数据查询（用户 2026-10-14 决定）：挂独立无鉴权 router，
# 供 Java 后端内网直接调用，与其余内部服务的调用方式保持一致。
recommend_router = APIRouter(tags=["semantic-modeling"])
logger = logging.getLogger(__name__)


async def _forward_traced_progress(
    stage_tracker: StageSpanTracker,
    downstream,
    event: dict[str, Any],
) -> None:
    """Queue telemetry without waiting for SDK/export work on the SSE loop."""

    enqueue = getattr(stage_tracker, "enqueue", None)
    if callable(enqueue):
        enqueue(event)

    if downstream is not None:
        result = downstream(event)
        if inspect.isawaitable(result):
            await result
        # Queue.put() completes inline while the queue has capacity. Yield once
        # so the SSE producer can render this milestone before recognition
        # continues into its next synchronous catalog/validation section.
        await asyncio.sleep(0)
        return


class SpreadsheetImportRequest(StrictModel):
    application_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=128)
    object_name: str = Field(min_length=1, max_length=1024)
    semantic_model_id: int = Field(gt=0, strict=True)
    business_domain_id: int | None = Field(default=None, gt=0, strict=True)
    business_domain_ids: list[int] = Field(default_factory=list, max_length=50)
    database_id: int | None = Field(default=None, gt=0, strict=True)
    knowledge_base_names: list[str] = Field(default_factory=list, max_length=50)

    def _scope_chat(self) -> ChatRequest:
        keys = {'semantic_model_id','business_domain_id','business_domain_ids','database_id','knowledge_base_names'}
        values = {k:getattr(self,k) for k in keys if k in self.model_fields_set}
        return ChatRequest(application_id=self.application_id,conversation_id=self.conversation_id,
                           message_id='spreadsheet-import',question='Register uploaded spreadsheet',**values)

    @model_validator(mode='after')
    def validate_scope(self):
        chat = self._scope_chat()
        self.application_id = chat.application_id
        self.conversation_id = chat.conversation_id
        return self


class SpreadsheetImportResponse(StrictModel):
    success: bool = True
    dataset_id: str
    source_object: str
    sheets: list[str]
    columns: list[str]
    row_count: int
    expires_at: str


@router.post(
    "/v1/data-analysis/datasets/import-spreadsheet",
    response_model=SpreadsheetImportResponse,
    status_code=201,
    summary="将平台已上传到MinIO的CSV/XLSX注册为可追问数据集",
)
async def import_spreadsheet(
    payload: SpreadsheetImportRequest,
    request: Request,
    x_roles: str | None = Header(default=None),
) -> SpreadsheetImportResponse:
    identity = trusted_identity(request, payload)
    require_application_namespace(request, payload.application_id)
    application_id = payload.application_id
    importer = request.app.state.container.file_importer
    if importer is None:
        raise HTTPException(status_code=503, detail="spreadsheet import is disabled")
    try:
        reference, sheets = await importer.import_object(
            object_name=payload.object_name,
            scope=DatasetScope(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                application_id=application_id,
                conversation_id=payload.conversation_id,
                authorized_semantic_scope_fingerprint=payload._scope_chat().authorized_semantic_scope.fingerprint(),
            ),
            semantic_model_id=payload.semantic_model_id,
            business_domain_ids=payload._scope_chat().business_domain_ids,
        )
    except FileImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("spreadsheet import failed")
        raise HTTPException(status_code=502, detail="spreadsheet import failed") from exc
    return SpreadsheetImportResponse(
        dataset_id=reference.dataset_id,
        source_object=payload.object_name,
        sheets=sheets,
        columns=list(reference.columns),
        row_count=reference.row_count,
        expires_at=reference.expires_at,
    )


CHAT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "同一 message_id 被用于不同的有效请求载荷",
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "code": "MESSAGE_ID_REUSE_CONFLICT",
                        "message": "each new user message must use a new message_id",
                    }
                }
            }
        },
    },
    504: {
        "description": "下游服务处理超时",
        "content": {
            "application/json": {
                "example": {"detail": "upstream operation timed out"}
            }
        },
    },
}


@router.get(
    "/v1/data-analysis/diagnostics/bridge-profile",
    summary="当前桥接主线能力与计时点",
)
async def bridge_profile(request: Request) -> dict[str, Any]:
    """Return the content-free Phase-1 capability/timing contract."""

    return bridge_capability_profile(
        active_runtime_mode=request.app.state.container.settings.runtime_mode
    )


def trusted_identity(request: Request, payload: ChatRequest | SpreadsheetImportRequest) -> TrustedIdentity:
    """State isolation identity, verified by the backend service dependency."""
    return resolve_conversation_identity(request, payload.application_id, payload.conversation_id)


async def invoke(request: Request, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
    require_application_namespace(request, chat.application_id)
    stage_tracker = StageSpanTracker()
    settings = request.app.state.container.settings
    performance_tracker = (
        RequestTimingTracker(runtime_mode=settings.runtime_mode)
        if settings.runtime_mode == BRIDGE_RUNTIME_MODE
        else None
    )
    performance_trace_logged = False
    timing_terminal_status = "FAILED"
    existing_progress = _progress_callback.get()
    trace_token = set_log_trace_id(chat.message_id)

    async def trace_progress(event: dict[str, Any]) -> None:
        if performance_tracker is not None:
            performance_tracker.record_progress(event)
        await _forward_traced_progress(
            stage_tracker,
            existing_progress,
            event,
        )

    try:
        user_hash = hash_identifier(identity.user_id)
        session_hash = hash_identifier(chat.conversation_id)
        with trace_attributes(
            user_id=user_hash,
            session_id=session_hash,
            trace_name="DataAnalysis_Agent",
            tags=["DataAnalysis_Agent"],
            metadata={
                "tenant_id_hash": hash_identifier(identity.tenant_id),
                "application_id_hash": hash_identifier(chat.application_id),
                "message_id_hash": hash_identifier(chat.message_id),
            },
        ):
            with observe_span(
                as_type="agent",
                name="data-analysis-request",
                input={
                    "question": text_metadata(chat.question),
                    "conversation_id_hash": session_hash,
                    "application_id_hash": hash_identifier(chat.application_id),
                    "message_id_hash": hash_identifier(chat.message_id),
                    "regenerate": bool(chat.regenerate),
                },
            ) as root_span:
                register_structural_span("request", root_span)
                trace_response = None
                try:
                    with timing_scope(performance_tracker):
                        with progress_scope(trace_progress):
                            isolated_handler = getattr(
                                request.app.state, "isolated_chat_handler", None
                            )
                            operation = (
                                isolated_handler.handle(chat, identity)
                                if isolated_handler is not None
                                else request.app.state.container.workflow.ainvoke(
                                    {"chat": chat, "identity": identity}
                                )
                            )
                            result = await operation
                    response = (
                        result
                        if isolated_handler is not None
                        else result["response"]
                    )
                    trace_response = response
                    timing_terminal_status = response.status
                    if performance_tracker is not None:
                        performance_trace = performance_tracker.finish(
                            timing_terminal_status
                        )
                        response.performance_trace = performance_trace
                        log_performance_trace(performance_trace)
                        performance_trace_logged = True
                    _record_extension_tool_spans(response)
                    root_span.update(output={
                        "status": response.status,
                        "intent": response.intent.value,
                        "answer": text_metadata(response.answer, allow_content=False),
                        "evidence_count": len(response.evidence),
                        "extension_count": len(response.extension_executions),
                        "file_count": len(response.files),
                        "chart_count": len(response.chart_specs),
                    })
                    return response
                except TimeoutError:
                    timing_terminal_status = "TIMEOUT"
                    root_span.update(
                        output={"status": "TIMEOUT"},
                        level="ERROR",
                        status_message="upstream operation timed out",
                    )
                    raise
                except MessageIdReuseConflictError:
                    timing_terminal_status = "MESSAGE_ID_REUSE_CONFLICT"
                    root_span.update(
                        output={"status": "MESSAGE_ID_REUSE_CONFLICT"},
                        level="ERROR",
                        status_message="message id reuse conflict",
                    )
                    raise
                except Exception as exc:
                    timing_terminal_status = "FAILED"
                    root_span.update(
                        output={"status": "FAILED", "error_type": type(exc).__name__},
                        level="ERROR",
                        status_message=type(exc).__name__,
                    )
                    raise
                finally:
                    def finish_trace() -> None:
                        try:
                            stage_tracker.finish(trace_response, status=timing_terminal_status)
                        except Exception:
                            logger.debug("Langfuse request finalization failed", exc_info=True)
                        finally:
                            end_structural_spans()

                    await asyncio.to_thread(finish_trace)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="upstream operation timed out") from exc
    except MessageIdReuseConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
        reset_log_trace_id(trace_token)
        if performance_tracker is not None and not performance_trace_logged:
            log_performance_trace(
                performance_tracker.finish(timing_terminal_status)
            )
        stage_tracker.close_all()


def _record_extension_tool_spans(response: AgentResponse) -> None:
    """Record tool shape/status without exporting raw tool results."""

    for execution in response.extension_executions:
        with observe_span(
            as_type="tool",
            name=f"tool:{execution.name}",
            input={
                "kind": execution.kind,
                "status_code": execution.status_code,
            },
        ) as tool_span:
            tool_span.update(
                output={
                    "status": execution.status,
                    "result": summarize_value(
                        execution.output
                        if execution.output is not None
                        else execution.error
                    ),
                    "error_type": execution.error_type,
                },
                level=("DEFAULT" if execution.status == "COMPLETED" else "ERROR"),
            )


async def _collect_business_question(request: Request, chat: ChatRequest) -> None:
    """Persist one real user question without coupling chat availability to I/O."""
    collector = request.app.state.container.business_question_collector
    if collector is None:
        return
    try:
        await asyncio.to_thread(
            collector.record,
            application_id=chat.application_id,
            conversation_id=chat.conversation_id,
            message_id=chat.message_id,
            question=chat.question,
        )
    except Exception:
        # Question collection is an observability feature. A transient disk
        # problem must never turn a valid business question into a chat error.
        logger.exception("business question collection failed")


async def bind_chat_spreadsheet(
    request: Request, chat: ChatRequest, identity: TrustedIdentity
) -> None:
    """Convert one caller-provided MinIO spreadsheet path into a dataset.

    The generic platform API permits multiple temporary files, while the current
    deterministic data workflow requires one explicit source dataset per atomic
    task. Rejecting ambiguity is safer than silently selecting the first file.
    """
    file_based = not bool(
        re.search(
            r"(?:不要|不用|忽略|跳过).{0,8}(?:文件|表格|上传数据)|"
            r"(?:改用|只查|仅查|查询).{0,8}(?:业务库|数据库)",
            chat.question,
        )
    )
    if not chat.temp_file_paths:
        chat._file_inspection = {
            "status": "DATASET_BOUND" if chat.dataset_id else "NOT_PROVIDED",
            "dataset_id": chat.dataset_id,
            "file_based": bool(chat.dataset_id) and file_based,
        }
        if chat.dataset_id and not file_based:
            chat.dataset_id = None
        return
    if any(
        item.casefold() == NO_PARSE_MARKER for item in chat.temp_file_paths
    ):
        resolver = getattr(
            request.app.state.container,
            "upload_file_resolver",
            None,
        )
        if resolver is None:
            raise HTTPException(
                status_code=422,
                detail="本轮上传文件缺少可用的MinIO对象名",
            )
        try:
            chat.temp_file_paths = await resolver.resolve(
                chat.temp_file_paths,
                conversation_id=chat.conversation_id,
                question=chat.question,
            )
        except UploadReferenceResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if chat.dataset_id is not None:
        chat._file_inspection = {
            "status": "DATASET_BOUND",
            "dataset_id": chat.dataset_id,
            "file_count": len(chat.temp_file_paths),
            "file_based": file_based,
        }
        if not file_based:
            chat.dataset_id = None
        return
    if len(chat.temp_file_paths) != 1:
        raise HTTPException(
            status_code=422,
            detail="当前每个原子问题只能绑定一个CSV/XLSX文件；多个文件请拆分问题或先合并。",
        )
    object_name = chat.temp_file_paths[0]
    if not object_name.lower().endswith((".csv", ".xlsx")):
        raise HTTPException(
            status_code=422,
            detail="temp_file_paths当前仅用于CSV/XLSX问数；文档问答请传knowledge_base_names。",
        )
    importer = request.app.state.container.file_importer
    if importer is None:
        raise HTTPException(status_code=503, detail="spreadsheet import is disabled")
    try:
        reference, sheets = await importer.import_object(
            object_name=object_name,
            scope=DatasetScope(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                application_id=chat.application_id,
                conversation_id=chat.conversation_id,
                authorized_semantic_scope_fingerprint=chat.authorized_semantic_scope.fingerprint(),
            ),
            semantic_model_id=chat.semantic_model_id,
            business_domain_ids=chat.business_domain_ids,
        )
    except FileImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        if chat.mcp and file_based:
            logger.warning(
                "local spreadsheet import failed; continuing with configured MCP: %s",
                type(exc).__name__,
            )
            chat._file_inspection = {
                "status": "LOCAL_IMPORT_FAILED_MCP_AVAILABLE",
                "file_name": object_name.rsplit("/", 1)[-1],
                "file_count": 1,
                "file_based": True,
            }
            return
        logger.exception("chat spreadsheet import failed")
        raise HTTPException(status_code=502, detail="spreadsheet import failed") from exc
    if file_based:
        chat.dataset_id = reference.dataset_id
    chat._file_inspection = {
        "status": "READ_SUCCESS",
        "file_name": object_name.rsplit("/", 1)[-1],
        "format": object_name.rsplit(".", 1)[-1].upper(),
        "sheet_count": len(sheets),
        "sheets": list(sheets[:10]),
        "row_count": reference.row_count,
        "column_count": len(reference.columns),
        "columns": list(reference.columns[:12]),
        "columns_truncated": len(reference.columns) > 12,
        "byte_size": reference.byte_size,
        "dataset_id": reference.dataset_id,
        "file_based": file_based,
    }


_DASH_TRANSLATION = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "﹘": "-", "﹣": "-", "－": "-",
})


def _normalized_regeneration_text(value: str) -> str:
    """Normalize only representation differences used to locate a history turn."""

    normalized = unicodedata.normalize("NFKC", value).translate(_DASH_TRANSLATION)
    return re.sub(r"\s+", "", normalized).casefold()


def _normalized_history_question(value: str) -> str:
    """Remove only known UI labels; never use fuzzy or arbitrary suffix matching."""

    normalized = _normalized_regeneration_text(value)
    return re.sub(
        r"^(?:第(?:\d+|[一二三四五六七八九十]+)轮(?:问题)?|用户原始问题|用户问题|问题):",
        "",
        normalized,
        count=1,
    )


def _regeneration_mode(payload: ChatRequest) -> str:
    """Distinguish answer refresh from editing and resubmitting a question."""

    if payload.original_question and (
        _normalized_regeneration_text(payload.original_question)
        != _normalized_regeneration_text(payload.question)
    ):
        return "REVISE"
    return "REFRESH"


async def _collect_revised_business_question(
    request: Request, payload: ChatRequest
) -> None:
    """Record a revised user question once, while pure refresh stays silent."""

    if _regeneration_mode(payload) != "REVISE":
        return
    revised = payload.model_copy(deep=True)
    attempt = payload.refresh_request_id or payload.message_id
    key = "\x1f".join((
        payload.application_id,
        payload.conversation_id,
        attempt,
        _normalized_regeneration_text(payload.question),
    ))
    revised.message_id = f"revision-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"
    await _collect_business_question(request, revised)


def _prepare_regeneration(payload: ChatRequest) -> tuple[ChatRequest, str, str]:
    """Prepare an idempotent refresh inside the original conversation scope.

    产品只允许操作当前会话的最后一个问题：
    - 纯刷新：question 仍为最后一个问题；
    - 修改重提：question 为修改后的问题，original_question 为修改前的问题。
    history 不是刷新接口的必填项。调用方若携带 history，则仅在最后一条
    user 消息确实是目标问题时截断该轮及旧答案；永远不向前搜索或替换更早轮次。
    replaces_message_id 仅保留兼容，也只能指向最后一条 user 消息。
    """
    external_conversation_id = payload.conversation_id
    external_message_id = payload.message_id
    execution = payload.model_copy(deep=True)
    # Keep all generated state and datasets in the real conversation.  Only the
    # message id is namespaced so a refresh cannot collide with the original
    # answer.  The deterministic id makes an identical network retry idempotent;
    # a deliberate later refresh supplies a new refresh_request_id.
    mode = _regeneration_mode(payload)
    refresh_attempt_id = payload.refresh_request_id or external_message_id
    refresh_seed_parts = [
        payload.application_id,
        external_conversation_id,
        refresh_attempt_id,
    ]
    if payload.refresh_request_id is None:
        # Compatibility for older clients that reuse the original message id
        # when editing: a changed question is a new deterministic attempt, not
        # a message-id conflict with the prior pure refresh.
        refresh_seed_parts.extend((
            mode,
            _normalized_regeneration_text(payload.question),
            _normalized_regeneration_text(payload.original_question or ""),
        ))
    refresh_seed = "\x1f".join(refresh_seed_parts)
    refresh_digest = hashlib.sha256(refresh_seed.encode("utf-8")).hexdigest()[:32]
    execution.message_id = f"refresh-{refresh_digest}"
    execution.regenerate = False
    execution._bypass_repeat_query_cache = True
    execution._is_regeneration_execution = True
    execution._regeneration_mode = mode

    # 修改重提时，history 中的目标仍是修改前的问题；纯刷新时目标就是 question。
    match_target = payload.original_question or payload.question
    normalized_target = _normalized_regeneration_text(match_target)

    # 只定位 history 中最后一条 user 消息，绝不按 message_id 向前搜索旧轮次。
    last_user_index = next(
        (
            index
            for index in range(len(execution.history) - 1, -1, -1)
            if execution.history[index].role == "user"
        ),
        None,
    )
    if last_user_index is None:
        # history 可省略；结构化短期状态仍由原 conversation_id 隔离并在执行前清理。
        if payload.replaces_message_id:
            logger.warning(
                "regeneration carries deprecated replaces_message_id but no "
                "history; proceeding with last-turn-only semantics"
            )
    else:
        item = execution.history[last_user_index]
        if (
            payload.replaces_message_id
            and item.message_id != payload.replaces_message_id
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "刷新接口只允许操作最后一条用户消息；"
                    "replaces_message_id 与最后一条用户消息不一致"
                ),
            )
        normalized_content = _normalized_history_question(item.content)
        if normalized_content == normalized_target:
            # 截断被替换轮及其之后的所有消息（含旧答案），旧答案不得
            # 作为证据或追问结果参与重新生成。
            execution.history = execution.history[:last_user_index]
        else:
            # 客户端可能只携带目标轮之前的上下文。目标不一致时保留原 history，
            # 不能把更早一轮误当作最后一问删除。
            logger.warning(
                "last history user turn is not the regeneration target; "
                "history kept as-is"
            )
    return execution, external_conversation_id, external_message_id


@router.post(
    "/agent_chat",
    response_model=AgentResponse,
    responses=CHAT_ERROR_RESPONSES,
    summary="同步数据分析对话",
    description=(
        "每条新用户消息必须使用新的 message_id；只有同一次网络重试才能复用。"
        "业务状态由响应体 status 判断，NEEDS_CLARIFICATION 和 SAFE_FALLBACK 仍为 HTTP 200。"
    ),
)
async def chat(
    payload: ChatRequest,
    request: Request,
    x_roles: str | None = Header(default=None, description="可选；逗号分隔的角色"),
) -> AgentResponse:
    identity = trusted_identity(request, payload)
    require_application_namespace(request, payload.application_id)
    external_conversation_id = payload.conversation_id
    isolated_handler = getattr(request.app.state, "isolated_chat_handler", None)
    if isolated_handler is None or bool(
        getattr(isolated_handler, "uses_v1_ingress", False)
    ):
        is_regeneration = payload.regenerate
        if not is_regeneration:
            await _collect_business_question(request, payload)
        else:
            await _collect_revised_business_question(request, payload)
        if is_regeneration:
            payload, external_conversation_id, _ = _prepare_regeneration(payload)
        await bind_chat_spreadsheet(request, payload, identity)
    response = await invoke(request, payload, identity)
    response.conversation_id = external_conversation_id
    return response


@router.post(
    "/agent_chat/refresh",
    response_model=AgentResponse,
    responses=CHAT_ERROR_RESPONSES,
    summary="刷新或修改重提当前会话最后一个问题",
    description=(
        "仅操作当前会话最后一个问题。刷新原问题时 question 保持不变；"
        "修改后重新提问时 question 传新问题、original_question 传修改前问题。"
        "history 和 replaces_message_id 均非必填。refresh_request_id 是刷新尝试幂等键。"
    ),
)
async def chat_refresh(
    payload: ChatRequest,
    request: Request,
    x_roles: str | None = Header(default=None),
) -> AgentResponse:
    payload.regenerate = True
    return await chat(payload, request, x_roles)


@router.post(
    "/agent_chat/stream",
    response_class=StreamingResponse,
    summary="SSE 数据分析对话",
    description=(
        "这是 POST SSE，采用与 New_Agent 相同的 data-only Envelope；"
        "事件类型放在JSON的 type 字段中，不输出 event: 行。"
        "常用类型为 updata_state、message_chunk、tool_result、answer、complete。"
        "各节点先即时推送运行状态，已完成并校验的展示文本再通过 message_chunk 渐进推送；普通文本默认"
        "每 4 个 Unicode 字符一片，超长内容自适应增大片长并限制事件总数。"
        "最终答案完成可靠性校验后，按 New_Agent 的规则每 6 个 Unicode 字符"
        "推送一个 output/message_chunk（末片可少于 6 字）。"
        "身份、请求格式和已存在的 message_id 冲突在建立事件流前保持标准 HTTP 状态；"
        "流建立后的超时或运行异常使用 error 事件返回。"
    ),
    responses={
        200: {
            "description": "命名 SSE 事件流",
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": (
                        'data: {"step":"","type":"updata_state",'
                        '"data":"accepted"}\n\n'
                        'data: {"type":"message_chunk",'
                        '"step":"output","content":"最近六个月","role":"assistant"}\n\n'
                        'data: {"type":"answer","step":"output",'
                        '"content":"最近六个月销售额整体上升。"}\n\n'
                        'data: {"type":"complete","step":"output",'
                        '"content":"","status":"COMPLETED"}\n\n'
                    ),
                }
            },
        },
        409: CHAT_ERROR_RESPONSES[409],
        504: CHAT_ERROR_RESPONSES[504],
    },
)
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    x_roles: str | None = Header(default=None, description="可选；逗号分隔的角色"),
) -> StreamingResponse:
    identity = trusted_identity(request, payload)
    require_application_namespace(request, payload.application_id)
    external_conversation_id = payload.conversation_id
    external_message_id = payload.message_id
    isolated_handler = getattr(request.app.state, "isolated_chat_handler", None)
    if isolated_handler is None or bool(
        getattr(isolated_handler, "uses_v1_ingress", False)
    ):
        is_regeneration = payload.regenerate
        if not is_regeneration:
            await _collect_business_question(request, payload)
        else:
            await _collect_revised_business_question(request, payload)
        if is_regeneration:
            payload, external_conversation_id, external_message_id = _prepare_regeneration(payload)
        await bind_chat_spreadsheet(request, payload, identity)
    # Preserve the most important pre-stream idempotency guarantee.  Once the
    # first SSE byte is sent HTTP status is necessarily 200, so a known reuse
    # conflict must be detected before committing the stream.
    try:
        if isolated_handler is not None and hasattr(
            isolated_handler, "check_message_conflict"
        ):
            await isolated_handler.check_message_conflict(payload, identity)
        else:
            fingerprint = DataAnalysisOrchestrator._request_fingerprint(payload, identity)
            await request.app.state.container.sessions.get_response(
                identity.tenant_id,
                identity.user_id,
                payload.application_id,
                payload.conversation_id,
                payload.message_id,
                fingerprint,
            )
    except MessageIdReuseConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    # SSE 流式链路独立于 invoke 的任务上下文：在端点上下文绑定 trace_id，
    # 覆盖流式生成器里的日志；请求任务结束即随上下文消亡，不会跨请求泄漏。
    set_log_trace_id(payload.message_id)

    async def events() -> AsyncIterator[str]:
        progress_queue: asyncio.Queue[
            tuple[dict[str, Any], asyncio.Future[None]]
        ] = asyncio.Queue(maxsize=100)

        async def publish_progress(event: dict[str, Any]) -> None:
            rendered = asyncio.get_running_loop().create_future()
            await progress_queue.put((event, rendered))
            # Give the SSE producer a short opportunity to put the first text
            # fragment on the wire. Do not make model/tool execution wait for
            # the full character animation, or several long milestones can
            # consume the request deadline purely in presentation work.
            try:
                await asyncio.wait_for(asyncio.shield(rendered), timeout=0.05)
            except asyncio.TimeoutError:
                pass

        async def execute() -> AgentResponse:
            with progress_scope(publish_progress):
                return await invoke(request, payload, identity)

        started_at = time.monotonic()
        last_visible_at = started_at
        execution = asyncio.create_task(execute())
        deferred_planning: list[dict[str, Any]] = []
        intent_completed = False
        question_completion_displayed = False
        file_inspection_completed = False
        planning_released = False
        presentation_scenario = "ANALYTIC"
        titled_think_sections: set[str] = set()
        composite_mode = False
        off_topic_flag = False
        planning_data_verdict = ""
        planning_data_operation_text = ""
        planning_start_emitted = False
        composite_child_orderer = _CompositeChildProgressOrderer()
        latest_progress_stage = "INTENT_RECOGNITION"
        settings = request.app.state.container.settings
        heartbeat_interval = settings.thinking_stream_heartbeat_seconds

        def render_thinking(event: dict[str, Any]) -> list[str]:
            stage = str(event.get("stage") or "processing").strip().upper()
            section = _thinking_section(stage)
            include_heading = bool(
                section
                and section not in titled_think_sections
                and _heading_event_is_visible_summary(section, event)
                # "正在进行任务拆分…"提示行不是节点开篇内容，
                # 节标题仍随其后的规划块（拆分判断完成）下发
                and event.get("progress_phase") != "V2_TASK_PLANNING_START"
            )
            if include_heading:
                titled_think_sections.add(section)
            return _thinking_events(
                event,
                heading=(
                    _thinking_title(section, presentation_scenario)
                    if include_heading else None
                ),
                chunk_size=settings.thinking_stream_chunk_size,
                max_chunks=settings.thinking_stream_max_chunks,
            )

        async def stream_thinking(event: dict[str, Any]) -> AsyncIterator[str]:
            nonlocal last_visible_at
            rendered_events = render_thinking(event)
            message_chunk_count = max(0, len(rendered_events) - 1)
            settings = request.app.state.container.settings
            chunk_delay = (
                0.0
                if settings.env == "test"
                else _thinking_chunk_delay(
                    message_chunk_count,
                    interval_seconds=settings.thinking_stream_chunk_interval_seconds,
                )
            )
            for index, rendered_event in enumerate(rendered_events):
                yield rendered_event
                if index > 0:
                    last_visible_at = time.monotonic()
                # The first item is the node-state event.  Yield control between
                # subsequent text chunks so the ASGI server and browser can
                # paint them progressively instead of coalescing one milestone.
                if chunk_delay and 0 < index < len(rendered_events) - 1:
                    await asyncio.sleep(chunk_delay)

        def heartbeat_events_if_due(*, force: bool = False) -> list[str]:
            nonlocal last_visible_at
            now = time.monotonic()
            if not force and now - last_visible_at < heartbeat_interval:
                return []
            heartbeat_events = _progress_heartbeat_events(
                latest_progress_stage,
                elapsed_seconds=now - started_at,
            )
            last_visible_at = now
            return heartbeat_events

        def _with_planning_verdict(
            planning_event: dict[str, Any],
        ) -> list[dict[str, Any]]:
            # 快照复用成立时把数据来源写进任务名：单个任务用"复用上一轮
            # 查询结果，{操作描述}"的说法，多个任务行退回句首状语；
            # 参数提取里的"是否查询数据库"同步纠正
            if not planning_data_verdict:
                return _split_planning_summary(planning_event)
            message = str(planning_event.get("message") or "")
            lines = message.split("\n")
            task_line_count = sum(
                1 for line in lines if re.match(r"^任务\d+：", line)
            )
            operation_text = planning_data_operation_text.strip(" 。")
            tagged_lines = []
            for line in lines:
                match = re.match(r"^(任务\d+：)(.*)$", line)
                if match and "复用上一轮" not in line and "基于上一轮" not in line:
                    if operation_text and task_line_count == 1:
                        line = match.group(1) + "复用上一轮查询结果，" + operation_text + "。"
                    else:
                        line = (
                            match.group(1)
                            + "基于上一轮查询结果数据集直接分析，"
                            + match.group(2)
                        )
                tagged_lines.append(line)
            merged = dict(planning_event)
            merged["message"] = "\n".join(tagged_lines)
            if merged.get("task_table"):
                # 快照复用后任务表里的查询标记同步改为不查库；
                # 单任务的名称与任务行同样改成复用上一轮的说法
                table_rows = merged["task_table"]
                merged["task_table"] = [
                    {
                        **row,
                        "是否查询数据库": "否（基于上一轮查询结果数据集直接分析）",
                        **(
                            {"名称": f"复用上一轮查询结果，{operation_text}"}
                            if operation_text and len(table_rows) == 1
                            else {}
                        ),
                    }
                    for row in table_rows
                ]
            return _split_planning_summary(merged)

        def _split_planning_summary(
            planning_event: dict[str, Any],
        ) -> list[dict[str, Any]]:
            # 决策行单独成事件保留 phase；任务与参数提取内容拆到后续事件
            # 且不带 phase，后端按 phase 识别提示行时不会扫到整块任务详情
            message = str(planning_event.get("message") or "")
            pieces = message.split("\n", 1)
            if len(pieces) == 1 or not planning_event.get("progress_phase"):
                return [planning_event]
            rest = pieces[1].strip("\n")
            if not rest:
                return [planning_event]
            tail_event = dict(planning_event, message=rest)
            tail_event.pop("progress_phase", None)
            # 任务表只随决策行事件的state下发一次
            tail_event.pop("task_table", None)
            return [dict(planning_event, message=pieces[0]), tail_event]

        def _planning_start_event() -> dict[str, Any]:
            return {
                "stage": "TASK_PLANNING",
                "status": "RUNNING",
                "progress_phase": "V2_TASK_PLANNING_START",
                "message": "正在进行任务拆分、任务意图分析与参数提取。",
            }

        def _with_planning_start_line(
            events: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            # "正在进行任务拆分…"提示行原先只挂在意图边界事件上补发，
            # 部分轮次拿不到边界事件或边界事件被前端节点刷新顶掉，
            # 展示时有时无；改为在规划块内容首次展示前固定补一条，整轮一次
            nonlocal planning_start_emitted
            if planning_start_emitted or not events:
                return events
            planning_start_emitted = True
            return [
                _planning_start_event(),
                *events,
            ]

        def ordered_progress(event: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal intent_completed, file_inspection_completed
            nonlocal question_completion_displayed
            nonlocal planning_released, presentation_scenario, composite_mode
            nonlocal latest_progress_stage, off_topic_flag
            nonlocal planning_data_verdict, planning_data_operation_text
            nonlocal planning_start_emitted
            stage = str(event.get("stage") or "").upper()
            message_text = str(event.get("message") or "")
            if bool(event.get("off_topic")):
                # 语义识别判成业务无关的问题，规划节点对这轮不再点亮
                off_topic_flag = True
            if stage == 'METADATA_RETRIEVAL' and bool(event.get('is_child_task')):
                # Cross the ordering barriers without displaying a data-query
                # node for a definition task. Mixed siblings keep their stages.
                return composite_child_orderer.push(dict(event, stage='DATA_RETRIEVAL', metadata_only=True))
            if bool(event.get("is_child_task")):
                try:
                    composite_mode = composite_mode or int(
                        event.get("task_count") or 0
                    ) > 1
                except (TypeError, ValueError):
                    pass
            if (
                bool(event.get("is_child_task"))
                and stage == "INTENT_RECOGNITION"
            ):
                # A DAG child is not the user's root question. Rendering the
                # first concurrently completed child as the public intent made
                # composite requests appear truncated and race-dependent.
                return []
            if (
                stage == "INTENT_RECOGNITION"
                and str(event.get("progress_phase") or "") in {
                    "V2_SEMANTIC_CATALOG_READY",
                    "V2_SEMANTIC_CANDIDATES_READY",
                }
            ):
                # These events remain available to timing/telemetry handlers,
                # but they describe catalog/binding internals rather than
                # user decisions and must not enter the public SSE document.
                return []
            if (
                stage == "INTENT_RECOGNITION"
                and event.get("progress_phase") == "V2_RESOLVED_INTENT_CONTEXT_READY"
            ):
                if question_completion_displayed:
                    return []
                # The completed question is the public intent/planning boundary.
                # Later classification milestones still drive internal ordering,
                # but must not reopen the intent node or its heartbeat.
                question_completion_displayed = True
                intent_completed = True
                # 补全问题即进入规划阶段：心跳等后续事件随规划节点展示，
                # 提示行本身改由规划块展示前固定补发
                latest_progress_stage = "TASK_PLANNING"
                if off_topic_flag:
                    # 业务无关的问题由聊天模型直接回答，不亮任务规划节点
                    return [dict(event, status="COMPLETED")]
                if bool(event.get("definition_direct")):
                    # 单一问题的解释口径：意图识别后跳过任务拆分与规划，
                    # 直接依据语义文件作答，规划节点对这轮不再点亮
                    latest_progress_stage = "INTENT_RECOGNITION"
                    return [dict(event, status="COMPLETED")]
                return [
                    dict(event, status="COMPLETED"),
                ]
            if stage == "TASK_PLANNING" and event.get("data_source_verdict"):
                # 追问的数据来源结论：快照复用要说明为什么不查库，保留并
                # 替换规划块首行；判定为新查询时和参数提取里的
                # "是否查询数据库"标注一致，不再重复说明
                if "不再发起新查询" in message_text:
                    planning_data_verdict = message_text
                    planning_data_operation_text = str(
                        event.get("dataset_operation_text") or ""
                    )
                return []
            if stage == "TASK_PLANNING" and (
                not planning_released
                or (event.get("task_table") and not composite_mode)
            ):
                # 单问题桥接轮的规划摘要在 completeness 提前释放后到达，
                # 要扣住等快照结论到齐再经改写函数下发，否则复用轮的
                # 任务名和数据来源标记来不及纠正；
                # 复合流程的摘要在意图完结后直通渲染，扣住会乱序
                if str(event.get("status") or "").upper() == "COMPLETED":
                    deferred_planning[:] = [event]
                    if intent_completed:
                        latest_progress_stage = "TASK_PLANNING"
                    return []
                if not deferred_planning:
                    deferred_planning.append(event)
                if intent_completed:
                    # Preserve the deployed early planning-running display;
                    # only the completed plan waits for readiness.
                    latest_progress_stage = "TASK_PLANNING"
                    # 这条RUNNING已实时展示，不能留在延迟区；否则复合意图
                    # 完结或执行边界重放延迟事件时，同一行会出现两遍。
                    if event in deferred_planning:
                        deferred_planning.remove(event)
                    return [event]
                return []
            if (
                stage == "INTENT_RECOGNITION"
                and event.get("status") == "COMPLETED"
            ):
                intent_completed = True
                presentation_scenario = str(
                    event.get("presentation_scenario") or "ANALYTIC"
                ).upper()
                if bool(event.get("is_composite")):
                    composite_mode = True
                    ordered = [
                        event,
                        *(
                            item
                            for deferred in deferred_planning
                            for item in _with_planning_verdict(deferred)
                        ),
                    ]
                    deferred_planning.clear()
                    planning_released = True
                    # 提示行插在意图完结事件之后、规划块内容之前；复合轮的
                    # 规划事件在意图完结后直达渲染、不再进扣留区，扣留区为空
                    # 时提示行在这里先补发，否则复合轮整轮都没有这一行
                    tail = _with_planning_start_line(ordered[1:])
                    if not tail and not planning_start_emitted:
                        planning_start_emitted = True
                        tail = [_planning_start_event()]
                    return [ordered[0], *tail]
                pending_planning_runs = [
                    item for item in deferred_planning
                    if str(item.get("status") or "").upper() != "COMPLETED"
                ]
                if pending_planning_runs:
                    deferred_planning[:] = [
                        item for item in deferred_planning if item not in pending_planning_runs
                    ]
                    return [
                        event,
                        *_with_planning_start_line(pending_planning_runs),
                    ]
            if (
                composite_mode
                and bool(event.get("is_child_task"))
                and stage in {"COMPLETENESS_CHECK", "CLARIFICATION_EXECUTION", "CLARIFICATION_RESULT"}
            ):
                # A child's clarification is not a global stop. Its task-local
                # terminal milestone is rendered in the execution section.
                return []
            if (
                composite_mode
                and bool(event.get("is_child_task"))
                and _thinking_section(stage) in {
                    "parsing", "execution", "validation", "insight",
                }
            ):
                # Child tasks execute concurrently. Publish execution events as
                # they happen, then open validation and insight only when every
                # child has crossed the preceding stage boundary. This keeps the
                # document headings stable without replaying all child progress
                # after the DAG has already finished.
                return composite_child_orderer.push(event)
            if (
                stage == "FILE_INSPECTION"
                and event.get("status") == "COMPLETED"
            ):
                file_inspection_completed = True
                return [event] if bool(event.get("file_based")) else []
            if stage == "COMPLETENESS_CHECK":
                ordered: list[dict[str, Any]] = []
                needs_input = (
                    str(event.get("status") or "").upper() == "NEEDS_INPUT"
                )
                if not needs_input and presentation_scenario == "CLARIFICATION":
                    # Live semantic recovery may fill the provisional missing
                    # slot after intent display. Once execution is authorized,
                    # use the normal data-task section family consistently.
                    presentation_scenario = "ANALYTIC"
                if deferred_planning:
                    if needs_input:
                        presentation_scenario = "CLARIFICATION"
                        planning = dict(deferred_planning[-1])
                        # Keep the same task / intent / domain / parameters
                        # layout when asking for missing input. Only replace
                        # the decision line; no executable plan is promised.
                        verdict_pieces = _with_planning_verdict(
                            deferred_planning[-1]
                        )
                        task_details = "\n".join(
                            str(item.get("message") or "")
                            for item in verdict_pieces[1:]
                        ).strip()
                        decision_parts = ["当前任务参数不完整，暂停子任务拆分。"]
                        if planning_data_verdict:
                            # 追问结论保留在判定行，参数不完整紧随其后
                            decision_parts.insert(0, planning_data_verdict)
                        if task_details:
                            decision_parts.append(task_details)
                        planning["message"] = "\n".join(decision_parts).rstrip()
                        ordered.extend(_with_planning_start_line(
                            _split_planning_summary(planning)
                        ))
                    else:
                        ordered.extend(_with_planning_start_line(
                            _with_planning_verdict(deferred_planning[-1])
                        ))
                    deferred_planning.clear()
                    planning_released = True
                # Completeness is expressed inside the document-defined intent
                # and planning blocks; do not render an extra unlabeled line.
                return ordered
            if _thinking_section(stage) in {"parsing", "execution"} and deferred_planning:
                # Public analytic sections have a fixed order. Some execution
                # adapters emit ASL/SQL milestones before DATA_RETRIEVAL; release
                # the completed planning block before the first such milestone
                # instead of appending planning after tool output.
                ordered = [
                    *_with_planning_start_line(
                        list(_with_planning_verdict(deferred_planning[-1]))
                    ),
                    event,
                ]
                deferred_planning.clear()
                planning_released = True
                return ordered
            if stage in {
                "REQUEST_VALIDATION",
                "RESPONSE_CACHE",
                "CONTEXT_RESTORE",
                "QUESTION_REWRITE",
                "MEMORY_RETRIEVAL",
                "DETERMINISTIC_ANALYSIS",
                "ANSWER_SYNTHESIS",
            }:
                return []
            return [event]

        async def stream_progress_item(
            item: tuple[dict[str, Any], asyncio.Future[None]],
        ) -> AsyncIterator[str]:
            nonlocal latest_progress_stage
            progress_event, rendered = item
            try:
                visible_progress = ordered_progress(progress_event)
                for event in visible_progress:
                    if (
                        question_completion_displayed
                        and event.get("stage") == "INTENT_RECOGNITION"
                        and event.get("progress_phase") != "V2_RESOLVED_INTENT_CONTEXT_READY"
                    ):
                        if not str(event.get("message") or "").strip():
                            # 空的意图完结事件不转挂规划节点：解释口径等
                            # 直答轮不再出现空的“任务拆分与规划”标题
                            continue
                        if _thinking_section(latest_progress_stage) not in {"intent", "planning"}:
                            continue
                        # Keep useful parameter/child-question summaries, under
                        # planning rather than emitting another intent node.
                        event = dict(event, stage="TASK_PLANNING")
                    candidate_progress_stage = str(
                        event.get("stage") or latest_progress_stage
                    ).strip().upper()
                    if event.get("progress_phase") == "V2_RESOLVED_INTENT_CONTEXT_READY":
                        # 补全问题边界事件本身不回推阶段：边界分支已把展示
                        # 阶段推进到规划，心跳等后续事件随规划节点走
                        candidate_progress_stage = latest_progress_stage
                    if _thinking_section(candidate_progress_stage) is not None:
                        latest_progress_stage = candidate_progress_stage
                    rendered_index = 0
                    async for rendered_event in stream_thinking(event):
                        yield rendered_event
                        rendered_index += 1
                        if rendered_index == 2 and not rendered.done():
                            # The node-state event and first visible text
                            # fragment have reached the ASGI response. Let the
                            # business coroutine continue while the remaining
                            # characters are paced; the single consumer still
                            # preserves queue and document order.
                            rendered.set_result(None)
                if not visible_progress:
                    for heartbeat_event in heartbeat_events_if_due():
                        yield heartbeat_event
            finally:
                if not rendered.done():
                    rendered.set_result(None)

        yield _event("updata_state", {
            "step": "",
            "data": "accepted",
            "message_id": external_message_id,
        })
        try:
            while not execution.done() or not progress_queue.empty():
                if not progress_queue.empty():
                    progress_item = progress_queue.get_nowait()
                    async for rendered_event in stream_progress_item(progress_item):
                        yield rendered_event
                    continue

                next_progress = asyncio.create_task(progress_queue.get())
                remaining_heartbeat_seconds = max(
                    0.05,
                    heartbeat_interval - (time.monotonic() - last_visible_at),
                )
                done, _ = await asyncio.wait(
                    {execution, next_progress},
                    timeout=remaining_heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_progress in done:
                    progress_item = next_progress.result()
                    async for rendered_event in stream_progress_item(progress_item):
                        yield rendered_event
                    continue
                next_progress.cancel()
                try:
                    await next_progress
                except asyncio.CancelledError:
                    pass
                if execution in done:
                    continue
                for heartbeat_event in heartbeat_events_if_due(force=True):
                    yield heartbeat_event

            response = await execution
            response.conversation_id = external_conversation_id
            for event in composite_child_orderer.flush():
                async for rendered_event in stream_thinking(event):
                    yield rendered_event
            for deferred_item in deferred_planning:
                # 兜底释放也要过改写函数：正常轮原样透传，快照轮补任务名纠正
                for piece in _with_planning_start_line(
                    _with_planning_verdict(deferred_item)
                ):
                    async for rendered_event in stream_thinking(piece):
                        yield rendered_event
            deferred_planning.clear()
            response_scenario = (
                "METADATA"
                if response.intent == PrimaryIntent.METRIC_DEFINITION and (
                    response.execution_shape != 'COMPOSITE' or all(
                        task.intent == PrimaryIntent.METRIC_DEFINITION for task in response.task_results))
                else "ANALYTIC"
                if response.execution_shape == "COMPOSITE"
                else "CHAT"
                if response.intent == PrimaryIntent.CHAT
                else "CLARIFICATION"
                if response.status == "NEEDS_CLARIFICATION"
                else "ANALYTIC"
                if presentation_scenario == "CLARIFICATION"
                else presentation_scenario
            )
            presentation_scenario = response_scenario
            if response_scenario in {"CHAT", "CLARIFICATION", "METADATA"} or response.execution_shape == "COMPOSITE":
                async for rendered_event in stream_thinking({
                    "stage": "FINAL_OUTPUT",
                    "status": "COMPLETED",
                    "presentation_scenario": response_scenario,
                    "message": "",
                    "heading_only": True,
                }):
                    yield rendered_event
            # New_Agent consumers switch from thinking to the answer panel on
            # this state event. A step="output" chunk alone is not a state
            # transition; otherwise a completed table can remain invisible.
            # Emit after the final thinking heading, before any output payload.
            yield _event("updata_state", {"step": "", "data": "output"})
            for extension in response.extension_executions:
                tool_content = (
                    json.dumps(extension.output, ensure_ascii=False, default=str)
                    if extension.output is not None
                    else extension.error or ""
                )
                yield _event("tool_result", {
                    "step": "extension",
                    "name": extension.name,
                    "content": tool_content,
                    "status": "success" if extension.status == "COMPLETED" else "error",
                    "status_code": (
                        extension.status_code
                        if extension.status_code is not None
                        else 200 if extension.status == "COMPLETED" else 502
                    ),
                    "error_type": (
                        extension.error_type
                        or ("" if extension.status == "COMPLETED" else extension.status)
                    ),
                })
            if response.answer:
                chunks = _answer_chunks(response.answer)
                chunk_delay = _answer_chunk_delay(len(chunks))
                for index, content in enumerate(chunks):
                    yield _event(
                        "message_chunk",
                        {
                            "step": "output",
                            "index": index,
                            "content": content,
                            "role": "assistant",
                            "node": "data_analysis",
                            "is_last": index == len(chunks) - 1,
                        },
                    )
                    if chunk_delay:
                        await asyncio.sleep(chunk_delay)
                yield _event("answer", {
                    "step": "output",
                    "content": response.answer,
                    "status": response.status,
                })
            final_payload = response.model_dump(mode="json")
            final_payload.update({"step": "output", "content": ""})
            yield _event("complete", final_payload)
        except asyncio.CancelledError:
            execution.cancel()
            raise
        except HTTPException as exc:
            yield _event("error", _stream_error_payload(exc))
        except Exception:
            logger.exception("unhandled data-analysis SSE execution failure")
            yield _event(
                "error",
                {
                    "code": "INTERNAL_ERROR",
                    "message": "数据分析服务执行异常，请稍后重试。",
                    "retryable": True,
                },
            )
        finally:
            if not execution.done():
                execution.cancel()
            try:
                await execution
            except (asyncio.CancelledError, Exception):
                pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/agent_chat/refresh/stream",
    response_class=StreamingResponse,
    summary="SSE 刷新或修改重提当前会话最后一个问题",
    description=(
        "仅操作当前会话最后一个问题。刷新时 question 保持不变；修改重提时"
        "question 传新问题、original_question 传修改前问题。history 和"
        "replaces_message_id 均非必填，响应格式与 /agent_chat/stream 相同。"
    ),
)
async def chat_refresh_stream(
    payload: ChatRequest,
    request: Request,
    x_roles: str | None = Header(default=None),
) -> StreamingResponse:
    payload.regenerate = True
    return await chat_stream(payload, request, x_roles)


def _event(name: str, data: dict) -> str:
    """Serialize the platform's data-only SSE envelope used by New_Agent."""
    payload = {"type": name, **data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _progress_heartbeat_events(
    stage: str,
    *,
    elapsed_seconds: float,
) -> list[str]:
    """Keep the SSE connection and the platform's visible stage both alive.

    New_Agent's page consumes ``<stage>`` markers carried by the existing
    ``message_chunk`` contract.  A raw ``updata_state`` heartbeat alone is not
    rendered, so a long model or tool call otherwise looks frozen even while
    the request is healthy.  This reports elapsed work only; it does not claim
    that a semantic or execution milestone has completed.
    """

    normalized_stage = (stage or "INTENT_RECOGNITION").strip().upper()
    elapsed_raw = max(0.0, float(elapsed_seconds))
    elapsed = round(elapsed_raw, 1)
    message = _progress_waiting_message(normalized_stage)
    meta = {
        "stage": normalized_stage,
        "status": "RUNNING",
        "elapsed_seconds": elapsed,
    }
    return [
        _event("message_chunk", {
            "step": _new_agent_think_step(normalized_stage),
            # Some deployed platform adapters suppress an ``updata_state``
            # heartbeat together with the following synthetic final chunk.
            # Emit the visible wait state as an ordinary, non-final think
            # fragment with a changing index so it follows the same path as
            # all other progressively rendered thinking content.
            "index": max(1, int(round(elapsed_raw * 1000))),
            "content": (
                f"<stage>⏳ {message}（已用时 {elapsed:g} 秒）...</stage>"
            ),
            "role": "assistant",
            "node": "",
            "is_last": False,
            "meta": meta,
        }),
    ]


def _progress_waiting_message(stage: str) -> str:
    section = _thinking_section(stage.strip().upper())
    return {
        "intent": "正在识别问题中的指标、维度与筛选条件",
        "file": "正在读取并解析文件内容",
        "planning": "正在生成任务拆分与调用计划",
        "parsing": "正在解析并校验 ASL 参数",
        "execution": "正在翻译 SQL 或读取数据",
        "validation": "正在校验查询结果和数据质量",
        "insight": "正在生成分析洞察和图表",
        "clarification_execution": "正在处理补充信息",
        "clarification_result": "正在整理补充信息的处理结果",
        "final_output": "正在生成最终结果",
    }.get(section or "", "当前处理仍在进行")


def _thinking_events(
    progress: dict[str, Any],
    *,
    heading: str | None = None,
    chunk_size: int = 4,
    max_chunks: int = 120,
) -> list[str]:
    """Render one node milestone as state plus progressive text SSE events.

    Producers may publish truthful RUNNING milestones before the validated
    result. This function splits each public milestone at the HTTP boundary,
    preserving exact content, order and metadata while allowing the browser to
    paint a few Unicode characters at a time.
    """

    stage = str(progress.get("stage") or "processing")
    # Keep the transport labels identical to New_Agent.  The platform-side
    # stream renderer does not understand data-agent-specific step names such
    # as ``intent_recognition`` or ``data_retrieval``; it groups think content
    # by the canonical New_Agent nodes below.
    step = _new_agent_think_step(stage)
    state_payload = {"step": "", "data": step}
    if progress.get("task_table"):
        # 任务表存进state状态：任务名、是否查询数据库、依赖都在表里
        state_payload["task_table"] = progress["task_table"]
    state_event = _event("updata_state", state_payload)
    if state_payload.get("task_table") and bool(progress.get("heading_only")):
        # 纯任务表状态刷新事件没有正文，只下发state，避免渲染出空消息分片
        return [state_event]
    content = (
        ""
        if bool(progress.get("heading_only"))
        else str(progress.get("message") or stage).strip()
    )
    # Remove node-owned headings, then emit one normalized public heading for
    # each of the seven data-agent stages at the SSE boundary.
    content = re.sub(r"^\s*#{1,6}\s+[^\r\n]+(?:\r?\n)?", "", content).strip()
    content = _markdown_hard_line_breaks(content)
    if heading:
        content = f"{heading}\n\n{content}" if content else heading
    # Keep each body milestone in a fresh block so adjacent chunks are not
    # concatenated into a single line by the platform renderer.
    content = f"\n\n{content}\n\n"
    chunks = _thinking_chunks(
        content,
        chunk_size=chunk_size,
        max_chunks=max_chunks,
    )
    # progress_phase 只对外的四类关键提示行暴露：补全问题、判断问题类型、
    # 任务拆分启动、拆分判断完成；其余内部中间步骤的 phase 不出传输层
    public_phase = progress.get("progress_phase") in _PUBLIC_PROGRESS_PHASES
    message_events: list[str] = []
    for index, chunk in enumerate(chunks):
        chunk_meta = progress if index == 0 else {
            key: progress[key]
            for key in (
                "stage",
                "status",
                "progress_phase",
                "is_child_task",
                "task_id",
                "task_index",
                "task_count",
                "presentation_scenario",
            )
            if key in progress
        }
        if not public_phase:
            chunk_meta = dict(chunk_meta)
            chunk_meta.pop("progress_phase", None)
        message_payload: dict[str, Any] = {
            "step": step,
            "index": index,
            "content": chunk,
            "role": "assistant",
            "node": "",
            "is_last": index == len(chunks) - 1,
            # Keep the complete diagnostic metadata on the first fragment only.
            # Repeating an ASL-sized ``meta.message`` in every tiny transport
            # chunk would multiply response bytes without helping the renderer.
            "meta": chunk_meta,
        }
        if step == "execute_exe":
            # New_Agent associates execution records with a task.  A single
            # data query is task 0; multi-task details remain available in meta.
            message_payload["task_index"] = int(progress.get("task_index") or 0)
        message_events.append(_event("message_chunk", message_payload))
    return [state_event, *message_events]


def _thinking_event(
    progress: dict[str, Any], *, heading: str | None = None
) -> str:
    """Backward-compatible serialized form for callers that need one string."""

    return "".join(_thinking_events(progress, heading=heading))


def _thinking_chunks(
    content: str,
    *,
    chunk_size: int = 4,
    max_chunks: int = 120,
) -> list[str]:
    """Split thinking text into small chunks with a bounded event count."""

    if not content:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if max_chunks <= 0:
        raise ValueError("max_chunks must be greater than zero")
    effective_size = max(chunk_size, (len(content) + max_chunks - 1) // max_chunks)
    return [
        content[index:index + effective_size]
        for index in range(0, len(content), effective_size)
    ]


def _thinking_chunk_delay(
    chunk_count: int, *, interval_seconds: float = 0.03
) -> float:
    """Return a paintable cadence for successive SSE text fragments."""

    if chunk_count <= 1 or interval_seconds <= 0:
        return 0.0
    return interval_seconds


def _markdown_hard_line_breaks(content: str) -> str:
    """Preserve document-style logical lines in CommonMark renderers.

    A plain newline inside one Markdown paragraph is rendered as a space by
    CommonMark.  Public thinking messages are intentionally sent as one SSE
    chunk per milestone, so mark adjacent non-empty logical lines as hard
    breaks while keeping existing blank-line paragraph boundaries intact.
    """

    lines = content.splitlines()
    rendered: list[str] = []
    for index, line in enumerate(lines):
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        rendered.append(f"{line}  " if line and next_line else line)
    return "\n".join(rendered)


# 对外只暴露这四类提示行的阶段标识（前端按 phase 区分展示），
# 其余内部中间步骤的 phase 不进传输层
_PUBLIC_PROGRESS_PHASES = frozenset({
    "V2_CONTEXT_START",
    "V2_COMPOUND_JUDGE_START",
    "V2_TASK_PLANNING_START",
    "V2_PLANNING_SUMMARY",
})


def _thinking_section(stage: str) -> str | None:
    return {
        "INTENT_RECOGNITION": "intent",
        "FILE_INSPECTION": "file",
        "TASK_PLANNING": "planning",
        "DATA_RETRIEVAL": "execution",
        "SEMANTIC_QUERY_PLANNING": "execution",
        "ASL_GENERATION": "parsing",
        "SQL_TRANSLATION": "execution",
        "SQL_EXECUTION": "execution",
        "ANALYSIS_TOOL": "execution",
        "KNOWLEDGE_RETRIEVAL": "execution",
        "EXTERNAL_SEARCH": "execution",
        "RELIABILITY_CHECK": "validation",
        "INSIGHT_ANALYSIS": "insight",
        "CLARIFICATION_EXECUTION": "clarification_execution",
        "CLARIFICATION_RESULT": "clarification_result",
        "FINAL_OUTPUT": "final_output",
    }.get(stage)


class _CompositeChildProgressOrderer:
    """Release concurrent child progress at truthful public stage barriers."""

    _TERMINAL_STATUSES = {"COMPLETED", "DEGRADED", "FAILED", "SKIPPED"}

    def __init__(self) -> None:
        self.expected_task_count = 0
        self.execution_completed: set[str] = set()
        self.validation_completed: set[str] = set()
        self.execution_barrier_open = False
        self.validation_barrier_open = False
        self.deferred_validation: list[dict[str, Any]] = []
        self.deferred_insight: list[dict[str, Any]] = []
        self.labelled_tasks: set[tuple[str, int]] = set()
        self.execution_pending: dict[int, list[dict[str, Any]]] = {}
        self.execution_finished_indices: set[int] = set()
        self.next_execution_index = 0
        self.parsing_completed: set[str] = set()
        self.parsing_stopped: set[str] = set()
        self.parsing_barrier_open = False
        self.deferred_execution: list[dict[str, Any]] = []
        # 并发子任务的解析校验块按任务序号发布：各任务ASL完成顺序不稳定，
        # 到达即发布会让“任务2”先于“任务1”出现（2026-10-22 用户反馈）。
        self.parsing_pending: dict[int, list[dict[str, Any]]] = {}
        self.parsing_finished_indices: set[int] = set()
        self.next_parsing_index = 0

    @staticmethod
    def _parsing_task_index(event: dict[str, Any]) -> int:
        try:
            return max(0, int(event.get("task_index") or 0))
        except (TypeError, ValueError):
            return 0

    def _is_parsing_terminal(self, event: dict[str, Any]) -> bool:
        return str(event.get("status") or "").upper() in (
            self._TERMINAL_STATUSES | {"NEEDS_INPUT"}
        )

    def _advance_parsing(self) -> list[dict[str, Any]]:
        """Release buffered parsing blocks in task order as earlier ones end."""
        released: list[dict[str, Any]] = []
        while True:
            index = self.next_parsing_index
            buffered = self.parsing_pending.pop(index, None)
            if buffered:
                released.extend(self._label(buffered))
                if not any(self._is_parsing_terminal(item) for item in buffered):
                    # 该任务的解析流还没结束，后续事件到齐前保持当前序号
                    break
            elif index not in self.parsing_finished_indices:
                break
            self.next_parsing_index = index + 1
        return released

    def _mark_parsing_crossed(self, index: int) -> list[dict[str, Any]]:
        """Execution milestones imply the task's parsing (if any) has finished."""
        if index < self.next_parsing_index:
            return []
        self.parsing_finished_indices.add(index)
        if index == self.next_parsing_index:
            return self._advance_parsing()
        return []

    @staticmethod
    def _task_key(event: dict[str, Any]) -> str:
        task_id = str(event.get("task_id") or "").strip()
        if task_id:
            return task_id
        try:
            return f"task-index-{max(0, int(event.get('task_index') or 0))}"
        except (TypeError, ValueError):
            return "task-index-0"

    def _observe_task_count(self, event: dict[str, Any]) -> None:
        try:
            self.expected_task_count = max(
                self.expected_task_count, int(event.get("task_count") or 0)
            )
        except (TypeError, ValueError):
            pass

    def _all_tasks_completed(self, completed: set[str]) -> bool:
        return bool(
            self.expected_task_count > 1
            and len(completed) >= self.expected_task_count
        )

    def _label(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _ordered_composite_child_progress_events(
            [event for event in events if not event.get('metadata_only')], labelled_tasks=self.labelled_tasks
        )

    def _open_validation_if_ready(self) -> list[dict[str, Any]]:
        if (
            self.execution_barrier_open
            or not self._all_tasks_completed(self.execution_completed)
        ):
            return []
        self.execution_barrier_open = True
        released = self._label(self.deferred_validation)
        self.deferred_validation.clear()
        released.extend(self._open_insight_if_ready())
        return released

    def _open_insight_if_ready(self) -> list[dict[str, Any]]:
        if (
            not self.execution_barrier_open
            or self.validation_barrier_open
            or not self._all_tasks_completed(self.validation_completed)
        ):
            return []
        self.validation_barrier_open = True
        released = self._label(self.deferred_insight)
        self.deferred_insight.clear()
        return released

    def push(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep all child ASL output above SQL without delaying actual tools."""
        self._observe_task_count(event)
        section = _thinking_section(str(event.get("stage") or "").upper())
        task_key = self._task_key(event)
        visible: list[dict[str, Any]] = []
        if section == "parsing":
            status = str(event.get("status") or "").upper()
            task_index = self._parsing_task_index(event)
            if task_index < self.next_parsing_index:
                # 已按序发布过的任务的补充里程碑，按到达顺序直通
                visible.extend(self._label([event]))
            elif task_index == self.next_parsing_index:
                visible.extend(self._label([event]))
                if self._is_parsing_terminal(event):
                    self.parsing_finished_indices.add(task_index)
                    visible.extend(self._advance_parsing())
            else:
                self.parsing_pending.setdefault(task_index, []).append(event)
                if self._is_parsing_terminal(event):
                    self.parsing_finished_indices.add(task_index)
            if status in {"NEEDS_INPUT", "FAILED"}:
                self.parsing_stopped.add(task_key)
            if status in self._TERMINAL_STATUSES | {"NEEDS_INPUT"}:
                self.parsing_completed.add(task_key)
        else:
            if section == "execution":
                # Tasks with no ASL (files, skipped tasks) also cross the
                # parsing barrier; don't invent a successful ASL for them.
                self.parsing_completed.add(task_key)
                visible.extend(self._mark_parsing_crossed(
                    self._parsing_task_index(event)
                ))
            if not self.parsing_barrier_open:
                self.deferred_execution.append(event)
            else:
                return [*visible, *self._push_after_parsing(event)]
        if not self.parsing_barrier_open and self._all_tasks_completed(self.parsing_completed):
            self.parsing_barrier_open = True
            for pending in self.deferred_execution:
                visible.extend(self._push_after_parsing(pending))
            self.deferred_execution.clear()
        return visible

    def _push_after_parsing(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        current = dict(event)
        self._observe_task_count(current)
        stage = str(current.get("stage") or "").upper()
        status = str(current.get("status") or "").upper()
        section = _thinking_section(stage)
        task_key = self._task_key(current)

        if section == "execution":
            try:
                task_index = max(0, int(current.get("task_index") or 0))
            except (TypeError, ValueError):
                task_index = 0
            terminal = bool(current.get("task_terminal"))
            duplicate_terminal = (
                terminal and task_key in self.execution_completed
            )
            if not duplicate_terminal and not (terminal and task_key in self.parsing_stopped):
                self.execution_pending.setdefault(task_index, []).append(current)
            if (
                stage == "DATA_RETRIEVAL"
                and status in self._TERMINAL_STATUSES
            ):
                self.execution_completed.add(task_key)
                self.execution_finished_indices.add(task_index)
            if terminal:
                # No fake validation/insight events for a skipped task; it
                # merely cannot hold successful siblings behind the barrier.
                self.validation_completed.add(task_key)
            visible = []
            while self.next_execution_index < self.expected_task_count:
                visible.extend(self._label(self.execution_pending.pop(self.next_execution_index, [])))
                if self.next_execution_index not in self.execution_finished_indices:
                    break
                self.next_execution_index += 1
            # Preserve any additional adapter execution milestone emitted
            # after this task's ordinary retrieval output was released.
            if task_index < self.next_execution_index:
                visible.extend(self._label(self.execution_pending.pop(task_index, [])))
            visible.extend(self._open_validation_if_ready())
            visible.extend(self._open_insight_if_ready())
            return visible

        if section == "validation":
            if status in self._TERMINAL_STATUSES:
                self.validation_completed.add(task_key)
            if not self.execution_barrier_open:
                self.deferred_validation.append(current)
                return []
            visible = self._label([current])
            visible.extend(self._open_insight_if_ready())
            return visible

        if section == "insight":
            if not self.validation_barrier_open:
                self.deferred_insight.append(current)
                return []
            return self._label([current])
        return [current]

    def flush(self) -> list[dict[str, Any]]:
        released = []
        for index in sorted(self.parsing_pending):
            released.extend(self._label(self.parsing_pending.pop(index)))
        self.parsing_pending.clear()
        for event in self.deferred_execution:
            released.extend(self._push_after_parsing(event))
        self.deferred_execution.clear()
        pending = [
            *(event for index in sorted(self.execution_pending) for event in self.execution_pending[index]),
            *self.deferred_validation, *self.deferred_insight,
        ]
        self.execution_pending.clear()
        self.deferred_validation.clear()
        self.deferred_insight.clear()
        return [*released, *self._label(pending)]


def _ordered_composite_child_progress_events(
    events: list[dict[str, Any]],
    *,
    labelled_tasks: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Keep concurrent child milestones inside their public UI sections."""

    section_order = {"parsing": -1, "execution": 0, "validation": 1, "insight": 2}

    def sort_key(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        sequence, event = indexed
        section = _thinking_section(str(event.get("stage") or "").upper())
        try:
            task_index = max(0, int(event.get("task_index") or 0))
        except (TypeError, ValueError):
            task_index = 0
        return section_order.get(section or "", 99), task_index, sequence

    ordered = [event for _, event in sorted(enumerate(events), key=sort_key)]
    labelled_tasks = labelled_tasks if labelled_tasks is not None else set()
    rendered: list[dict[str, Any]] = []
    for event in ordered:
        current = dict(event)
        section = _thinking_section(str(current.get("stage") or "").upper())
        try:
            task_index = max(0, int(current.get("task_index") or 0))
        except (TypeError, ValueError):
            task_index = 0
        task_key = (section or "", task_index)
        if (
            section in {"parsing", "execution", "validation", "insight"}
            and task_key not in labelled_tasks
        ):
            labelled_tasks.add(task_key)
            message = str(current.get("message") or "").strip()
            if section == "execution":
                message = re.sub(
                    r"^\s*#{1,6}\s+[^\r\n]+(?:\r?\n)?", "", message
                ).strip()
                if message.startswith("执行链路："):
                    message = message.removeprefix("执行链路：")
                current["message"] = (
                    f"任务{task_index + 1}：{current.get('task_question') or ''}\n{message}"
                    if current.get("task_terminal")
                    else f"任务{task_index + 1}执行链路：{message}"
                )
            else:
                task_question = str(current.get("task_question") or "").strip()
                task_label = f"任务{task_index + 1}"
                if task_question:
                    task_label += f"：{task_question}"
                current["message"] = (
                    f"{task_label}\n{message}" if message else task_label
                )
        rendered.append(current)
    return rendered


def _thinking_title(section: str, scenario: str = "ANALYTIC") -> str:
    normalized_scenario = scenario.strip().upper()
    if normalized_scenario == "CLARIFICATION":
        scenario_titles = {
            "intent": "#### ◉ 意图识别",
            "planning": "#### 2、任务拆分与规划",
            "clarification_execution": "#### 3、调研执行",
            "clarification_result": "#### 4、结果生成",
            "final_output": "#### 5、最终输出",
        }
        if section in scenario_titles:
            return scenario_titles[section]
    if normalized_scenario == "CHAT":
        scenario_titles = {
            "intent": "#### ◉ 意图识别",
            "final_output": "#### 2、最终输出",
        }
        if section in scenario_titles:
            return scenario_titles[section]
    return {
        "intent": "#### ◉ 意图识别",
        "file": "#### ◉ 文件感知与解析",
        "planning": "#### ◉ 任务拆分与规划",
        "parsing": "#### ◉ 解析校验",
        "execution": "#### ◉ 调度执行",
        "validation": "#### ◉ 结果校验",
        "insight": "#### ◉ 数据洞察分析",
        "clarification_execution": "#### 3、调研执行",
        "clarification_result": "#### 4、结果生成",
        "final_output": "#### ◉ 最终输出",
    }[section]


def _heading_event_is_visible_summary(
    section: str, event: dict[str, Any]
) -> bool:
    # Open every section on its first truthful milestone. In particular, the
    # intent node must become visible before structured model validation ends;
    # otherwise a 30-second model call looks like a stalled request even though
    # SSE is connected and work is progressing.
    return True


def _new_agent_think_step(stage: str) -> str:
    """Map internal milestones to New_Agent's public think-tag protocol."""

    normalized = stage.strip().upper()
    if normalized in {"TASK_PLANNING", "TASK_STATUS"}:
        return "execute_plan"
    if normalized in {
        "DATA_RETRIEVAL",
        "SEMANTIC_QUERY_PLANNING",
        "ASL_GENERATION",
        "SQL_TRANSLATION",
        "SQL_EXECUTION",
        "ANALYSIS_TOOL",
        "KNOWLEDGE_RETRIEVAL",
        "EXTERNAL_SEARCH",
    }:
        return "execute_exe"
    if normalized in {
        "DETERMINISTIC_ANALYSIS",
        "ANSWER_SYNTHESIS",
        "RELIABILITY_CHECK",
        "INSIGHT_ANALYSIS",
        "CLARIFICATION_EXECUTION",
        "CLARIFICATION_RESULT",
        "FINAL_OUTPUT",
    }:
        return "response_result"
    # Validation, context restoration, question completion, intent extraction,
    # memory retrieval and clarification checks all belong to New_Agent step1.
    return "step1"


def _answer_chunks(answer: str, *, chunk_size: int = 6) -> list[str]:
    """Split only the already validated final answer into transport chunks.

    The Qwen synthesis result is structured and evidence-validated before it
    reaches this function.  Streaming unvalidated model tokens would bypass
    those safety gates, so chunks are produced from the approved answer.  Use
    the same fixed six-Unicode-character slicing rule as New_Agent's
    ``stream_text_to_frontend`` transport helper; only the final chunk may be
    shorter.
    """

    if not answer:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    return [
        answer[index:index + chunk_size]
        for index in range(0, len(answer), chunk_size)
    ]


def _answer_chunk_delay(chunk_count: int) -> float:
    """Provide a subtle typing cadence without delaying long answers excessively."""

    if chunk_count <= 1:
        return 0.0
    return min(0.018, 0.9 / chunk_count)


def _stream_error_payload(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail
    code = "STREAM_EXECUTION_ERROR"
    message = "数据分析任务执行失败，请稍后重试。"
    if isinstance(detail, dict):
        code = str(detail.get("code") or code)[:100]
        message = str(detail.get("message") or message)[:500]
    elif exc.status_code == 504:
        code = "REQUEST_TIMEOUT"
        message = "数据分析任务执行超时，请稍后重试。"
    return {
        "code": code,
        "message": message,
        "status_code": exc.status_code,
        "retryable": exc.status_code >= 500,
    }


# ====================== 语义建模推荐接口（只读，供后端调用） ======================
# 路径、参数与 Resp 包装和 youoagent-api 的 Java 版保持一致：
#   GET /semanticModelEntityType/recommendTables
#   GET /semanticModelEntityType/recommendFields
#   GET /semanticModelEntityType/relationSuggestions
#   GET /semanticModelRelationConfig/detectRelations
#   GET /semanticModelRelationConfig/recommendRelations


def _recommend_resp(data: Any, code: int = 200, msg: str = "成功") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data, "timestamp": int(time.time() * 1000)}


async def _recommend_call(operation, *args) -> dict[str, Any]:
    """同步 DB/网关调用移出事件循环；业务错误统一转 Resp(code=500)。"""
    try:
        return _recommend_resp(await asyncio.to_thread(operation, *args))
    except semantic_recommend.SemanticRecommendError as exc:
        return _recommend_resp(None, 500, str(exc))
    except Exception:
        logger.exception("语义建模推荐接口执行失败: %s", operation.__name__)
        return _recommend_resp(None, 500, "推荐服务内部错误")


@recommend_router.get(
    "/semanticModelEntityType/recommendTables",
    summary="实体表推荐（语义建模画布：拖入空白实体时推荐可映射的物理表）",
)
async def recommend_tables_endpoint(
    semantic_model_id: int = Query(alias="semanticModelId", gt=0),
    data_source_id: int | None = Query(alias="dataSourceId", default=None, gt=0),
    keyword: str | None = Query(default=None, max_length=200),
    limit: int | None = Query(default=None, ge=1, le=200),
) -> dict[str, Any]:
    return await _recommend_call(
        semantic_recommend.get_service().recommend_tables,
        semantic_model_id, data_source_id, keyword, limit,
    )


@recommend_router.get(
    "/semanticModelEntityType/recommendFields",
    summary="实体属性推荐（推荐弹框选中表后加载字段清单）",
)
async def recommend_fields_endpoint(
    table_id: int = Query(alias="tableId", gt=0),
) -> dict[str, Any]:
    return await _recommend_call(
        semantic_recommend.get_service().recommend_fields, table_id
    )


@recommend_router.get(
    "/semanticModelEntityType/relationSuggestions",
    summary="实体保存时的关联建议（兼容接口，已停用中间表推荐）",
)
async def relation_suggestions_endpoint(
    semantic_model_id: int = Query(alias="semanticModelId", gt=0),
    entity_type_id: str = Query(alias="entityTypeId", min_length=1, max_length=64),
    business_domain_id: int | None = Query(alias="businessDomainId", default=None, gt=0),
) -> dict[str, Any]:
    return await _recommend_call(
        semantic_recommend.get_service().check_relation_suggestions,
        semantic_model_id, entity_type_id, business_domain_id,
    )


@recommend_router.get(
    "/semanticModelRelationConfig/detectRelations",
    summary="实体关联检测（拖入新实体时检测与已有实体的关联）",
)
async def detect_relations_endpoint(
    semantic_model_id: int = Query(alias="semanticModelId", gt=0),
    entity_type_id: str = Query(alias="entityTypeId", min_length=1, max_length=64),
    business_domain_id: int | None = Query(alias="businessDomainId", default=None, gt=0),
) -> dict[str, Any]:
    return await _recommend_call(
        semantic_recommend.get_service().detect_relations,
        semantic_model_id, entity_type_id, business_domain_id,
    )


@recommend_router.get(
    "/semanticModelRelationConfig/recommendRelations",
    summary="关系推荐（两个实体连线时推荐已配置关系与物理层推导候选）",
)
async def recommend_relations_endpoint(
    semantic_model_id: int = Query(alias="semanticModelId", gt=0),
    source_entity_type_id: str = Query(alias="sourceEntityTypeId", min_length=1, max_length=64),
    target_entity_type_id: str = Query(alias="targetEntityTypeId", min_length=1, max_length=64),
) -> dict[str, Any]:
    return await _recommend_call(
        semantic_recommend.get_service().recommend_relations,
        semantic_model_id, source_entity_type_id, target_entity_type_id,
    )
