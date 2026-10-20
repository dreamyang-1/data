"""澄清判定上下文构建：问题链 + 上一轮结果 + 当前问题。

结果概要按任务表存储内容原样渲染，不做二次截断；采样与单值截断口径
由概要生成侧（编排器 _summarize_query_result）统一决定。
"""
from __future__ import annotations

from typing import Any


def build_question_chain_section(question_log: list[dict]) -> str:
    """会话问题链渲染，按时间从早到近排列。

    存储侧新条目在前（lpush 写入），这里反转成自然顺序。每轮条目
    附带的系统反问与缺失参数一并渲染，判定时用于识别本轮回复与上一轮
    反问的对应关系。
    """
    entries = [entry for entry in reversed(question_log) if isinstance(entry, dict)]
    if not entries:
        return ""
    lines = ["【近期用户问题】（从早到近）"]
    for index, entry in enumerate(entries, 1):
        question = str(entry.get("question") or "").strip()
        if not question:
            continue
        line = f"{index}. {question}"
        answer = str(entry.get("answer_summary") or "").strip()
        if answer:
            line += f"\n   回答摘要：{answer}"
        asked = entry.get("clarification_questions") or []
        asked = [str(item).strip() for item in asked if str(item).strip()]
        if asked:
            line += "\n   系统反问：" + "；".join(asked)
        missing = entry.get("missing_slots") or []
        missing = [str(item).strip() for item in missing if str(item).strip()]
        if missing:
            line += "\n   缺失参数：" + "、".join(missing)
        lines.append(line)
    return "\n".join(lines)


def build_result_section(task_table: dict | None) -> str:
    """上一轮查询结果渲染，按结果概要存储内容全列输出。"""
    if not isinstance(task_table, dict):
        return ""
    lines: list[str] = ["【上一轮查询结果】"]
    question = str(task_table.get("question") or "").strip()
    if question:
        lines.append(f"上一轮问题：{question}")
    rows = task_table.get("tasks") or []
    rendered = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("名称") or "").strip()
        summary = row.get("结果概要")
        if not name and not isinstance(summary, dict):
            continue
        rendered += 1
        detail: list[str] = []
        if isinstance(summary, dict):
            columns = summary.get("columns")
            if columns:
                detail.append("结果列：" + "、".join(str(c) for c in columns))
            if summary.get("row_count") is not None:
                detail.append(f"共{summary['row_count']}行")
            sample = summary.get("sample")
            if isinstance(sample, list) and sample and isinstance(sample[0], dict):
                for sample_row in sample:
                    pairs = "；".join(
                        f"{key}={value}" for key, value in sample_row.items()
                    )
                    detail.append(pairs)
        lines.append(("- " + name) if name else "- （未命名任务）")
        if detail:
            lines.extend("  " + item for item in detail)
    if not rendered:
        return ""
    return "\n".join(lines)


def build_context_text(
    *,
    current_question: str,
    question_log: list[dict] | None = None,
    task_table: dict | None = None,
    extra_sections: dict[str, Any] | None = None,
) -> str:
    """拼装判定模型输入。

    extra_sections 供调用方补充结构化参数等附加区块，键为区块标题、
    值为文本；模块不依赖任何特定键。
    """
    sections: list[str] = []
    chain = build_question_chain_section(question_log or [])
    if chain:
        sections.append(chain)
    result = build_result_section(task_table)
    if result:
        sections.append(result)
    sections.append(f"【本轮用户问题】\n{str(current_question or '').strip()}")
    for title, body in (extra_sections or {}).items():
        text = str(body or "").strip()
        if text:
            sections.append(f"【{title}】\n{text}")
    return "\n\n".join(sections)
