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
