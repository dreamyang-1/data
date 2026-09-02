from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.domain.models import AgentResponse, ChatRequest, StrictModel, TrustedIdentity
from app.stores import MessageIdReuseConflictError
from app.services.file_ingestion import FileImportError
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from minio_followup_store import DatasetScope


router = APIRouter(tags=["data-analysis"])
logger = logging.getLogger(__name__)


class SpreadsheetImportRequest(StrictModel):
    application_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=128)
    object_name: str = Field(min_length=1, max_length=1024)


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
    identity = trusted_identity(x_roles)
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
            ),
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


def trusted_identity(roles: str | None) -> TrustedIdentity:
    """Use an internal anonymous scope; caller identity headers are not required."""
    return TrustedIdentity(
        tenant_id="default-tenant",
        user_id="default-user",
        roles=[role.strip() for role in (roles or "").split(",") if role.strip()],
    )


async def invoke(request: Request, chat: ChatRequest, identity: TrustedIdentity) -> AgentResponse:
    try:
        result = await asyncio.wait_for(
            request.app.state.container.workflow.ainvoke({"chat": chat, "identity": identity}),
            timeout=request.app.state.container.settings.request_timeout_seconds,
        )
        return result["response"]
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="analysis request deadline exceeded") from exc
    except MessageIdReuseConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


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
            ),
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


def _prepare_regeneration(payload: ChatRequest) -> tuple[ChatRequest, str, str]:
    """Create an isolated execution while preserving external response IDs."""
    external_conversation_id = payload.conversation_id
    external_message_id = payload.message_id
    execution = payload.model_copy(deep=True)
    refresh_id = f"refresh-{uuid4().hex}"
    execution.conversation_id = refresh_id
    execution.message_id = refresh_id
    execution.regenerate = False

    # Some refresh callers include the answer being replaced in history. Cut
    # the refreshed turn and everything after it so the old answer cannot be
    # treated as evidence or a follow-up result during regeneration.
    normalized_question = re.sub(r"\s+", "", execution.question)
    for index in range(len(execution.history) - 1, -1, -1):
        item = execution.history[index]
        normalized_content = re.sub(r"\s+", "", item.content)
        if item.role == "user" and (
            normalized_content == normalized_question
            or normalized_content.endswith(normalized_question)
        ):
            execution.history = execution.history[:index]
            break
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
    identity = trusted_identity(x_roles)
    external_conversation_id = payload.conversation_id
    is_regeneration = payload.regenerate
    if not is_regeneration:
        await _collect_business_question(request, payload)
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
    summary="完整重新生成数据分析回答",
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
    identity = trusted_identity(x_roles)
    external_conversation_id = payload.conversation_id
    external_message_id = payload.message_id
    is_regeneration = payload.regenerate
    if not is_regeneration:
        await _collect_business_question(request, payload)
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
        titled_think_sections: set[str] = set()

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
                heading=_thinking_title(section) if include_heading else None,
            )

        def ordered_progress(event: dict[str, Any]) -> list[dict[str, Any]]:
            nonlocal intent_completed, file_inspection_completed
            if event.get("stage") == "TASK_PLANNING" and not file_inspection_completed:
                deferred_planning.append(event)
                return []
            if (
                event.get("stage") == "INTENT_RECOGNITION"
                and event.get("status") == "COMPLETED"
            ):
                intent_completed = True
            if (
                event.get("stage") == "FILE_INSPECTION"
                and event.get("status") == "COMPLETED"
            ):
                file_inspection_completed = True
                ordered = [event] if bool(event.get("file_based")) else []
                ordered.extend(deferred_planning)
                deferred_planning.clear()
                return ordered
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
            for event in deferred_planning:
                yield render_thinking(event)
            deferred_planning.clear()
            query_evidence = [
                item for item in response.evidence if item.kind == "QUERY_RESULT"
            ]
            analysis_evidence = [
                item for item in response.evidence if item.kind == "ANALYSIS_RESULT"
            ]
            yield render_thinking({
                "stage": "OUTPUT_SUMMARY",
                "status": "COMPLETED",
                "message": (
                    "### 7、输出总结\n"
                    f"任务状态：{response.status}；输出意图：{response.intent.value}。\n"
                    f"数据查询结果：{'已生成并保留证据' if query_evidence else '本轮无数据查询结果'}；"
                    f"数据分析结果：{'已生成' if analysis_evidence else '本轮未生成独立分析结论'}。\n"
                    f"附件：{len(response.files)} 个；图表：{len(response.chart_specs)} 个；"
                    f"证据：{len(response.evidence)} 项。"
                ),
                "file_count": len(response.files),
                "chart_count": len(response.chart_specs),
                "evidence_count": len(response.evidence),
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
    summary="SSE 完整重新生成数据分析回答",
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
    content = str(progress.get("message") or stage).strip()
    # Remove node-owned headings, then emit one normalized public heading for
    # each of the seven data-agent stages at the SSE boundary.
    content = re.sub(r"^\s*#{1,6}\s+[^\r\n]+(?:\r?\n)?", "", content).strip()
    if heading:
        content = f"{heading}\n{content}" if content else heading
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


def _thinking_section(stage: str) -> str | None:
    return {
        "INTENT_RECOGNITION": "intent",
        "FILE_INSPECTION": "file",
        "TASK_PLANNING": "planning",
        "DATA_RETRIEVAL": "execution",
        "RELIABILITY_CHECK": "validation",
        "INSIGHT_ANALYSIS": "insight",
        "OUTPUT_SUMMARY": "summary",
    }.get(stage)


def _thinking_title(section: str) -> str:
    return {
        "intent": "#### ◉ 意图识别",
        "file": "#### ◉ 文件感知与解析",
        "planning": "#### ◉ 任务拆分与规划",
        "execution": "#### ◉ 调度执行",
        "validation": "#### ◉ 结果校验",
        "insight": "#### ◉ 数据洞察分析",
        "summary": "#### ◉ 输出总结",
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
    }:
        return "response_result"
    # Validation, context restoration, question completion, intent extraction,
    # memory retrieval and clarification checks all belong to New_Agent step1.
    return "step1"


def _answer_chunks(
    answer: str, *, max_chars: int = 12, max_chunks: int = 300
) -> list[str]:
    """Split only the already validated final answer into transport chunks.

    The Qwen synthesis result is structured and evidence-validated before it
    reaches this function.  Streaming unvalidated model tokens would bypass
    those safety gates, so chunks are produced from the approved answer.
    """

    if not answer:
        return []
    # A normal answer is emitted in short, readable pieces. Very large tables
    # use a larger adaptive piece size so the SSE event count stays bounded.
    max_chars = max(max_chars, (len(answer) + max_chunks - 1) // max_chunks)
    chunks: list[str] = []
    buffer: list[str] = []
    punctuation = {"。", "！", "？", "；", "\n"}
    for character in answer:
        buffer.append(character)
        if len(buffer) >= max_chars or (
            character in punctuation and len(buffer) >= 8
        ):
            chunks.append("".join(buffer))
            buffer = []
    if buffer:
        chunks.append("".join(buffer))
    return chunks


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
