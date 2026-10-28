"""只负责模型解释；不依赖智能体 app，不执行工具或修改业务状态。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import httpx

from .diagnostics import Diagnostics, redact
from .prompt import SYSTEM_PROMPT


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str
    model: str


async def explain(state: Diagnostics, config: ModelConfig | None, *,
                  fallback: str, outcome: dict | None = None,
                  recorder: Callable | None = None) -> str:
    """每次异常收尾最多调用一次；解释器故障不递归触发解释器。"""
    if state.explanation is not None:
        return state.explanation
    state.explaining = True
    state.explanation = redact(fallback, None)
    if config is None:
        return state.explanation
    payload = state.payload(outcome)
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            result = await client.post(config.base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer ' + config.api_key},
                json={'model': config.model, 'messages': messages, 'temperature': 0,
                      'response_format': {'type': 'json_object'},
                      'enable_thinking': False})
        result.raise_for_status()
        response = result.json()
        if recorder:
            recorder(response)
        content = json.loads(response['choices'][0]['message']['content'])
        refs = content.get('evidence_ids')
        valid = {e['id'] for e in payload['evidence']}
        explanation = content.get('explanation')
        if (isinstance(explanation, str) and explanation.strip()
                and isinstance(refs, list) and refs and all(isinstance(r, str) and r in valid for r in refs)
                and content.get('certainty') in ('confirmed', 'limited')):
            state.explanation = redact(explanation, None)
    except Exception as exc:
        # A model outage cannot replace the original failure with another error.
        logging.getLogger(__name__).warning('exception explanation unavailable: %s', type(exc).__name__)
    return state.explanation
