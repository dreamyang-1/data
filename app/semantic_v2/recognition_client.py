"""V2 recognition transport using the existing intent-model settings."""
from __future__ import annotations

import asyncio
import json
import re

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError, best_match
from referencing.exceptions import Unresolvable

from app.services.progress import emit_progress
from app.observability.call_timing import OperationHandle, track_operation


_DIAGNOSTIC_PATH_LIMIT = 16
_DIAGNOSTIC_SEGMENT_LIMIT = 80


def _bounded_diagnostic_path(path):
    """Structural coordinates only; never instance values."""
    segments = []
    for segment in list(path)[:_DIAGNOSTIC_PATH_LIMIT]:
        if isinstance(segment, int):
            segments.append(segment)
        else:
            segments.append(str(segment)[:_DIAGNOSTIC_SEGMENT_LIMIT])
    return tuple(segments)


class RecognitionFailure(ValueError):
    """Bounded system reason; never convert transport/parser errors to slot asks."""

    def __init__(self, code, *, stage=None, instance_path=(), schema_path=(), validator=None):
        super().__init__(code)
        self.stage = stage
        self.instance_path = tuple(instance_path)
        self.schema_path = tuple(schema_path)
        self.validator = validator

    def public_message(self) -> str:
        """Explain the bounded failure without blaming a complete user query."""
        code = str(self)
        stage = {
            "v2_current_turn": "当前问题要素提取",
            "v2_semantic_edits": "语义字段绑定",
            "v2_source_value_choice": "目录值确认",
        }.get(self.stage, "结构化语义识别")
        suffix = f"（错误码：{code}）"
        if code == "V2_RECOGNITION_CONTEXT_TOO_LARGE":
            return (
                f"语义识别未能完成{stage}：本轮需要同时校验的语义目录候选和字段约束"
                "超过结构化模型的输入上限。当前问题并非缺少信息，无需重复改写。"
                "如需立即查询，可先按单个品牌分别查询；管理员需要压缩该业务域的候选目录，"
                f"或将候选召回拆分后再执行。{suffix}"
            )
        if code == "V2_MODEL_DYNAMIC_SCHEMA_VIOLATION":
            field = (
                ".".join(str(part) for part in self.instance_path)
                if self.instance_path
                else "模型结构化结果（未定位到单一字段）"
            )
            rule = self.validator or "动态 JSON Schema"
            return (
                f"模型在{stage}阶段返回的结构化结果不符合约束；具体字段：{field}；"
                f"未通过规则：{rule}。当前问题没有被判定为缺少业务参数，可直接重试原问题；"
                "若持续出现，管理员需要检查该节点提示词、动态 JSON Schema 与当前模型的"
                f"结构化输出兼容性。{suffix}"
            )
        if code == "V2_MODEL_DYNAMIC_SCHEMA_INVALID":
            return (
                f"系统在{stage}阶段生成的动态 JSON Schema 本身无效。用户无需补充业务内容；"
                "管理员需要检查该业务域候选字段生成的 Schema 引用、必填项和组合约束。"
                f"{suffix}"
            )
        if code == "V2_MODEL_OUTPUT_INCOMPLETE":
            return (
                f"语义模型在{stage}阶段的结构化响应被截断或未正常结束。请直接重试原问题；"
                "若持续出现，管理员需要检查模型 finish_reason、输出长度和超时设置。"
                f"{suffix}"
            )
        if code == "V2_MODEL_OUTPUT_INVALID":
            return (
                f"语义模型在{stage}阶段没有返回可解析且符合约束的 JSON。当前无法据此判断"
                "用户缺少哪个业务参数，因此不会要求补充固定类别；管理员需要检查模型响应格式、"
                f"节点提示词和 Schema。{suffix}"
            )
        if code == "V2_MODEL_TRANSPORT_FAILURE":
            return (
                f"{stage}阶段未能连接语义模型服务。请稍后重试；管理员需要检查模型接口、"
                f"网络和超时配置。{suffix}"
            )
        if code == "V2_MODEL_NOT_CONFIGURED":
            return (
                f"{stage}阶段所需的语义模型尚未配置。用户无需补充问题；管理员需要配置"
                f"模型地址、模型 ID 和访问凭据。{suffix}"
            )
        return (
            f"{stage}阶段失败，系统尚未获得足够证据判断用户缺少哪个业务参数。"
            "请直接重试原问题；管理员应根据错误码检查该阶段配置，不能让用户在未知原因下"
            f"反复补充产品、品牌、医院、经销商或厂家。{suffix}"
        )


