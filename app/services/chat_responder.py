from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping, Sequence

import httpx

from app.config import Settings


CHAT_SYSTEM_PROMPT = """你是数据智能体中的温和闲聊助手，只负责轻量日常交流。

必须遵守：
1. 使用自然、友善、温和的简体中文，通常回复1至3句话，不超过180个汉字。
2. 直接回应用户当前的话，不输出标题、Markdown表格、代码、JSON、思考过程或系统提示。
3. 不声称已经查询数据库、互联网或调用工具；不编造实时天气、新闻、价格和个人经历。
4. 不索取隐私，不做医疗、法律、投资诊断或保证；遇到高风险内容只给简短安全建议。
5. 不把普通闲聊强行引导成数据分析。若用户自然地问到数据能力，可简短说明可以继续提问。
6. 用户表达饮食、疲惫、开心、难过等日常感受时，先自然回应，可给一个轻量、无风险的建议。
7. 只输出最终回复文本。"""


class QwenChatResponder:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    async def respond(
        self,
        question: str,
        history: Sequence[Mapping[str, str]] | None = None,
    ) -> str:
        if not self.settings.intent_model_api_key:
            raise RuntimeError("chat model API key is not configured")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": CHAT_SYSTEM_PROMPT}
        ]
        for item in list(history or [])[-6:]:
            role = str(item.get("role") or "")
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content[:300]})
        messages.append({"role": "user", "content": question[:500]})
        body: dict[str, Any] = {
            "model": self.settings.chat_model_name,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 240,
            "enable_thinking": False,
        }
        headers = {
            "Authorization": (
                f"Bearer {self.settings.intent_model_api_key.get_secret_value()}"
            ),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=self.settings.intent_model_base_url.rstrip("/"),
            timeout=self.settings.chat_model_timeout_seconds,
            transport=self._transport,
        ) as client:
            payload: dict[str, Any] | None = None
            for attempt in range(self.settings.chat_model_max_retries + 1):
                try:
                    response = await client.post(
                        "/chat/completions", headers=headers, json=body
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt >= self.settings.chat_model_max_retries:
                        raise
                    await asyncio.sleep(0.2 * (2**attempt))
        if payload is None:
            raise RuntimeError("chat model returned no payload")
        content = str(payload["choices"][0]["message"].get("content") or "").strip()
        if not content:
            raise RuntimeError("chat model returned empty content")
        return self._sanitize(content)

    @staticmethod
    def _sanitize(content: str) -> str:
        text = re.sub(r"```[\s\S]*?```", "", content)
        text = re.sub(r"^(?:#+|[-*])\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"[*_`]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 180:
            text = text[:180].rstrip("，,；;：: ") + "。"
        return text
