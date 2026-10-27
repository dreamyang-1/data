"""结构化参数提取：提示词与提取出口逻辑。

提示词见 prompts.py；模型输出清洗、明细字段归位、目录单位换算与参数
转换见 extractor.py。任务拆分与模型调用仍在 app.planning 层。
"""
from app.structured_extraction.extractor import (
    EXTRACTION_INTENT_KEY,
    database_query_choice,
    extraction_to_parameters,
    normalize_extraction_metrics,
    normalize_metric_name,
    sanitize_extraction,
    strip_prose_placeholder_filters,
)
from app.structured_extraction.prompts import (
    CALCULATION_PARAMETER_PROMPT,
    EXTRACTION_EMBEDDED_SPEC,
    EXTRACTION_PARSER_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    PARAMETER_PROMPT,
)

__all__ = [
    "CALCULATION_PARAMETER_PROMPT",
    "EXTRACTION_EMBEDDED_SPEC",
    "EXTRACTION_INTENT_KEY",
    "EXTRACTION_PARSER_PROMPT",
    "EXTRACTION_SYSTEM_PROMPT",
    "PARAMETER_PROMPT",
    "database_query_choice",
    "extraction_to_parameters",
    "normalize_extraction_metrics",
    "normalize_metric_name",
    "sanitize_extraction",
    "strip_prose_placeholder_filters",
]
