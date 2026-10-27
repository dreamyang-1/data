"""前置门控：问题进入理解与补全节点之前的六分类判定。

判定模型默认 jev（结构化选项判定），qwen 全提示词在两种情况下接管：
jev 调用失败或置信度不达标、jev 判为危险操作需要复核。任何判定路径
失败都返回 None，表示门控不参与本轮，调用方按原链路继续执行。
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx
from pydantic import BaseModel, SecretStr

from app.decision.jev_client import jev_choice
from app.domain.semantic_description import load_semantic_description
from app.observability.langfuse_client import observe_span, text_metadata

from .prompt import (
    GATE_CLASS_NAMES,
    GATE_CRITERIA,
    GATE_INSTRUCTIONS,
    GATE_PASS_CLASSES,
    GATE_PROMPT_TEMPLATE,
    GATE_TYPE_NAMES,
)

logger = logging.getLogger(__name__)


class GateDecision(BaseModel):
    """门控判定结果。

    gate_class 取六分类之一；judge 记录实际产出结果的模型（jev 或 qwen）。
    action 由分类推导，短路逻辑只按 action 分支：
    pass 放行、chat 闲聊回复、off_topic 非业务回复、danger 危险拦截、
    clarify 待定澄清。
    """

    gate_class: str
    confidence: float
    need_clarification: bool = False
    reason: str = ""
    judge: str

    @property
    def action(self) -> str:
        if self.gate_class in GATE_PASS_CLASSES:
            return "pass"
        return {
            "闲聊": "chat",
            "非业务场景问题": "off_topic",
            "数据危险操作": "danger",
            "待定": "clarify",
        }.get(self.gate_class, "pass")


# 语义规范的进程内缓存：model_id → (过期时间, 内容)。
# 门控每轮都要用规范，不能每轮都查库加下载文件
_spec_cache: dict[int, tuple[float, str]] = {}


async def _load_spec(settings, semantic_model_id) -> str:
    """按 model_id 取语义规范内容，带 TTL 缓存；拉取失败返回空串。"""
    key = semantic_model_id if isinstance(semantic_model_id, int) and semantic_model_id > 0 else 0
    now = time.monotonic()
    hit = _spec_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    reference = await load_semantic_description(
        settings, key if key else None
    )
    content = str(reference.get("content") or "") if reference.get("available") else ""
    # 失败也缓存一小段时间，平台文件服务不可用时避免每轮都等一遍超时
    ttl = settings.gate_spec_cache_seconds if content else 60
    _spec_cache[key] = (now + ttl, content)
    return content


def build_gate_context(question_log, max_turns: int = 4, max_chars: int = 200) -> str:
    """把会话问题链渲染成门控的上下文文本。

    与 relation 判定同源：最近几轮用户问题，带回答摘要和系统反问。
    追问分类依赖上一轮信息，上下文为空时追问特征判不出来。
    """
    entries = [
        entry for entry in reversed(list(question_log or []))
        if isinstance(entry, dict)
    ]
    lines = []
    for index, entry in enumerate(entries[-max_turns:], 1):
        question = str(entry.get("question") or "").strip()[:max_chars]
        if not question:
            continue
        line = f"{index}. {question}"
        summary = str(entry.get("answer_summary") or "").strip()
        if summary:
            line += f"｜上轮回答摘要：{summary[:120]}"
        asked = [
            str(item).strip()
            for item in (entry.get("clarification_questions") or [])
            if str(item).strip()
        ]
        if asked:
            line += f"｜系统曾反问：{'；'.join(asked)[:150]}"
        lines.append(line)
    return "\n".join(lines)


def _parse_qwen_output(text: str) -> dict | None:
    """解析 qwen 输出的 JSON，容忍 markdown 围栏和前后多余文本。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _decision_from_qwen(parsed: dict) -> GateDecision | None:
    """把 qwen 输出的 JSON 字段映射成 GateDecision，分类名非法返回 None。

    分类名优先取 intent_name，取不到时按 intent_type 数字映射；
    提示词里的覆盖度字段（schema_coverage/unsupported_fields）不参与
    门控路由，解析时忽略。
    """
    name = str(parsed.get("intent_name") or "").strip()
    if name not in GATE_CLASS_NAMES:
        try:
            name = GATE_TYPE_NAMES.get(int(parsed.get("intent_type")), "")
        except (TypeError, ValueError):
            name = ""
    if name not in GATE_CLASS_NAMES:
        return None
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return GateDecision(
        gate_class=name,
        confidence=confidence,
        need_clarification=bool(parsed.get("need_clarification")),
        reason=str(parsed.get("reason") or "").strip(),
        judge="qwen",
    )


