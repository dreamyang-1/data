"""Root-goal reporting. Execution results stay authoritative and unmodified."""
from __future__ import annotations

from typing import Any

from app.analysis.interpretation import AnswerPlan
from app.domain.models import AgentResponse, TaskExecutionResult, TaskPlan
from app.analysis.visualization import requested_chart_specs
from app.presentation.summary import brief_summary


def preview_result_tables(text: str) -> str:
    """Limit rendered table bodies only; never slice execution/model datasets."""
    lines, table, output = text.splitlines(), [], []
    def flush():
        if not table:
            return
        output.extend(table[:22])  # header, separator, twenty data rows
        if len(table) > 22:
            output.extend(["", f"> 本表共 {len(table) - 2} 行，当前仅展示前 20 行；完整结果及下载状态见附件说明。", ""])
        table.clear()
    for line in lines:
        if line.lstrip().startswith("|"):
            table.append(line)
        else:
            flush()
            output.append(line)
    flush()
    return "\n".join(output)


def result_table_title(material: dict[str, Any], titles: Any) -> str:
    """Use root-question deliverable wording, never generate titles from cells.

    Missing optional presentation metadata must not block a usable answer.
    A calculation's last-step question is not a complete deliverable title.
    """
    title = titles.get(material["task_id"]) if isinstance(titles, dict) else None
    if isinstance(title, str) and title.strip():
        return " ".join(title.split()).strip("#*`")
    if material.get("facts", {}).get("computation"):
        return "综合计算结果"
    return str(material.get("question") or "查询结果")


