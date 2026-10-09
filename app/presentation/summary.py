"""Small presentation helpers; never calculate or replace query facts."""
import re
from typing import Any


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
