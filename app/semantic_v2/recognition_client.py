"""V2 recognition transport using the existing intent-model settings."""
from __future__ import annotations

import asyncio
import json

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing.exceptions import Unresolvable

from app.services.progress import emit_progress


class RecognitionFailure(ValueError):
    """Bounded system reason; never convert transport/parser errors to slot asks."""


def _validate_exact_dynamic_schema(instance, schema) -> None:
    """Fail closed on the exact schema issued for this model invocation."""
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(instance)
    except (SchemaError, Unresolvable):
        raise RecognitionFailure('V2_MODEL_DYNAMIC_SCHEMA_INVALID') from None
    except ValidationError:
        raise RecognitionFailure('V2_MODEL_DYNAMIC_SCHEMA_VIOLATION') from None


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

    async def _stream_choice(self, client, *, headers, body, stage):
        async with client.stream(
            'POST', '/chat/completions', headers=headers,
            json={**body, 'stream': True},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '').casefold()
            if 'text/event-stream' not in content_type:
                payload = json.loads((await response.aread()).decode('utf-8'))
                return self._choice_payload(payload)

            fragments: list[str] = []
            finish_reason = None
            refusal = None
            published_start = False
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
                        await self._emit_stream_started(stage)
                        published_start = True
            return ''.join(fragments), finish_reason, refusal

    async def complete(self, *, stage, instruction, context, output_model, schema=None):
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
                                client, headers=headers, body=body, stage=stage,
                            )
                        else:
                            response = await client.post('/chat/completions', headers=headers, json=body)
                            response.raise_for_status()
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
            _validate_exact_dynamic_schema(raw_output, schema)
            return output_model.model_validate_json(content)
        except RecognitionFailure:
            raise
        except httpx.HTTPError:
            raise RecognitionFailure('V2_MODEL_TRANSPORT_FAILURE') from None
        except (ValueError, TypeError, KeyError, IndexError):
            raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID') from None
