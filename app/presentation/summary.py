"""Small presentation helpers; never calculate or replace query facts."""
import re
from typing import Any


def result_introduction(question: str, *, analysis: bool = False) -> str:
    """Introduce results without turning the user's question into a broken title.

    The factual summary below provides question-specific context. Keep this lead
    short: stripping colloquial verbs or appending a suffix to chart instructions
    can leave fragments and repeated words. No new model call is needed.
    """
    analysis = analysis or bool(re.search(r'分析|比较|对比|趋势|排名|最高|最低|预测', question))
    return '分析结果如下：' if analysis else '查询结果如下：'


def brief_summary(value: Any) -> str:
    """Keep a few complete prose sentences, not tables, links or insight reports."""
    if not isinstance(value, str):
        return ''
    lines = [line.strip() for line in value.splitlines() if line.strip()
             and not line.lstrip().startswith(('|', '#', '<', '!['))]
    text = ' '.join(lines).strip()
    if not text or '<svg' in text or re.search(r'\]\(https?://', text):
        return ''
    sentences = re.split(r'(?<=[。！？!?])\s*', text)
    selected = []
    for sentence in sentences:
        if not sentence:
            continue
        if selected and sum(map(len, selected)) + len(sentence) > 240:
            break
        selected.append(sentence)
        if len(selected) >= 3:
            break
    return ''.join(selected)
