"""澄清判定：单次模型调用输出关系判定与补全后的问题。

判定与补全共用同一份上下文：上下文中能够落实的指代在补全问题里替换
为具体值；缺少必需参数且无法从上下文取得时才输出反问。模型调用经
complete 回调注入，接入方可以是识别模型、分诊模型或独立网关。

失败分层：模型调用异常、输出非 JSON、relation 与载荷不匹配都抛出
ClarificationDecisionError，由接线层按系统失败处理（走既有
SAFE_FALLBACK 链路），不转成对用户的澄清反问——模型故障不能伪装成
用户问题不清楚。只有模型正常输出 CLARIFY 才生成反问。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


class ClarificationDecisionError(RuntimeError):
    """判定模型不可用或输出不合规。接线层捕获后按系统失败兜底。"""


@dataclass
class ClarificationDecision:
    relation: Literal["NEW_TASK", "COMPLETE", "CLARIFY"]
    # relation=COMPLETE 时的自包含完整问题；CLARIFY 时为空
    completed_question: str | None = None
    # relation=CLARIFY 时需要用户回答的问题清单
    questions: list[str] = field(default_factory=list)
    # 与 questions 按位对应的候选值清单（借鉴 New_Agent 的 dropdown 参数卡）：
    # 有候选的问句给出 options，用户可直接按序号或候选名回答；无候选为空数组
    options: list[list[str]] = field(default_factory=list)
    # 判定依据，用于前端判定思考行展示
    reason: str = ""


CLARIFY_DECISION_PROMPT = """你在判断用户最新问题与会话上下文的关系，并决定是否需要用户补全参数。

判断属于哪一类，只输出 JSON：
- 上下文里没有任何关联的往轮问题，或用户明显换了话题：
{"relation": "NEW_TASK", "completed_question": "把本轮问题原样整理成一句完整业务问题", "reason": "一句话第一人称说明判断依据"}
- 本轮问题承接往轮（追问、改条件、回复系统反问），且结合上下文与上一轮结果能把指代和缺失参数补全：
{"relation": "COMPLETE", "completed_question": "补全后的一句完整自包含业务问题", "reason": "一句话第一人称说明补全依据"}
- 本轮问题承接往轮，但有必需参数上下文里没有、也没法合理推断，必须问用户：
{"relation": "CLARIFY", "questions": ["问题1", "问题2"], "options": [["候选A", "候选B"], []], "reason": "一句话第一人称说明为什么问"}

补全规则：
- completed_question 只合并上下文和本轮问题里出现过的指标、维度、筛选和时间，不发明两边都没有的条件。
- 会话问答记录里往轮已确认的有效参数（实体、时间范围、指标、筛选）必须全部带进 completed_question，再叠加本轮补上的参数，最终一句问题把有效参数都带上；系统反问过但用户没答的参数不得编造。
- 本轮问题提到上一轮结果里的对象（如"这些""上述N家"）时，只把指代替换成上一轮查询结果采样中出现过的具体值。采样里没有的对象不得编造或凑数：采样覆盖不了完整名单时，输出 CLARIFY，在 questions 里请用户列出具体对象；或保持指代并沿用上轮结果的口径，由结果集引用机制处理。
- 系统此前反问过的问题，用户的回复优先视为对该反问的回答，补全时用候选或回复中的规范名称。
- 对话历史里系统已反问过且用户已回答的参数视为已确认，禁止再次追问同一个参数。
- 可默认的参数不追问：时间未指定按近12个月处理；排序方向、展示格式、图表类型这类不影响查什么的偏好不追问。
- 必须追问的参数：实体名称存在多种理解、指标口径有歧义、业务域归属不明——这些选错会查出完全不同的结果。
- questions 一次最多两个，每个问题独立成句，用户能直接一句话回答。能从上下文或常识列出有限候选的问题，在 options 对应位置给出候选数组（如"要查哪个区域？"配 ["华东", "华南"]）；没有明确候选的问句对应空数组 []，options 可整体省略。
"""


async def decide(
    *,
    current_question: str,
    context_text: str,
    complete: Callable[[str, str], Awaitable[str]],
) -> ClarificationDecision:
    """执行判定。complete(instruction, context_text) 返回模型 JSON 文本。

    complete 抛出的异常原样向上传播；输出非 JSON、relation 与载荷
    不匹配（如 COMPLETE 缺问题文本）抛出 ClarificationDecisionError。
    两者都由接线层按系统失败处理，不转用户澄清。
    """
    raw = await complete(CLARIFY_DECISION_PROMPT, context_text)
    if not str(raw or "").strip():
        logger.warning(
            "clarification decision empty output: context_chars=%d",
            len(context_text or ""),
        )
        raise ClarificationDecisionError("判定输出为空")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        # 记录模型原始输出头部，定位是格式漂移还是网关返回了非预期内容
        logger.warning(
            "clarification decision unparseable output: head=%r",
            str(raw)[:200],
        )
        raise ClarificationDecisionError("判定输出不是合法JSON") from exc
    if not isinstance(data, dict):
        raise ClarificationDecisionError("判定输出不是JSON对象")
    relation = str(data.get("relation") or "").strip().upper()
    reason = str(data.get("reason") or "").strip()
    completed = str(data.get("completed_question") or "").strip()
    questions = [
        str(item).strip()
        for item in (data.get("questions") or [])
        if str(item).strip()
    ][:2]
    if relation == "NEW_TASK":
        return ClarificationDecision(
            relation="NEW_TASK",
            completed_question=completed or current_question,
            reason=reason,
        )
    if relation == "COMPLETE" and completed:
        return ClarificationDecision(
            relation="COMPLETE", completed_question=completed, reason=reason
        )
    if relation == "CLARIFY" and questions:
        raw_options = data.get("options")
        options: list[list[str]] = []
        if isinstance(raw_options, list):
            for item in raw_options:
                if isinstance(item, list):
                    options.append(
                        [str(value).strip() for value in item if str(value).strip()]
                    )
                else:
                    options.append([])
        # options 与 questions 按位对齐：缺位补空，超长截断
        options = options[: len(questions)]
        options.extend([] for _ in range(len(questions) - len(options)))
        return ClarificationDecision(
            relation="CLARIFY", questions=questions, options=options, reason=reason
        )
    raise ClarificationDecisionError(
        f"判定relation与载荷不匹配: relation={relation or '空'}"
    )
