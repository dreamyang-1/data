"""Small presentation helpers; never calculate or replace query facts."""
import re
from typing import Any


def result_introduction(question: str, *, analysis: bool = False) -> str:
    """Describe the completed question without generating new business facts."""
    title = ' '.join(question.split()).strip().rstrip('？?。！!；;')
    analysis = analysis or bool(re.search(r'分析|比较|对比|趋势|排名|最高|最低|预测', title))
    title = re.sub(r'^(?:请帮我|请问|帮我|请)\s*', '', title)
    title = re.sub(r'^(?:查询|统计|查看|列出|分析)\s*', '', title)
    title = re.sub(r'(?:是多少|是什么|有哪些)$', '', title).strip()
    title = re.sub(r'(?:查询|分析)$', '', title).rstrip('：:，, ')
    return f"{title or '本次问题'}{'分析' if analysis else '查询'}结果如下："


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
