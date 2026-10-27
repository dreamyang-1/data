"""jev 决策模型客户端：SystemOne Choice 判定的统一入口。

接口为 TypeSafe 官方 SystemOne API，输入 state 文本与选项 criteria，
输出类型化选项与校准概率。机器出外网依赖本机反向隧道（默认
127.0.0.1:17890），可用性随隧道波动，因此所有失败都在客户端内部
吞掉并返回 None，由调用方保持原判定逻辑，主链路绝不因 jev 不可用
而中断。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, SecretStr

from app.observability.langfuse_client import generation_under, text_metadata

logger = logging.getLogger(__name__)

# api key 兜底文件：设置项为空时读取，进程内只读一次
_KEY_FILE = Path.home() / ".jev_api_key"
_key_file_cache: str | None = None

# 状态识别选项：key 与 V2 ContextProposal.relation 枚举一致，仅影子比对用
RELATION_CRITERIA: dict[str, str] = {
    "NEW_TASK": "与历史轮次无关的全新业务问题",
    "CONTINUE": "在上一轮结果上继续补充展示或分析",
    "MODIFY": "修改上一轮的筛选条件或时间等参数后重新查",
    "ADD": "在上一轮基础上追加条件或内容",
    "REPLACE": "用新值替换上一轮的某个条件",
    "REMOVE": "去掉上一轮的某个条件",
    "CLEAR": "清掉上一轮的全部或某类条件",
    "CORRECT": "修正上一轮理解错的内容",
    "DRILL_DOWN": "对上一轮结果中的对象看更细的数据",
    "RETURN_TO_TOPIC": "回到之前放下的任务话题",
    "ANSWER_CLARIFICATION": "回答系统上一轮的反问",
}


class JevChoiceResult(BaseModel):
    choice: str
    confidence: float
    probabilities: dict[str, float]


def read_jev_api_key(settings) -> str:
    """取 api key：设置项优先，为空时回退 ~/.jev_api_key（进程内缓存）。"""
    global _key_file_cache
    secret = getattr(settings, "jev_api_key", None)
    if secret is not None:
        value = (
            secret.get_secret_value()
            if isinstance(secret, SecretStr)
            else str(secret)
        ).strip()
        if value:
            return value
    if _key_file_cache is None:
        try:
            _key_file_cache = _KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            _key_file_cache = ""
    return _key_file_cache


async def jev_choice(
    settings,
    *,
    state_text: str,
    question_name: str,
    instructions: str,
    criteria: dict[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
    parent=None,
) -> JevChoiceResult | None:
    """发一次 Choice 判定。失败、越界、低置信一律返回 None，不抛异常。

    parent 给定 Langfuse span 时，jev-decision 节点挂到它下面（跨阶段
    归拢用）；否则按当前上下文嵌套。
    """
    api_key = read_jev_api_key(settings)
    if not api_key:
        logger.warning("jev choice skipped: no api key question=%s", question_name)
        return None
    body = {
        "model": settings.jev_model,
        "state": state_text,
        "questions": {
            question_name: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    started = time.perf_counter()
    # Langfuse 追踪：model 记为 jev 模型名，输入按隐私开关脱敏
    with generation_under(
        parent,
        name="jev-decision",
        model=getattr(settings, "jev_model", None) or None,
        input={
            "state": text_metadata(state_text),
            "question": question_name,
            "instructions": text_metadata(instructions),
            # 选项描述全文留痕：判定规则都写在描述里，只记选项名会看不出
            # jev 实际拿到的判定依据
            "criteria": {key: text_metadata(desc) for key, desc in criteria.items()},
        },
    ) as observation:
        try:
            async with httpx.AsyncClient(
                timeout=None,
                proxy=settings.jev_proxy or None,
                transport=transport,
            ) as client:
                response = await client.post(
                    settings.jev_base_url, headers=headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(
                "jev choice failed: question=%s elapsed=%.0fms error=%s",
                question_name,
                (time.perf_counter() - started) * 1000,
                exc,
            )
            try:
                observation.update(
                    level="ERROR", output={"error": str(exc)[:200]}
                )
            except Exception:
                logger.debug("Langfuse generation update failed", exc_info=True)
            return None
        answer = (payload.get("answers") or {}).get(question_name) or {}
        choice = str(answer.get("choice") or "")
        elapsed_ms = (time.perf_counter() - started) * 1000
        if choice not in criteria:
            logger.warning(
                "jev choice out of range: question=%s choice=%s elapsed=%.0fms",
                question_name,
                choice,
                elapsed_ms,
            )
            try:
                observation.update(
                    level="ERROR",
                    output={"error": f"choice out of range: {choice[:80]}"},
                )
            except Exception:
                logger.debug("Langfuse generation update failed", exc_info=True)
            return None
        confidence = float(answer.get("confidence") or 0.0)
        usage = payload.get("usage") or {}
        token_usage = {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        }
        if confidence < settings.jev_min_confidence:
            # 低置信不采用，但保留日志供影子阶段统计判定质量
            logger.info(
                "jev choice low confidence: question=%s choice=%s confidence=%.2f "
                "elapsed=%.0fms",
                question_name,
                choice,
                confidence,
                elapsed_ms,
            )
            try:
                observation.update(
                    output={
                        "choice": choice,
                        "confidence": confidence,
                        "elapsed_ms": round(elapsed_ms),
                        "input_tokens": token_usage["input"],
                        "output_tokens": token_usage["output"],
                    },
                    usage_details=token_usage,
                    metadata={"rejected": "low_confidence"},
                )
            except Exception:
                logger.debug("Langfuse generation update failed", exc_info=True)
            return None
        probabilities = {
            str(key): float(value)
            for key, value in (answer.get("probabilities") or {}).items()
        }
        logger.info(
            "jev choice ok: question=%s choice=%s confidence=%.2f elapsed=%.0fms",
            question_name,
            choice,
            confidence,
            elapsed_ms,
        )
        try:
            observation.update(
                output={
                    "choice": choice,
                    "confidence": confidence,
                    "elapsed_ms": round(elapsed_ms),
                    "input_tokens": token_usage["input"],
                    "output_tokens": token_usage["output"],
                },
                usage_details=token_usage,
            )
        except Exception:
            logger.debug("Langfuse generation update failed", exc_info=True)
        return JevChoiceResult(
            choice=choice, confidence=confidence, probabilities=probabilities
        )
