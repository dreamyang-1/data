"""Canonical user-visible names for the query execution pipeline."""

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