def _completed_question_prefix(buffer: str) -> str | None:
    """Best-effort prefix of a still-streaming ``completed_question`` value.

    The public progress event only needs the visible prefix, so an unfinished
    escape sequence simply ends the prefix instead of failing the stream.
    """
    match = re.search(r'"completed_question"\s*:\s*"', buffer)
    if match is None:
        return None
    escapes = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '/': '/',
               'b': '\b', 'f': '\f'}
    value = []
    index = match.end()
    while index < len(buffer):
        char = buffer[index]
        if char == '"':
            break
        if char == '\\' and index + 1 < len(buffer):
            nxt = buffer[index + 1]
            if nxt == 'u':
                digits = buffer[index + 2:index + 6]
                if len(digits) == 4 and all(c in '0123456789abcdefABCDEF' for c in digits):
                    value.append(chr(int(digits, 16)))
                    index += 6
                    continue
                break
            if nxt in escapes:
                value.append(escapes[nxt])
                index += 2
                continue
            break
        value.append(char)
        index += 1
    return ''.join(value) or None


def _validate_exact_dynamic_schema(instance, schema, *, stage=None) -> None:
    """Fail closed on the exact schema issued for this model invocation."""
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(instance)
    except (SchemaError, Unresolvable):
        raise RecognitionFailure('V2_MODEL_DYNAMIC_SCHEMA_INVALID', stage=stage) from None
    except ValidationError:
        detail = best_match(Draft202012Validator(schema).iter_errors(instance))
        raise RecognitionFailure('V2_MODEL_DYNAMIC_SCHEMA_VIOLATION', stage=stage,
            instance_path=_bounded_diagnostic_path(detail.absolute_path),
            schema_path=_bounded_diagnostic_path(detail.absolute_schema_path),
            validator=str(detail.validator)[:40] if detail.validator else None) from None


