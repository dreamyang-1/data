"""Root-goal reporting. Execution results stay authoritative and unmodified."""
from __future__ import annotations

from typing import Any

from app.analysis.interpretation import AnswerPlan
from app.domain.models import AgentResponse, TaskExecutionResult, TaskPlan


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

    Missing or unknown references fall back to available results, not a new
    validation gate. Failed and incomplete branches are always disclosed.
    """
    final = final or {}
    available = [item for item in materials if item["status"] in {"COMPLETED", "PARTIAL_SUCCESS"}]
    available_ids = {item["task_id"] for item in available}
    requested = final.get("result_task_ids") or []
    valid_selection = bool(requested) and all(item in available_ids for item in requested)
    selected = [item for item in available if not valid_selection or item["task_id"] in requested]
    tables, charts, notes = [], [], []
    for item in materials:
        notes.extend(item.get("warnings") or [])
        notes.extend(item.get("presentation", {}).get("notes") or [])
        if item["status"] not in {"COMPLETED", "PARTIAL_SUCCESS"}:
            reason = "；".join(item.get("missing_information") or []) or str(item.get("summary") or "该部分未完成")
            notes.append(f"尚未完成的内容：{item['question']}。{reason}")
        for query in item.get("query_results") or []:
            if query.get("truncated"):
                notes.append(f"“{item['question']}”仅提供{query.get('returned_row_count', '部分')}条预览，不能作为全量统计。")
    for item in selected:
        presentation = item.get("presentation") or {}
        table = str(presentation.get("table") or "").strip()
        if table:
            tables.append((f"**{item['question']}**\n\n" if len(selected) > 1 else "") + table)
        elif item.get("summary"):
            # Non-tabular/empty query results are still meaningful, unlike a
            # dropped result or an invented zero. No task-number wrapper.
            tables.append(str(item["summary"]))
        if presentation.get("chart"):
            charts.append(presentation["chart"])
    if requested and not valid_selection:
        notes.append("结果展示选择未能完整对应已返回数据，已保留可用结果。")
    overview = final.get("overview") or (
        "以下为本次问题已获得的结果。" if available else "本次尚未获得可用于回答问题的数据。"
    )
    answer = AnswerPlan(
        headline=overview, key_facts=final.get("findings") or [],
        priorities=final.get("tips") or [], limitations=list(dict.fromkeys(notes)),
    ).render_report(question=question, table="\n\n".join(dict.fromkeys(tables)),
                    chart="\n\n".join(dict.fromkeys(charts)))
    return answer, [item["task_id"] for item in selected]
