"""relation（新问题/追问/澄清）的 jev 终审与 qwen 兜底。

识别模型每轮先给 relation，jev 并行判定；置信度达标且通过路由一致性
校验时采纳 jev，否则保持识别模型判定。校验不过、失败、低置信一律
兜底，主链路绝不因 jev 不可用而改变行为。
"""
from __future__ import annotations

import asyncio
import logging

from app.observability.langfuse_client import text_metadata

from .jev_client import RELATION_CRITERIA, jev_choice

logger = logging.getLogger(__name__)



def build_relation_state(
    request, question_log=None, pending=None, task_count: int = 0
) -> str:
    """把会话上下文压成 jev 的 state：当前问题 + 最近问题链 + 挂起反问。

    relation 判定离不开会话历史，只给单句会导致依赖式复合问题被误判成
    下钻；这里复用会话问题链落盘数据，首轮明示"无历史"帮助纠偏。
    """
    lines = ["【当前用户问题】" + str(getattr(request, "question", "") or "").strip()]
    entries = [
        entry for entry in reversed(list(question_log or []))
        if isinstance(entry, dict)
    ]
    if entries:
        lines.append("【会话最近问题（旧到新）】")
        for index, entry in enumerate(entries[-3:], 1):
            question = str(entry.get("question") or "").strip()[:200]
            line = f"{index}. {question}" if question else f"{index}.（无原文）"
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
    else:
        lines.append("【会话历史】无（本轮为会话首轮，无任何历史任务和反问）")
    if pending is not None:
        lines.append("【挂起状态】存在等待用户回复的系统反问")
    if task_count:
        lines.append(f"【历史任务】{task_count} 个")
    return "\n".join(lines)


def start_relation_decider_task(settings, state_text: str):
    """开关齐备时并行发起关系判定任务，否则返回 None。"""
    if not (settings.jev_enabled and settings.jev_relation_enabled):
        return None
    return asyncio.create_task(
        jev_choice(
            settings,
            state_text=state_text,
            question_name="relation",
            instructions="判定本轮用户问题与会话上下文的关系",
            criteria=RELATION_CRITERIA,
        )
    )


def relation_override_guard(
    jev_choice_value: str, context_trace: dict, conversation_state
) -> str | None:
    """覆写前的路由一致性校验。返回拒绝原因，None 表示允许覆写。

    relation 直接驱动执行分支，翻转到另一种关系时必须保证下游路由
    还能走通：新问题不能背着历史任务绑定，追问族必须有可承接的任务，
    澄清回复必须真的有挂起的反问。
    """
    target = context_trace.get("FINAL_TARGET")
    has_pending = conversation_state.pending is not None
    if jev_choice_value == "ANSWER_CLARIFICATION":
        # 有挂起反问即可承接，此轮尚未绑定任务，不能套用追问族的 target 检查
        return None if has_pending else "no_pending_clarification"
    if jev_choice_value == "NEW_TASK":
        if target is not None:
            return "target_task_bound"
        if has_pending:
            return "pending_clarification_active"
        return None
    if target is None:
        return "no_target_for_followup"
    return None


async def adopt_relation_verdict(
    task,
    proposal,
    conversation_state,
    context_trace: dict,
    message_id: str,
) -> bool:
    """收集 jev 判定并终审 relation。返回是否采纳了 jev。

    采纳时同步改写 context_trace['FINAL_RELATION'] 与 proposal.relation，
    下游分流和展示统一看到终审结果；未采纳时两者保持识别模型原值。
    """
    if task is None:
        return False
    await asyncio.wait({task})
    qwen_relation = str(context_trace.get("FINAL_RELATION") or "")
    if not qwen_relation:
        # 关系未定（status≠ACCEPTED），没有可终审的基准
        return False
    try:
        jev_res = task.result()
    except Exception:
        # 客户端契约是不抛异常，这里兜住任务级意外，qwen 判定兜底
        return False
    if jev_res is None:
        # 失败/低置信：客户端已记日志，识别模型判定即兜底结果
        return False
    if jev_res.choice == qwen_relation:
        logger.info(
            "jev_relation verdict agreed: relation=%s confidence=%.2f "
            "message_id=%s",
            qwen_relation,
            jev_res.confidence,
            message_id,
        )
        return False
    guard_reason = relation_override_guard(
        jev_res.choice, context_trace, conversation_state
    )
    if guard_reason is not None:
        logger.info(
            "jev_relation override rejected: qwen=%s jev=%s reason=%s "
            "confidence=%.2f message_id=%s",
            qwen_relation,
            jev_res.choice,
            guard_reason,
            jev_res.confidence,
            message_id,
        )
        return False
    context_trace["FINAL_RELATION"] = jev_res.choice
    proposal.relation = jev_res.choice
    logger.info(
        "jev_relation override adopted: qwen=%s jev=%s confidence=%.2f "
        "message_id=%s",
        qwen_relation,
        jev_res.choice,
        jev_res.confidence,
        message_id,
    )
    return True


__all__ = [
    "RELATION_DECIDER_WAIT_SECONDS",
    "adopt_relation_verdict",
    "build_relation_state",
    "relation_override_guard",
    "start_relation_decider_task",
    "text_metadata",
]
