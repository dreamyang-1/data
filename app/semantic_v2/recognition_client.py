"""Opt-in V2 recognition transport using the existing intent-model settings."""
from __future__ import annotations

import asyncio
import json

import httpx


class RecognitionFailure(ValueError):
    """Bounded system reason; never convert transport/parser errors to slot asks."""


class RecognitionModelClient:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def complete(self, *, stage, instruction, context, output_model, schema=None):
        if not self.settings.intent_model_api_key:
            raise RecognitionFailure('V2_MODEL_NOT_CONFIGURED')
        schema = schema or output_model.model_json_schema()
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
                        response = await client.post('/chat/completions', headers=headers, json=body)
                        response.raise_for_status()
                        payload = response.json()
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
            choice = payload['choices'][0]
            if choice.get('finish_reason') not in (None, 'stop') or choice['message'].get('refusal'):
                raise RecognitionFailure('V2_MODEL_OUTPUT_INCOMPLETE')
            content = choice['message'].get('content')
            if not isinstance(content, str) or not content or len(content) > 128_000:
                raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID')
            return output_model.model_validate_json(content)
        except RecognitionFailure:
            raise
        except httpx.HTTPError:
            raise RecognitionFailure('V2_MODEL_TRANSPORT_FAILURE') from None
        except (ValueError, TypeError, KeyError, IndexError):
            raise RecognitionFailure('V2_MODEL_OUTPUT_INVALID') from None
