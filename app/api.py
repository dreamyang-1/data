from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
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
    hash_identifier,
    observe_span,
    summarize_value,
    text_metadata,
    trace_attributes,
)
from app.services.file_ingestion import FileImportError
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import _progress_callback, progress_scope
from minio_followup_store import DatasetScope
from app.security import trusted_backend, require_application_namespace, resolve_conversation_identity


router = APIRouter(tags=["data-analysis"], dependencies=[Depends(trusted_backend)])
logger = logging.getLogger(__name__)


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
        "description": "智能体总处理时间超过服务端截止时间",
        "content": {
            "application/json": {
                "example": {"detail": "analysis request deadline exceeded"}
            }
        },
    },
}


def trusted_identity(request: Request, payload: ChatRequest | SpreadsheetImportRequest) -> TrustedIdentity:
    """State isolation identity, verified by the backend service dependency."""
    return resolve_conversation_identity(request, payload.application_id, payload.conversation_id)


async def invoke(request: Request, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
    require_application_namespace(request, chat.application_id)
    stage_tracker = StageSpanTracker()
    existing_progress = _progress_callback.get()

    def trace_progress(event: dict[str, Any]) -> Any:
        stage_tracker.handle(event)
        if existing_progress is not None:
            return existing_progress(event)
        return None

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
                try:
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
                        result = await asyncio.wait_for(
                            operation,
                            timeout=request.app.state.container.settings.request_timeout_seconds,
                        )
                    response = (
                        result
                        if isolated_handler is not None
                        else result["response"]
                    )
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
                    root_span.update(
                        output={"status": "TIMEOUT"},
                        level="ERROR",
                        status_message="analysis request deadline exceeded",
                    )
                    raise
                except MessageIdReuseConflictError:
                    root_span.update(
                        output={"status": "MESSAGE_ID_REUSE_CONFLICT"},
                        level="ERROR",
                        status_message="message id reuse conflict",
                    )
                    raise
                except Exception as exc:
                    root_span.update(
                        output={"status": "FAILED", "error_type": type(exc).__name__},
                        level="ERROR",
                        status_message=type(exc).__name__,
                    )
                    raise
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="analysis request deadline exceeded") from exc
    except MessageIdReuseConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
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

    async def events() -> AsyncIterator[str]:
        progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)

        async def publish_progress(event: dict[str, Any]) -> None:
            await progress_queue.put(event)

        async def execute() -> AgentResponse:
            with progress_scope(publish_progress):
                return await invoke(request, payload, identity)

        started_at = time.monotonic()
        execution = asyncio.create_task(execute())
        deferred_planning: list[dict[str, Any]] = []
        intent_completed = False
        file_inspection_completed = False
        planning_released = False
        presentation_scenario = "ANALYTIC"
        titled_think_sections: set[str] = set()
        composite_mode = False
        composite_child_progress: list[dict[str, Any]] = []

        def render_thinking(event: dict[str, Any]) -> str:
            stage = str(event.get("stage") or "processing").strip().upper()
            section = _thinking_section(stage)
            include_heading = bool(
                section
                and section not in titled_think_sections
                and _heading_event_is_visible_summary(section, event)
            )
            if include_heading:
                titled_think_sections.add(section)
            return _thinking_event(
                event,
                heading=(
                    _thinking_title(section, presentation_scenario)
                    if include_heading else None
                ),
            )

        def ordered_progress(event: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal intent_completed, file_inspection_completed
            nonlocal planning_released, presentation_scenario, composite_mode
            stage = str(event.get("stage") or "").upper()
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
            if stage == "TASK_PLANNING" and not planning_released:
                # The document format presents one stable planning block. Keep
                # the latest completed planner message and release it only once
                # completeness is known, so a missing-parameter request cannot
                # first claim that it will execute and then immediately stop.
                if str(event.get("status") or "").upper() == "COMPLETED":
                    deferred_planning[:] = [event]
                elif not deferred_planning:
                    deferred_planning.append(event)
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
                    ordered = [event, *deferred_planning]
                    deferred_planning.clear()
                    planning_released = True
                    return ordered
            if (
                composite_mode
                and bool(event.get("is_child_task"))
                and _thinking_section(stage) in {
                    "execution", "validation", "insight",
                }
            ):
                # Composite children execute concurrently, so their raw event
                # arrival order can be task-1 validation/insight followed by
                # task-2 SQL execution.  Rendering that order places the second
                # tool call under the already-open insight heading.  Buffer only
                # the public child milestones and release them in document
                # section order after the DAG finishes.  Execution remains
                # parallel; this is a presentation boundary only.
                composite_child_progress.append(dict(event))
                return []
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
                        planning["message"] = (
                            "当前任务参数不完整，暂停子任务拆分。\n"
                            "规划链路：终止 SQL 生成、数据库查询等后续取数流程，"
                            "输出追问话术收集缺失条件。"
                        )
                        ordered.append(planning)
                    else:
                        ordered.append(deferred_planning[-1])
                    deferred_planning.clear()
                    planning_released = True
                # Completeness is expressed inside the document-defined intent
                # and planning blocks; do not render an extra unlabeled line.
                return ordered
            if stage == "DATA_RETRIEVAL" and deferred_planning:
                ordered = [deferred_planning[-1], event]
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
        yield _event("updata_state", {
            "step": "",
            "data": "accepted",
            "message_id": external_message_id,
        })
        try:
            while not execution.done() or not progress_queue.empty():
                if not progress_queue.empty():
                    for event in ordered_progress(progress_queue.get_nowait()):
                        yield render_thinking(event)
                    continue

                next_progress = asyncio.create_task(progress_queue.get())
                done, _ = await asyncio.wait(
                    {execution, next_progress},
                    timeout=10.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_progress in done:
                    for event in ordered_progress(next_progress.result()):
                        yield render_thinking(event)
                    continue
                next_progress.cancel()
                try:
                    await next_progress
                except asyncio.CancelledError:
                    pass
                if execution in done:
                    continue
                yield _event("updata_state", {
                    "step": "",
                    "data": "heartbeat",
                    "message_id": external_message_id,
                    "elapsed_seconds": round(time.monotonic() - started_at, 1),
                })

            response = await execution
            response.conversation_id = external_conversation_id
            for event in _ordered_composite_child_progress_events(
                composite_child_progress
            ):
                yield render_thinking(event)
            composite_child_progress.clear()
            for event in deferred_planning:
                yield render_thinking(event)
            deferred_planning.clear()
            query_evidence = [
                item for item in response.evidence if item.kind == "QUERY_RESULT"
            ]
            analysis_evidence = [
                item for item in response.evidence if item.kind == "ANALYSIS_RESULT"
            ]
            response_scenario = (
                "CHAT"
                if response.intent == PrimaryIntent.CHAT
                else "CLARIFICATION"
                if response.status == "NEEDS_CLARIFICATION"
                else "ANALYTIC"
                if presentation_scenario == "CLARIFICATION"
                else presentation_scenario
            )
            presentation_scenario = response_scenario
            if response_scenario != "CLARIFICATION":
                yield render_thinking({
                    "stage": "OUTPUT_SUMMARY",
                    "status": "COMPLETED",
                    "presentation_scenario": response_scenario,
                    "message": (
                        "基于用户闲聊文本，由大模型直接生成自然语言闲聊回复，"
                        "不拼接报表、指标、表格等业务结果。"
                        if response_scenario == "CHAT"
                        else (
                            "任务状态：已完成；\n"
                            f"输出意图：{response.intent.value}。\n"
                            f"数据查询结果：{'已生成并保留证据' if query_evidence else '本轮无数据查询结果'}；"
                            f"数据分析结果：{'已生成' if analysis_evidence else '本轮未生成独立分析结论'}。\n"
                            f"附件：{len(response.files)} 个；图表：{len(response.chart_specs)} 个；"
                            f"证据：{len(response.evidence)} 项。"
                        )
                    ),
                    "file_count": len(response.files),
                    "chart_count": len(response.chart_specs),
                    "evidence_count": len(response.evidence),
                })
            if response_scenario in {"CHAT", "CLARIFICATION"}:
                yield render_thinking({
                    "stage": "FINAL_OUTPUT",
                    "status": "COMPLETED",
                    "presentation_scenario": response_scenario,
                    "message": "",
                    "heading_only": True,
                })
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


def _thinking_event(
    progress: dict[str, Any], *, heading: str | None = None
) -> str:
    stage = str(progress.get("stage") or "processing")
    # Keep the transport labels identical to New_Agent.  The platform-side
    # stream renderer does not understand data-agent-specific step names such
    # as ``intent_recognition`` or ``data_retrieval``; it groups think content
    # by the canonical New_Agent nodes below.
    step = _new_agent_think_step(stage)
    state_event = _event("updata_state", {
        "step": "",
        "data": step,
    })
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
    message_payload: dict[str, Any] = {
        "step": step,
        "content": content,
        "role": "assistant",
        "node": "",
        "meta": progress,
    }
    if step == "execute_exe":
        # New_Agent associates execution records with a task.  A single data
        # query is task 0; multi-task details remain available in meta.
        message_payload["task_index"] = int(progress.get("task_index") or 0)
    message_event = _event("message_chunk", message_payload)
    return state_event + message_event


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


def _thinking_section(stage: str) -> str | None:
    return {
        "INTENT_RECOGNITION": "intent",
        "FILE_INSPECTION": "file",
        "TASK_PLANNING": "planning",
        "DATA_RETRIEVAL": "execution",
        "SEMANTIC_QUERY_PLANNING": "execution",
        "ASL_GENERATION": "execution",
        "SQL_TRANSLATION": "execution",
        "SQL_EXECUTION": "execution",
        "KNOWLEDGE_RETRIEVAL": "execution",
        "EXTERNAL_SEARCH": "execution",
        "RELIABILITY_CHECK": "validation",
        "INSIGHT_ANALYSIS": "insight",
        "OUTPUT_SUMMARY": "summary",
        "CLARIFICATION_EXECUTION": "clarification_execution",
        "CLARIFICATION_RESULT": "clarification_result",
        "FINAL_OUTPUT": "final_output",
    }.get(stage)


def _ordered_composite_child_progress_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep concurrent child milestones inside their public UI sections."""

    section_order = {"execution": 0, "validation": 1, "insight": 2}

    def sort_key(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        sequence, event = indexed
        section = _thinking_section(str(event.get("stage") or "").upper())
        try:
            task_index = max(0, int(event.get("task_index") or 0))
        except (TypeError, ValueError):
            task_index = 0
        return section_order.get(section or "", 99), task_index, sequence

    return [
        event for _, event in sorted(enumerate(events), key=sort_key)
    ]


def _thinking_title(section: str, scenario: str = "ANALYTIC") -> str:
    normalized_scenario = scenario.strip().upper()
    if normalized_scenario == "CLARIFICATION":
        scenario_titles = {
            "intent": "#### 1、意图识别",
            "planning": "#### 2、任务拆分与规划",
            "clarification_execution": "#### 3、调研执行",
            "clarification_result": "#### 4、结果生成",
            "final_output": "#### 5、最终输出",
        }
        if section in scenario_titles:
            return scenario_titles[section]
    if normalized_scenario == "CHAT":
        scenario_titles = {
            "intent": "#### 1、意图识别",
            "summary": "#### 4、输出总结",
            "final_output": "#### 5、最终输出",
        }
        if section in scenario_titles:
            return scenario_titles[section]
    return {
        "intent": "#### 1、意图识别",
        "file": "#### ◉ 文件感知与解析",
        "planning": "#### ◉ 任务拆分与规划",
        "execution": "#### ◉ 调度执行",
        "validation": "#### ◉ 结果校验",
        "insight": "#### ◉ 数据洞察分析",
        "summary": "#### ◉ 输出总结",
        "clarification_execution": "#### 3、调研执行",
        "clarification_result": "#### 4、结果生成",
        "final_output": "#### 5、最终输出",
    }[section]


def _heading_event_is_visible_summary(
    section: str, event: dict[str, Any]
) -> bool:
    # The platform may collapse the short INTENT_RECOGNITION/RUNNING chunk.
    # Attach its heading to the completed structured summary so the title and
    # extracted fields are rendered together. Other stages keep their first
    # emitted event, matching the existing UI behavior.
    return section != "intent" or str(event.get("status") or "").upper() == "COMPLETED"


def _new_agent_think_step(stage: str) -> str:
    """Map internal milestones to New_Agent's public think-tag protocol."""

    normalized = stage.strip().upper()
    if normalized == "TASK_PLANNING":
        return "execute_plan"
    if normalized in {
        "DATA_RETRIEVAL",
        "SEMANTIC_QUERY_PLANNING",
        "ASL_GENERATION",
        "SQL_TRANSLATION",
        "SQL_EXECUTION",
        "KNOWLEDGE_RETRIEVAL",
        "EXTERNAL_SEARCH",
    }:
        return "execute_exe"
    if normalized in {
        "DETERMINISTIC_ANALYSIS",
        "ANSWER_SYNTHESIS",
        "RELIABILITY_CHECK",
        "INSIGHT_ANALYSIS",
        "OUTPUT_SUMMARY",
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