def collect_root_materials(
    plan: TaskPlan, results: list[TaskExecutionResult],
    responses: dict[str, Any], deferred: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    indexed = {item.task_id: item for item in results}
    materials = []
    for task in plan.tasks:
        result = indexed[task.task_id]
        response = responses.get(task.task_id)
        material = dict(deferred.get(task.task_id) or {})
        material.update(task_id=task.task_id, status=result.status,
                        question=material.get("question") or task.question,
                        depends_on=list(task.depends_on), expected_output=task.expected_output,
                        structured_parameters=material.get("structured_parameters") or task.extraction)
        material.setdefault("summary", result.answer)
        material["calculation_incomplete"] = bool(
            result.status == "FAILED" and result.reliability
            and result.reliability.gates.get("deterministic_computation") is False
        )
        # Old checkpoints may retain the former generic calculation paragraph.
        # Only shorten that known boilerplate, never a specific failure reason.
        if material["calculation_incomplete"] and str(material["summary"]).startswith(
            "本次尚未生成可复核的计算结果，计算、排序或筛选未完成。"
        ):
            material["summary"] = "计算未完成，暂未获得可用的计算结果。"
        material.setdefault("facts", {})
        material["warnings"] = list(dict.fromkeys([
            *material.get("warnings", []),
            *(result.reliability.warnings if result.reliability else []),
        ]))
        # Cached/empty/failed branches may have no deferred insight. Their status
        # and evidence must still reach the root instead of silently disappearing.
        if isinstance(response, AgentResponse):
            material["query_results"] = [item.payload for item in response.evidence if item.kind == "QUERY_RESULT"]
            material["missing_information"] = list(response.clarification_questions)
        if "presentation" not in material:
            table = "\n".join(line for line in result.answer.splitlines() if line.lstrip().startswith("|"))
            material["presentation"] = {"table": table, "chart": "", "notes": []}
        materials.append(material)
    return materials


def render_root_report(
    question: str, materials: list[dict[str, Any]], final: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    """Model selects evidence IDs, never supplies table cells/chart URLs.

    Missing model metadata uses completed dependency outputs, not every input
    table. Independent outputs stay independent; failed branches remain visible.
    """
    final = final or {}
    available = [item for item in materials if item["status"] in {"COMPLETED", "PARTIAL_SUCCESS"}]
    if not available:
        # No completed output: all terminal failure kinds bypass insight
        # templates, including stale optional model-generated final metadata.
        # Hide only dependent SKIPPED noise; independent failures remain visible.
        task_ids = {item["task_id"] for item in materials}
        actionable = [
            item for item in materials
            if not (item["status"] == "SKIPPED" and task_ids.intersection(item.get("depends_on", [])))
        ]
        if not actionable:
            actionable = materials
        messages: list[str] = []
        for item in actionable:
            text = str(item.get("summary") or "").strip()
            if not text:
                text = "\n".join(
                    str(value).strip()
                    for value in item.get("missing_information", [])
                    if str(value).strip()
                )
            if not text:
                text = (
                    "该任务还需要补充信息后才能继续查询。"
                    if item["status"] == "NEEDS_CLARIFICATION"
                    else "该任务未能完成，暂未提供具体原因，请重试或联系管理员查看执行日志。"
                )
            if len(actionable) > 1:
                text = f"{item.get('question') or '未完成的任务'}：\n{text}"
            messages.append(text)
        return "\n\n".join(dict.fromkeys(messages)) or "本次任务未执行成功，尚未获得查询结果，请重试或联系管理员查看执行日志。", []

    available_ids = {item["task_id"] for item in available}
    requested = final.get("result_task_ids") or []
    valid_selection = bool(requested) and all(item in available_ids for item in requested)
    if valid_selection:
        selected = [item for item in available if item["task_id"] in requested]
        # A real computed deliverable supersedes its intermediate statistics.
        # Optional model selection must not replace TopN with its raw input.
        computed = [item for item in available if item.get('facts', {}).get('delivery_operation')]
        for item in computed:
            deps = set(item.get('depends_on', []))
            if deps.intersection(value['task_id'] for value in selected):
                selected = [value for value in selected if value['task_id'] not in deps]
                if item not in selected:
                    selected.append(item)
    else:
        # Use the actual dependency graph, not keywords or the last task index.
        # Only a successful downstream result supersedes its inputs. If the
        # final calculation fails, keep the available evidence and explain why.
        consumed = {dep for item in available for dep in item.get("depends_on", [])}
        selected = [item for item in available if item["task_id"] not in consumed] or available
    selected_ids = {item["task_id"] for item in selected}
    tables, charts, notes = [], [], []
    for item in materials:
        if item["task_id"] in selected_ids or item["status"] != "COMPLETED":
            notes.extend(item.get("warnings") or [])
        if item["task_id"] in selected_ids:
            notes.extend(item.get("presentation", {}).get("notes") or [])
        if item["status"] not in {"COMPLETED", "PARTIAL_SUCCESS"}:
            reason = "；".join(item.get("missing_information") or []) or str(item.get("summary") or "该部分未完成")
            if item.get("calculation_incomplete"):
                # A short real failure status is sufficient; do not repeat the
                # planner's entire calculation instruction in business tips.
                notes.append(reason)
            else:
                label = ("未执行的后续任务" if item["status"] == "SKIPPED" and item.get("depends_on")
                         else "尚未完成的内容")
                notes.append(f"{label}：{item['question']}。{reason}")
        for query in (item.get("query_results") or []) if item["task_id"] in selected_ids else []:
            if query.get("truncated"):
                notes.append(f"“{item['question']}”仅提供{query.get('returned_row_count', '部分')}条预览，不能作为全量统计。")
    for item in selected:
        presentation = item.get("presentation") or {}
        table = preview_result_tables(str(presentation.get("table") or "").strip())
        if table:
            tables.append((f"**{result_table_title(item, final.get('result_titles'))}**\n\n" if len(selected) > 1 else "") + table)
        elif item.get("summary"):
            # Non-tabular/empty query results are still meaningful, unlike a
            # dropped result or an invented zero. No task-number wrapper.
            tables.append(str(item["summary"]))
        if presentation.get("chart"):
            charts.append(presentation["chart"])
    if requested and not valid_selection:
        notes.append("部分汇总信息未生成，以下保留已完成的结果。")
    overview = brief_summary(final.get("overview")) or ' '.join(dict.fromkeys(
        summary for item in selected[:2] if (summary := brief_summary(item.get('summary')))
    )) or "以下为本次问题已获得的结果。"
    answer = AnswerPlan(
        headline=overview, key_facts=final.get("findings") or [],
        priorities=final.get("tips") or [], limitations=list(dict.fromkeys(notes)),
    ).render_report(question=question, table="\n\n".join(dict.fromkeys(tables)),
                    chart="\n\n".join(dict.fromkeys(charts)))
    return answer, [item["task_id"] for item in selected]


def apply_requested_root_charts(question, materials, selected_ids, render):
    """Draw only delivered datasets, including dependent computed outputs."""
    specifications = None
    for item in materials:
        if item['task_id'] not in selected_ids:
            continue
        facts = item.get('facts') or {}
        data = facts.get('query_data') or {}
        requested, notes = requested_chart_specs(question, data.get('columns') or [],
            data.get('rows') or [], facts)
        if requested is None:
            continue
        if specifications is None:
            specifications = []
        room = max(0, 3 - len(specifications))
        if len(requested) > room:
            notes.append('本次已达到3张图的展示上限，其余结果仍保留在表格和附件中。')
        requested = requested[:room]
        specifications.extend(requested)
        images = render(chart_specs=[spec.model_dump(mode='json') for spec in requested])
        presentation = item.setdefault('presentation', {})
        presentation['chart'] = ('\n\n#### 图表\n\n' + '\n\n'.join(images)) if images else ''
        if requested and not images:
            notes.append('所要求的图表暂未渲染成功，已保留查询结果，未提供虚构图片链接。')
        presentation.setdefault('notes', []).extend(notes)
    return specifications
