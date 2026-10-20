"""澄清上下文构建与参数补全判定。

把会话问题链、上一轮查询结果概要与当前问题组装为判定上下文，由一次
模型调用输出三种结果之一：新问题、参数可补全（附完整问题）、需要澄清
（附反问清单）。模型调用通过注入的 complete 回调接入，模块内不做
网络传输；挂起状态的读写转发由 pending_bridge 承担。
"""
from .context_builder import build_context_text, build_question_chain_section
from .decision import CLARIFY_DECISION_PROMPT, ClarificationDecision, ClarificationDecisionError, decide

__all__ = [
    "CLARIFY_DECISION_PROMPT",
    "ClarificationDecision",
    "ClarificationDecisionError",
    "build_context_text",
    "build_question_chain_section",
    "decide",
]
