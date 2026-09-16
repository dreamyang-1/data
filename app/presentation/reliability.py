"""Public, presentation-only rendering for result reliability evidence.

The execution contracts intentionally retain stable English enum values.  This
module translates those values only at the UI boundary and summarizes evidence
without exposing internal identifiers, paths, or data-source credentials.
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.models import EvidenceItem, PrimaryIntent, ReliabilityReport


_INTENT_LABELS = {
    PrimaryIntent.CHAT: "闲聊",
    PrimaryIntent.METRIC_QUERY: "指标查询",
    PrimaryIntent.DETAIL_QUERY: "明细查询",
    PrimaryIntent.TREND_ANALYSIS: "趋势分析",
    PrimaryIntent.COMPARISON_ANALYSIS: "对比分析",
    PrimaryIntent.COMPOSITION_ANALYSIS: "占比分析",
    PrimaryIntent.ANOMALY_ANALYSIS: "异常分析",
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: "归因分析",
    PrimaryIntent.FORECAST_ANALYSIS: "预测分析",
    PrimaryIntent.REPORT_GENERATION: "报表生成",
    PrimaryIntent.METRIC_DEFINITION: "指标口径说明",
    PrimaryIntent.DATA_LINEAGE: "数据血缘",
    PrimaryIntent.DATA_QUALITY: "数据质量",
    PrimaryIntent.CAPABILITY_HELP: "能力说明",
    PrimaryIntent.OUT_OF_SCOPE: "非数据任务",
}

_QUALITY_LABELS = {
    "PASS": "通过",
    "FAIL": "不通过",
    "FAILED": "不通过",
    "INVALID": "无效",
    "ERROR": "异常",
    "WARNING": "有告警",
    "WARN": "有告警",
    "DEGRADED": "降级",
    "LIMITED": "受限",
}

_RELIABILITY_LABELS = {
    "HIGH": "高可信",
    "LIMITED": "有限可信",
    "FAIL": "未通过",
}


def intent_label_zh(intent: PrimaryIntent) -> str:
    """Return the public Chinese label while preserving the internal enum."""

    return _INTENT_LABELS.get(intent, "其他分析")


def quality_status_label_zh(value: str) -> str:
    """Translate an internal quality status for public display."""

    return _QUALITY_LABELS.get(str(value or "").strip().upper(), "待确认")


def reliability_level_label_zh(value: str) -> str:
    """Translate an internal reliability level for public display."""

    return _RELIABILITY_LABELS.get(str(value or "").strip().upper(), "待确认")


def _compact_text(value: Any, *, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _compact_list(value: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _compact_text(item, limit=60)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _evidence_line(item: EvidenceItem) -> str:
    payload = item.payload if isinstance(item.payload, dict) else {}

    if item.kind == "QUERY_RESULT":
        row_count = payload.get("row_count")
        columns = _compact_list(payload.get("columns"))
        row_text = f"返回 {row_count} 行" if isinstance(row_count, int) else "已返回数据集"
        column_text = f"，字段包括{'、'.join(columns)}" if columns else ""
        return (
            f"查询结果：{row_text}{column_text}"
            "（来源：本轮只读数据库查询返回的数据集，用于证明展示结果来自实际查询）"
        )

    if item.kind == "SEMANTIC_METRIC_RESOLUTION":
        metric = _compact_text(payload.get("canonical_name") or payload.get("metric_name"))
        detail = f"指标“{metric}”已完成口径绑定" if metric else "查询指标已完成口径绑定"
        return (
            f"指标口径证据：{detail}"
            "（来源：智能语义查询器生成的查询结构与当前语义模型，用于证明指标含义已经核验）"
        )

    if item.kind == "DERIVED_METRIC_RESOLUTION":
        metric = _compact_text(
            payload.get("canonical_name") or payload.get("metric_name")
        )
        detail = f"派生指标“{metric}”已复算" if metric else "派生指标已复算"
        return (
            f"派生指标证据：{detail}"
            "（来源：经审计的查询转换，用于证明派生值由实际查询结果计算）"
        )

    if item.kind == "ANALYSIS_RESULT":
        return (
            "分析结果证据：已形成可复算的确定性分析结果"
            "（来源：本轮查询数据和确定性分析器，用于证明分析结论可由数据复核）"
        )

    if item.kind == "ANSWER_SYNTHESIS":
        claim_count = payload.get("claim_count")
        detail = f"已整理 {claim_count} 条已验证结论" if isinstance(claim_count, int) else "已整理已验证结论"
        return (
            f"回答整理证据：{detail}"
            "（来源：受证据约束的回答整理模型，只负责组织已核验事实）"
        )

    if item.kind == "ANALYSIS_KNOWLEDGE":
        hit_count = payload.get("hit_count")
        detail = f"检索到 {hit_count} 条相关资料" if isinstance(hit_count, int) else "已检索相关资料"
        return (
            f"业务资料证据：{detail}"
            "（来源：当前请求授权的知识库，用于补充业务解释）"
        )

    if item.kind == "KNOWLEDGE_DOCUMENT":
        return (
            "知识文档证据：已取得与问题相关的文档片段"
            "（来源：当前请求授权的知识库文档，用于支持本轮回答）"
        )

    if item.kind == "CONFIRMED_LONG_TERM_MEMORY":
        memory_ids = payload.get("memory_ids")
        count = len(memory_ids) if isinstance(memory_ids, list) else 0
        detail = f"使用 {count} 条用户已确认偏好" if count else "使用用户已确认偏好"
        return (
            f"会话偏好证据：{detail}"
            "（来源：当前用户已确认的长期记忆，不包含其他用户或会话信息）"
        )

    if item.kind == "WEB_SEARCH_RESULT":
        records = payload.get("records")
        count = len(records) if isinstance(records, list) else 0
        detail = f"取得 {count} 条公开信息" if count else "已取得公开信息"
        return (
            f"公开信息证据：{detail}"
            "（来源：本轮请求提供的联网搜索能力，仅作为补充参考）"
        )

    if item.kind == "DERIVED_RESULT_COMPARISON":
        return (
            "对比结果证据：已基于同一会话的数据集完成确定性对比"
            "（来源：本轮选定的不可变查询结果，用于证明对比值可复算）"
        )

    if item.kind in {"METRIC_DEFINITION", "DATA_LINEAGE"}:
        label = "指标定义" if item.kind == "METRIC_DEFINITION" else "数据血缘"
        return (
            f"{label}证据：已读取当前发布的语义资料"
            f"（来源：当前授权语义模型的{label}服务，用于核验回答依据）"
        )

    if item.kind == "DEPENDENCY_CONSTRAINT":
        return (
            "任务依赖证据：已将上游结果编译为受控过滤条件"
            "（来源：本轮多任务执行链，用于证明下游查询没有扩大范围）"
        )

    if item.kind == "DEPENDENCY_CONSTRAINT_REJECTED":
        return (
            "任务依赖保护证据：不安全的依赖条件已被拒绝"
            "（来源：本轮多任务范围校验，用于证明系统未执行失控查询）"
        )

    if item.kind == "REPORT_DATASET_MANIFEST":
        section_count = payload.get("section_count")
        detail = f"已登记 {section_count} 个报表分段" if isinstance(section_count, int) else "已登记报表数据分段"
        return (
            f"报表数据证据：{detail}"
            "（来源：本轮各子任务的数据集清单，用于追溯报表内容）"
        )

    return (
        "执行过程证据：已记录本轮受控处理步骤"
        "（来源：本轮受控执行链，用于保留可审计的处理依据）"
    )


def _warning_reason(warning: str) -> str:
    text = warning.casefold()
    if "数据质量" in text:
        return "上游数据质量没有达到通过状态，结果需要复核"
    if "水位" in text or "覆盖" in text:
        return "请求时间范围与当前业务数据更新时间不完全一致"
    if "样本" in text or "行有效数据" in text or "数据不足" in text:
        return "返回数据未满足当前分析方法所需的最低样本条件"
    if "长期" in text and ("偏好" in text or "记忆" in text):
        return "可选的用户偏好服务不可用，本轮仅使用当前问题和短期会话"
    if "公开网络" in text or "联网" in text:
        return "公开信息没有经过业务数据库口径校验，只能作为补充参考"
    if "关联" in text or "匹配率" in text:
        return "多来源数据的关联覆盖率未达到可靠性要求"
    if "快照" in text or "时效" in text:
        return "参与计算的数据快照时间不一致，可能影响时效性"
    if "聚合" in text or "全量" in text:
        return "上游只提供了明细或预览，缺少可核验的完整聚合统计"
    return "可靠性校验发现该限制，可能影响结果的完整性或适用范围"


def _warning_text_zh(warning: str) -> str:
    text = _compact_text(warning, limit=500)
    for status, label in sorted(_QUALITY_LABELS.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"\b{re.escape(status)}\b", label, text, flags=re.IGNORECASE)
    return text.replace("SQL服务", "查询服务").replace("Qwen", "模型")


def _warning_line(warning: str) -> str:
    text = _warning_text_zh(warning).rstrip("。；; ")
    return f"{text}（产生原因：{_warning_reason(text)}）"


def render_reliability_validation(
    reliability: ReliabilityReport,
    evidence: list[EvidenceItem],
    quality_status: str,
) -> str:
    """Render the complete public validation block in Chinese."""

    lines = [
        f"校验结论：{reliability_level_label_zh(reliability.level)}（{reliability.score:.2f}）。",
        f"数据质量：{quality_status_label_zh(quality_status)}。",
        f"证据（{len(evidence)}项）：",
    ]
    if evidence:
        lines.extend(
            f"{index}. {_evidence_line(item)}"
            for index, item in enumerate(evidence, 1)
        )
    else:
        lines.append("无（来源：本轮未形成可展示的核验证据）")

    if reliability.warnings:
        lines.append(f"告警（{len(reliability.warnings)}项）：")
        lines.extend(
            f"{index}. {_warning_line(warning)}"
            for index, warning in enumerate(reliability.warnings, 1)
        )
    else:
        lines.append("告警：无。")

    lines.append(
        "结果通过可靠性门禁。"
        if reliability.level != "FAIL"
        else "结果未通过可靠性门禁，不输出未经验证的数值。"
    )
    return "\n".join(lines)
