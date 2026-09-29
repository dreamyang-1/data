"""Canonical user-visible names for the query execution pipeline."""

import json

SEMANTIC_QUERY_TOOL_NAME = "智能语义查询器（ASL 结构化提取）"
SQL_TRANSLATION_TOOL_NAME = "SQL 翻译服务"
SQL_EXECUTION_TOOL_NAME = "SQL 执行服务"

QUERY_EXECUTION_CHAIN = " → ".join((
    SEMANTIC_QUERY_TOOL_NAME,
    SQL_TRANSLATION_TOOL_NAME,
    SQL_EXECUTION_TOOL_NAME,
    "数据集输出",
    "结果校验",
    "洞察分析",
))


def render_sql_execution_defaults(summary: dict) -> str:
    """Explain translator-owned defaults, without guessing or changing ASL."""
    lines = []
    if "effective_limit" in summary:
        limit = summary["effective_limit"]
        source = summary.get("limit_source")
        if source == "DETAIL_DEFAULT":
            lines.append(f"实际执行上限：{limit} 行（系统明细查询默认上限；ASL 未指定 limit，不代表不限行数）。")
        elif source == "ASL":
            lines.append(f"实际执行上限：{limit} 行（来自 ASL.limit）。")
        elif source == "NONE" and limit is None:
            lines.append("实际执行上限：未添加 LIMIT（ASL 未指定上限，本次不是明细查询）。")
    if "system_filters" in summary:
        rules = summary.get("system_filters")
        if isinstance(rules, list):
            lines.append("系统补充筛选（不属于用户 ASL.filters）：" + (
                json.dumps(rules, ensure_ascii=False) if rules else "无。"
            ))
    if not lines:
        return "执行补充说明：SQL 服务未提供实际上限及系统筛选摘要，请以本次执行 SQL 为准。"
    return "\n".join(lines)


def executed_asl_metrics(asl: dict) -> list[dict[str, str]]:
    """Read actual selected codes/labels, not advisory request bindings.

    Callers supply the validated execution ASL. This is presentation metadata,
    not a catalog lookup, ID generator or additional semantic validation.
    """
    metrics = asl.get("metrics")
    if not isinstance(metrics, list):
        return []
    result = []
    seen = set()
    for item in metrics:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"].strip()
        if not name:
            continue
        alias = item.get("alias")
        alias = alias.strip() if isinstance(alias, str) else ""
        pair = (name, alias)
        if pair not in seen:
            result.append({"name": name, "alias": alias})
            seen.add(pair)
    return result
