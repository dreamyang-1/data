import json
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, Field

from app.config import Settings
from app.semantic_v2.recognition_client import RecognitionFailure, RecognitionModelClient


class DynamicOutput(BaseModel):
    allowed: list[str] = Field(default_factory=list)
    field_binding_handles: list[str] = Field(default_factory=list)
    mention_id: str = 'm1'
    value: Literal['A'] = 'A'


def schema(*, empty_allowed=False, empty_handles=False, mention_ids=('m1',)):
    result = DynamicOutput.model_json_schema()
    if empty_allowed:
        result['properties']['allowed']['maxItems'] = 0
    if empty_handles:
        result['properties']['field_binding_handles']['maxItems'] = 0
    result['properties']['mention_id']['enum'] = list(mention_ids)
    return result


def client_for(content):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': content,
        }}]})

    settings = Settings(
        _env_file=None,
        intent_model_api_key='test-only-key',
        intent_model_base_url='https://model.invalid/v1',
        intent_model_max_retries=0,
    )
    return RecognitionModelClient(settings, httpx.MockTransport(handler)), calls


async def complete(content, issued_schema):
    client, calls = client_for(content)
    result = await client.complete(
        stage='semantic_edits',
        instruction='fixture',
        context={},
        output_model=DynamicOutput,
        schema=issued_schema,
    )
    return result, calls


@pytest.mark.asyncio
async def test_exact_schema_and_pydantic_both_pass():
    content = json.dumps({
        'allowed': ['kept'],
        'field_binding_handles': ['field-1'],
        'mention_id': 'm1',
        'value': 'A',
    })
    result, calls = await complete(content, schema())
    assert result.allowed == ['kept']
    assert result.field_binding_handles == ['field-1']
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_issued_schema_is_used_exactly_instead_of_replaced():
    content = json.dumps({
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    })
    result, calls = await complete(content, {})
    issued = json.loads(calls[0]['messages'][0]['content'].split('JSON Schema:\n', 1)[1])
    assert issued == {}
    assert result.value == 'A'


@pytest.mark.asyncio
async def test_zero_item_arrays_pass_when_empty():
    content = json.dumps({
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    })
    result, _ = await complete(content, schema(empty_allowed=True, empty_handles=True))
    assert result.allowed == []
    assert result.field_binding_handles == []


@pytest.mark.asyncio
async def test_dynamic_enum_passes_for_offered_mention():
    content = json.dumps({
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'offered-m2',
        'value': 'A',
    })
    result, _ = await complete(content, schema(mention_ids=('offered-m1', 'offered-m2')))
    assert result.mention_id == 'offered-m2'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['allowed', 'field_binding_handles'])
async def test_max_items_zero_violation_is_rejected_before_pydantic(field):
    payload = {
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    }
    payload[field] = ['forbidden']
    issued = schema(
        empty_allowed=field == 'allowed',
        empty_handles=field == 'field_binding_handles',
    )
    client, calls = client_for(json.dumps(payload))
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_VIOLATION$'):
        await client.complete(
            stage='semantic_edits', instruction='fixture', context={},
            output_model=DynamicOutput, schema=issued,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unoffered_mention_enum_is_rejected():
    payload = {
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'not-offered',
        'value': 'A',
    }
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_VIOLATION$'):
        await complete(json.dumps(payload), schema(mention_ids=('offered',)))


@pytest.mark.asyncio
async def test_dynamic_anyof_violation_is_rejected():
    issued = schema()
    issued['anyOf'] = [
        {'properties': {'allowed': {'minItems': 1}}},
        {'properties': {'field_binding_handles': {'minItems': 1}}},
    ]
    payload = {
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    }
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_VIOLATION$'):
        await complete(json.dumps(payload), issued)


@pytest.mark.asyncio
async def test_invalid_json_keeps_existing_output_error():
    client, calls = client_for('{not-json')
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_OUTPUT_INVALID$'):
        await client.complete(
            stage='semantic_edits', instruction='fixture', context={},
            output_model=DynamicOutput, schema=schema(),
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_dynamic_schema_pass_then_existing_pydantic_validation_still_runs():
    issued = {
        'type': 'object',
        'properties': {
            'allowed': {'type': 'string'},
            'field_binding_handles': {'type': 'array'},
            'mention_id': {'type': 'string'},
            'value': {'type': 'string'},
        },
        'required': ['allowed', 'field_binding_handles', 'mention_id', 'value'],
        'additionalProperties': False,
    }
    payload = {
        'allowed': 'dynamic-schema-allows-this',
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    }
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_OUTPUT_INVALID$'):
        await complete(json.dumps(payload), issued)


@pytest.mark.asyncio
async def test_invalid_issued_schema_fails_closed_without_model_retry():
    payload = {
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    }
    invalid_schema = {'type': 'not-a-json-schema-type'}
    client, calls = client_for(json.dumps(payload))
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_INVALID$'):
        await client.complete(
            stage='semantic_edits', instruction='fixture', context={},
            output_model=DynamicOutput, schema=invalid_schema,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unresolvable_issued_schema_reference_fails_closed_without_retry():
    payload = {
        'allowed': [],
        'field_binding_handles': [],
        'mention_id': 'm1',
        'value': 'A',
    }
    client, calls = client_for(json.dumps(payload))
    with pytest.raises(RecognitionFailure, match='^V2_MODEL_DYNAMIC_SCHEMA_INVALID$'):
        await client.complete(
            stage='semantic_edits', instruction='fixture', context={},
            output_model=DynamicOutput, schema={'$ref': '#/$defs/missing'},
        )
    assert len(calls) == 1
