"""原文与 ASL 参考信息组装（surface payload handoff）。

本模块只做纯组装：把用户确认后的完整原文与细粒度提取结果组装成
ASL 入口输入，不做业务分词、角色映射、指标/维度匹配，不注入默认
时间，不读取目录，不调用大模型。

合同边界（见 E:/YouoAgent/_agent_coordination/SURFACE_PARALLEL_CONTRACT.md）：
- 原文是 ASL 主输入，必须逐字保留（中文、空格、标点、前后空白均不改写）。
- surface_evidence 中的每项仅允许 ``text`` 与可选 ``role_hint``；
  ``field_id``、``confirmed``、``scope`` 等任何多余键一律拒绝。
- 角色提示只是假设，不能变成已确认约束；不输出任何"已确认"标记。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# 与共用合同一致的硬上限：不截断、超限即拒绝。
MAX_QUESTION_CHARS = 4000
MAX_MENTIONS = 50
MAX_MENTION_TEXT_CHARS = 300
MAX_ROLE_HINT_CHARS = 80

_ALLOWED_MENTION_KEYS = ("text", "role_hint")


def build_surface_asl_input(
    completed_question: str,
    mentions: list[dict],
    structured_extraction: dict | None = None,
) -> dict:
    """组装 ASL 入口输入。

    返回且仅返回三个键：
    - ``query`` / ``retrieval_query``：均为完全不变的 ``completed_question``；
    - ``surface_evidence``：``{"mentions": [...]}``，每项为新建字典。

    ``structured_extraction`` 为任务规划产出的结构化提取JSON，提供时原样
    透传为第四个键，语义查询器直接消费，键级内容不在本层校验。

    非法输入一律抛 ``ValueError``：不截断、不猜测、不做类型转换。
    """
    _require_valid_question(completed_question)
    validated_mentions = _validate_mentions(mentions)
    payload = {
        "query": completed_question,
        "retrieval_query": completed_question,
        "surface_evidence": {"mentions": validated_mentions},
    }
    if structured_extraction is not None:
        if not isinstance(structured_extraction, dict):
            raise ValueError("structured_extraction 必须是字典")
        payload["structured_extraction"] = deepcopy(structured_extraction)
    return payload


def _require_valid_question(completed_question: Any) -> None:
    if not isinstance(completed_question, str):
        raise ValueError("completed_question 必须是字符串")
    if len(completed_question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"completed_question 超过 {MAX_QUESTION_CHARS} 字符上限"
        )
    if not completed_question.strip():
        raise ValueError("completed_question 不能是非空白字符串以外的内容")


def _validate_mentions(mentions: Any) -> list[dict]:
    if not isinstance(mentions, list):
        raise ValueError("mentions 必须是列表")
    if len(mentions) > MAX_MENTIONS:
        raise ValueError(f"mentions 超过 {MAX_MENTIONS} 项上限")

    validated: list[dict] = []
    for index, mention in enumerate(mentions):
        if not isinstance(mention, dict):
            raise ValueError(f"mentions[{index}] 必须是字典")
        extra_keys = set(mention) - set(_ALLOWED_MENTION_KEYS)
        if extra_keys:
            raise ValueError(
                f"mentions[{index}] 含不允许的键"
            )
        if "text" not in mention:
            raise ValueError(f"mentions[{index}] 缺少 text")

        text = mention["text"]
        if not isinstance(text, str):
            raise ValueError(f"mentions[{index}].text 必须是字符串")
        if not 1 <= len(text) <= MAX_MENTION_TEXT_CHARS:
            raise ValueError(
                f"mentions[{index}].text 长度必须在 1~{MAX_MENTION_TEXT_CHARS} 字符之间"
            )

        item: dict[str, Any] = {"text": text}
        if "role_hint" in mention:
            role_hint = mention["role_hint"]
            if role_hint is not None and not isinstance(role_hint, str):
                raise ValueError(f"mentions[{index}].role_hint 必须是字符串")
            if role_hint is not None and len(role_hint) > MAX_ROLE_HINT_CHARS:
                raise ValueError(
                    f"mentions[{index}].role_hint 超过 {MAX_ROLE_HINT_CHARS} 字符上限"
                )
            item["role_hint"] = role_hint
        validated.append(item)
    return validated