async def _qwen_gate(settings, *, question: str, context: str, spec: str,
                     transport: httpx.AsyncBaseTransport | None = None) -> GateDecision | None:
    """qwen 全提示词判定：模板 + 按 model_id 注入的语义规范。失败返回 None。"""
    secret = getattr(settings, "intent_model_api_key", None)
    api_key = (
        secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret or "")
    ).strip()
    base_url = str(getattr(settings, "intent_model_base_url", "") or "").rstrip("/")
    if not api_key or not base_url:
        logger.warning("gate qwen skipped: intent model not configured")
        return None
    system = GATE_PROMPT_TEMPLATE + ("\n\n" + spec if spec else "")
    user = f"用户问题：{question.strip()}\n上下文：{context if context else '无'}"
    model = getattr(settings, "intent_model_name", None) or "qwen3.7-max"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "enable_thinking": False,
    }
    started = time.perf_counter()
    with observe_span(
        as_type="generation",
        name="gate-decision",
        model=model,
        input={"system": text_metadata(system), "user": text_metadata(user)},
    ) as observation:
        try:
            async with httpx.AsyncClient(
                timeout=getattr(settings, "intent_model_timeout_seconds", 15),
                transport=transport,
            ) as client:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": "Bearer " + api_key},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            parsed = _parse_qwen_output(str(content))
            decision = _decision_from_qwen(parsed) if parsed else None
        except Exception as exc:
            logger.warning(
                "gate qwen failed: elapsed=%.0fms error=%s",
                (time.perf_counter() - started) * 1000,
                exc,
            )
            try:
                observation.update(level="ERROR", output={"error": str(exc)[:200]})
            except Exception:
                logger.debug("Langfuse generation update failed", exc_info=True)
            return None
        if decision is None:
            logger.warning("gate qwen output unparsable: %s", str(content)[:200])
            try:
                observation.update(
                    level="ERROR", output={"error": "unparsable", "raw": str(content)[:200]}
                )
            except Exception:
                logger.debug("Langfuse generation update failed", exc_info=True)
            return None
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "gate qwen ok: class=%s confidence=%.2f elapsed=%.0fms",
            decision.gate_class,
            decision.confidence,
            elapsed_ms,
        )
        try:
            observation.update(output=decision.model_dump())
        except Exception:
            logger.debug("Langfuse generation update failed", exc_info=True)
        return decision


async def _qwen_gate_safe(settings, *, question: str, context: str, spec: str) -> GateDecision | None:
    """_qwen_gate 的异常安全包装，任何意外都返回 None。"""
    try:
        return await _qwen_gate(settings, question=question, context=context, spec=spec)
    except Exception:
        logger.warning("gate qwen unexpected error", exc_info=True)
        return None


async def run_gate(
    settings,
    *,
    question: str,
    question_log=None,
    semantic_model_id=None,
) -> GateDecision | None:
    """门控入口。返回 None 表示门控未启用或不参与本轮判定，调用方按原链路继续。"""
    if settings is None or not getattr(settings, "gate_enabled", False):
        return None
    question = (question or "").strip()
    if not question:
        return None
    try:
        spec = await _load_spec(settings, semantic_model_id)
    except Exception:
        logger.warning("gate spec load failed, continue without spec", exc_info=True)
        spec = ""
    context = build_gate_context(question_log)

    if getattr(settings, "gate_judge", "jev") == "qwen":
        return await _qwen_gate_safe(
            settings, question=question, context=context, spec=spec
        )

    state = f"用户问题：{question}\n对话上下文：{context if context else '无'}"
    if spec:
        state += "\n" + spec
    try:
        gate_answer = await jev_choice(
            settings,
            state_text=state,
            question_name="gate",
            instructions=GATE_INSTRUCTIONS,
            criteria=GATE_CRITERIA,
        )
    except Exception:
        logger.warning("gate jev unexpected error", exc_info=True)
        gate_answer = None
    if gate_answer is None:
        # 失败或低置信：qwen 全提示词兜底
        return await _qwen_gate_safe(
            settings, question=question, context=context, spec=spec
        )
    if gate_answer.choice == "数据危险操作":
        # 危险误判会直接拦截用户的正常问题，jev 判危险必须经 qwen 复核；
        # 复核失败返回 None，本轮不拦截
        return await _qwen_gate_safe(
            settings, question=question, context=context, spec=spec
        )
    return GateDecision(
        gate_class=gate_answer.choice,
        confidence=gate_answer.confidence,
        need_clarification=gate_answer.choice == "待定",
        judge="jev",
    )
