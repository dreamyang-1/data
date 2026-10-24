"""意图分类的 jev 边车判定：并行起任务、收集结果、按置信度覆写。

编排器只与本模块交互，jev 的起停、等待上限、日志与覆写细节都收在这里。
判定失败或低置信一律保持原结果，主链路不受隧道波动影响。
"""
from __future__ import annotations

import asyncio
import logging

from .jev_client import (
    INTENT_CRITERIA,
    JevChoiceResult,
    apply_jev_intent_override,
    jev_choice,
)

logger = logging.getLogger(__name__)

# 边车等待上限：分类模型本身秒级耗时，jev 正常三百毫秒量级早已完成，
# 超时视为失败并取消任务
INTENT_SIDECAR_WAIT_SECONDS = 3.0


def start_intent_sidecar(settings, question: str, parent=None):
    """开关齐备时并行发起意图判定任务；规划已定意图的场景不参与。

    settings 为 None（测试用 object.__new__ 绕过构造的裸实例）视为关闭。
    parent 给定 Langfuse span 时 jev-decision 节点挂到它下面。
    """
    if settings is None or not (settings.jev_enabled and settings.jev_intent_enabled):
        return None
    return asyncio.create_task(
        jev_choice(
            settings,
            state_text=question,
            question_name="primary_intent",
            instructions="判定用户问题的数据分析意图",
            criteria=INTENT_CRITERIA,
            parent=parent,
        )
    )


async def finish_intent_sidecar(
    task, result, settings, conversation_id: str
) -> JevChoiceResult | None:
    """收集判定并按需覆写意图，返回 jev 原始判定（未触发或失败为 None）。

    覆写面向全部意图来源（含 TASK_PLANNER 回填）：jev 置信度达标即终审，
    不达标或失败时保持原值，原链路即天然兜底。
    """
    if task is None:
        return None
    done, _ = await asyncio.wait({task}, timeout=INTENT_SIDECAR_WAIT_SECONDS)
    if done:
        try:
            jev_res = task.result()
        except Exception:
            # 客户端契约是不抛异常，这里兜住任务级意外，保持原判定
            jev_res = None
    else:
        task.cancel()
        jev_res = None
    if jev_res is None:
        return None
    if str(jev_res.choice) != str(result.primary_intent):
        logger.info(
            "node=intent_recognition jev_disagreement qwen=%s jev=%s "
            "confidence=%.2f conversation_id=%s",
            result.primary_intent,
            jev_res.choice,
            jev_res.confidence,
            conversation_id,
        )
    previous_source = result.intent_source
    previous_intent = result.primary_intent
    if apply_jev_intent_override(result, jev_res, settings.jev_min_confidence):
        logger.info(
            "node=intent_recognition jev_override intent=%s previous=%s/%s "
            "confidence=%.2f conversation_id=%s",
            result.primary_intent,
            previous_intent,
            previous_source,
            jev_res.confidence,
            conversation_id,
        )
    return jev_res
