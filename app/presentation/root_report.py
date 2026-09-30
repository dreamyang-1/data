"""Root-goal reporting. Execution results stay authoritative and unmodified."""
from __future__ import annotations

from typing import Any

from app.analysis.interpretation import AnswerPlan
from app.domain.models import AgentResponse, TaskExecutionResult, TaskPlan


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
        clarification_items = [
            item for item in materials if item["status"] == "NEEDS_CLARIFICATION"
        ]
        if clarification_items:
            # No query result exists yet, so an insight/report template would
            # produce empty sections such as "no findings" and "no data".
            # Return the actionable clarification itself and omit dependent
            # skipped tasks, which add no new information for the user.
            clarification_texts: list[str] = []
            for item in clarification_items:
                text = str(item.get("summary") or "").strip()
                if not text:
                    text = "\n".join(
                        str(value).strip()
                        for value in item.get("missing_information", [])
                        if str(value).strip()
                    )
                if not text:
                    text = "该任务还需要补充信息后才能继续查询。"
                if len(clarification_items) > 1:
                    text = f"{item.get('question') or '需要补充的信息'}：\n{text}"
                clarification_texts.append(text)

            # Keep unrelated hard failures visible without reintroducing the
            # generic insight template or downstream dependency noise.
            clarification_ids = {item["task_id"] for item in clarification_items}
            for item in materials:
                if item["status"] not in {"FAILED", "SAFE_FALLBACK"}:
                    continue
                if clarification_ids.intersection(item.get("depends_on", [])):
                    continue
                failure = str(item.get("summary") or "该任务未能完成。").strip()
                if failure:
                    clarification_texts.append(
                        f"{item.get('question') or '其他任务'}：{failure}"
                    )
            return "\n\n".join(clarification_texts), []

    available_ids = {item["task_id"] for item in available}
    requested = final.get("result_task_ids") or []
    valid_selection = bool(requested) and all(item in available_ids for item in requested)
    if valid_selection:
        selected = [item for item in available if item["task_id"] in requested]
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
            notes.append(f"尚未完成的内容：{item['question']}。{reason}")
        for query in (item.get("query_results") or []) if item["task_id"] in selected_ids else []:
            if query.get("truncated"):
                notes.append(f"“{item['question']}”仅提供{query.get('returned_row_count', '部分')}条预览，不能作为全量统计。")
    for item in selected:
        presentation = item.get("presentation") or {}
        table = str(presentation.get("table") or "").strip()
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
    overview = final.get("overview") or (
        str(selected[0].get("summary") or "以下为本次问题已获得的结果。")
        if len(selected) == 1 and selected[0].get("facts", {}).get("computation")
        else "以下为本次问题已获得的结果。" if available else "本次尚未获得可用于回答问题的数据。"
    )
    answer = AnswerPlan(
        headline=overview, key_facts=final.get("findings") or [],
        priorities=final.get("tips") or [], limitations=list(dict.fromkeys(notes)),
    ).render_report(question=question, table="\n\n".join(dict.fromkeys(tables)),
                    chart="\n\n".join(dict.fromkeys(charts)))
    return answer, [item["task_id"] for item in selected]
