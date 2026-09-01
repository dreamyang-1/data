from __future__ import annotations

import re

from app.domain.models import HistoryMessage


_USER_ANCHOR = re.compile(
    r"不是|改成|换成|只看|不看|排除|包含|口径|按.+(?:分析|拆分)|同比|环比|刚才|继续|接着"
)
_ASSISTANT_CLARIFICATION = re.compile(
    r"还需要补充|请补充|需要确认|请确认|哪个指标|时间范围|哪些字段|哪个维度|同比|环比"
)


def compact_history(
    history: list[HistoryMessage], *, maximum_messages: int = 40, recent_messages: int = 24
) -> list[HistoryMessage]:
    """Keep recent turns plus older task/correction/clarification anchors in order."""
    if len(history) <= maximum_messages:
        return list(history)
    recent_count = min(recent_messages, maximum_messages)
    selected = set(range(len(history) - recent_count, len(history)))

    first_user = next(
        (index for index, item in enumerate(history) if item.role == "user"), None
    )
    priority: list[int] = []
    if first_user is not None:
        priority.append(first_user)
        if first_user + 1 < len(history) and history[first_user + 1].role == "assistant":
            priority.append(first_user + 1)

    for index in range(len(history) - recent_count - 1, -1, -1):
        item = history[index]
        if item.role == "user" and _USER_ANCHOR.search(item.content):
            priority.extend((index, index + 1))
        elif item.role == "assistant" and _ASSISTANT_CLARIFICATION.search(item.content):
            priority.extend((index - 1, index))

    for index in priority:
        if len(selected) >= maximum_messages:
            break
        if 0 <= index < len(history):
            selected.add(index)
    return [history[index] for index in sorted(selected)]