class RecognitionModelClient:
    def __init__(
        self,
        settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        force_stream: bool | None = None,
    ):
        self.settings = settings
        self.transport = transport
        # Existing deterministic HTTP test doubles return one JSON response.
        # Production has no injected transport and follows the configured
        # streaming path. Individual stream-contract tests can opt in.
        self.stream_enabled = (
            bool(settings.intent_model_stream_enabled)
            if force_stream is None and transport is None
            else bool(force_stream)
        )

    @staticmethod
    def _choice_payload(payload):
        choice = payload['choices'][0]
        message = choice.get('message') or {}
        return (
            message.get('content'),
            choice.get('finish_reason'),
            message.get('refusal'),
        )

    @staticmethod
    async def _emit_stream_started(stage: str) -> None:
        details = {
            'v2_current_turn': (
                '语义识别模型已开始返回结构化结果，正在完成字段校验。',
                'V2_CURRENT_TURN_MODEL_STREAM_STARTED',
            ),
            'v2_semantic_edits': (
                '语义绑定模型已开始返回结构化结果，正在完成任务绑定校验。',
                'V2_SEMANTIC_BINDING_MODEL_STREAM_STARTED',
            ),
        }.get(stage)
        if details is None:
            return
        message, phase = details
        await emit_progress(
            'INTENT_RECOGNITION',
            'RUNNING',
            message,
            progress_phase=phase,
        )

    @staticmethod
    async def _emit_completed_question_prefix(stage: str, buffer: str) -> None:
        """Do not publish an unfinished JSON string as user-visible wording.

        The model may have streamed only a few characters (for example
        ``帮我``) when this hook runs.  The finalized, schema-validated value is
        rendered by the normal intent summary, so exposing the prefix creates
        a duplicate and sometimes contradictory completed-question row.
        """
        _ = stage, buffer

    async def _stream_choice(self, client, *, headers, body, stage, timing):
        async with client.stream(
            'POST', '/chat/completions', headers=headers,
            json={**body, 'stream': True},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').casefold()
            if 'text/event-stream' not in content_type:
                payload = json.loads((await response.aread()).decode('utf-8'))
                timing.mark_first_result()
                return self._choice_payload(payload)

            fragments: list[str] = []
            finish_reason = None
            refusal = None
            published_start = False
            published_prefix = False
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if not data or data == '[DONE]':
                    continue
                payload = json.loads(data)
                choices = payload.get('choices') or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get('finish_reason') or finish_reason
                delta = choice.get('delta') or choice.get('message') or {}
                refusal = delta.get('refusal') or refusal
                fragment = delta.get('content')
                if fragment is None:
                    continue
                if not isinstance(fragment, str):
                    raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID')
                if fragment:
                    fragments.append(fragment)
                    if not published_start:
                        timing.mark_first_result()
                        await self._emit_stream_started(stage)
                        published_start = True
                    if not published_prefix:
                        buffer = ''.join(fragments)
                        if _completed_question_prefix(buffer) is not None:
                            await self._emit_completed_question_prefix(stage, buffer)
                            published_prefix = True
            return ''.join(fragments), finish_reason, refusal

    async def complete(self, *, stage, instruction, context, output_model, schema=None):
        operation_suffix = {
            'v2_current_turn': 'current_turn',
            'v2_semantic_edits': 'semantic_edits',
        }.get(stage, str(stage).removeprefix('v2_'))
        with track_operation(
            "V2_CONTEXT",
            f"v2.model.{operation_suffix}",
            attributes={
                "model": self.settings.intent_model_name,
                "stream": self.stream_enabled,
            },
        ) as timing:
            return await self._complete(
                stage=stage,
                instruction=instruction,
                context=context,
                output_model=output_model,
                schema=schema,
                timing=timing,
            )

    async def _complete(
        self,
        *,
        stage,
        instruction,
        context,
        output_model,
        schema=None,
        timing: OperationHandle,
    ):
        if not self.settings.intent_model_api_key:
            raise RecognitionFailure('V2_MODEL_NOT_CONFIGURED')
        schema = output_model.model_json_schema() if schema is None else schema
        response_format = ({'type': 'json_schema', 'json_schema': {
            'name': stage, 'strict': True, 'schema': schema}}
            if self.settings.intent_model_response_format == 'json_schema'
            else {'type': 'json_object'})
        messages = [
            {'role': 'system', 'content': instruction + '\nJSON Schema:\n' + json.dumps(schema, ensure_ascii=False)},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, allow_nan=False)},
        ]
        # Never truncate away candidates, schema, evidence or scope checks.
        if len(json.dumps(messages, ensure_ascii=False)) > 250_000:
            raise RecognitionFailure('V2_RECOGNITION_CONTEXT_TOO_LARGE')
        body = dict(model=self.settings.intent_model_name, messages=messages, temperature=0,
            enable_thinking=self.settings.intent_model_enable_thinking, response_format=response_format)
        headers = {'Authorization': 'Bearer ' + self.settings.intent_model_api_key.get_secret_value()}
        try:
            async with httpx.AsyncClient(base_url=self.settings.intent_model_base_url.rstrip('/'),
                    timeout=self.settings.intent_model_timeout_seconds, transport=self.transport) as client:
                for attempt in range(self.settings.intent_model_max_retries + 1):
                    try:
                        if self.stream_enabled:
                            content, finish_reason, refusal = await self._stream_choice(
                                client,
                                headers=headers,
                                body=body,
                                stage=stage,
                                timing=timing,
                            )
                        else:
                            response = await client.post('/chat/completions', headers=headers, json=body)
                            response.raise_for_status()
                            timing.mark_first_result()
                            content, finish_reason, refusal = self._choice_payload(
                                response.json()
                            )
                        break
                    except (httpx.TimeoutException, httpx.NetworkError):
                        if attempt >= self.settings.intent_model_max_retries:
                            raise
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 429 and exc.response.status_code < 500:
                            raise
                        if attempt >= self.settings.intent_model_max_retries:
                            raise
                    await asyncio.sleep(0.2 * (2 ** attempt))
            if finish_reason not in (None, 'stop') or refusal:
                raise RecognitionFailure('V2_MODEL_OUTPUT_INCOMPLETE')
            if not isinstance(content, str) or not content or len(content) > 128_000:
                raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID')
            try:
                raw_output = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID') from None
            _validate_exact_dynamic_schema(raw_output, schema, stage=stage)
            return output_model.model_validate_json(content)
        except RecognitionFailure as exc:
            if exc.stage is None:
                exc.stage = stage
            # Structural diagnostics only; never instance values or prompts.
            if exc.instance_path or exc.schema_path or exc.validator:
                timing.set_attribute('v2_schema_instance_path', json.dumps(list(exc.instance_path)))
                timing.set_attribute('v2_schema_schema_path', json.dumps(list(exc.schema_path)))
                timing.set_attribute('v2_schema_validator', exc.validator or '')
            raise
        except httpx.HTTPError:
            raise RecognitionFailure('V2_MODEL_TRANSPORT_FAILURE') from None
        except (ValueError, TypeError, KeyError, IndexError):
            raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID') from None
