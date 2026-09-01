from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.domain.models import PrimaryIntent, StrictModel


class SkillCapability(StrictModel):
    name: str
    category: Literal["QUERY", "ANALYSIS", "METADATA", "DATASET", "FILE", "OUTPUT"]
    maturity: Literal["IMPLEMENTED", "PARTIAL", "PLANNED"]
    runtime_mode: str
    intents: list[PrimaryIntent] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    runtime_available: bool = False
    unavailable_reason: str | None = None


_SKILLS: tuple[SkillCapability, ...] = (
    SkillCapability(name="metric_query", category="QUERY", maturity="IMPLEMENTED", runtime_mode="ORCHESTRATOR_PIPELINE", intents=[PrimaryIntent.METRIC_QUERY]),
    SkillCapability(name="detail_query", category="QUERY", maturity="IMPLEMENTED", runtime_mode="ORCHESTRATOR_PIPELINE", intents=[PrimaryIntent.DETAIL_QUERY], limitations=["脱敏和行级权限依赖上游查询服务"]),
    SkillCapability(name="ranking_query", category="QUERY", maturity="IMPLEMENTED", runtime_mode="ASL_SQL_PLUS_DETERMINISTIC_VALIDATION", limitations=["TOP/BOTTOM-N作为通用算子而非独立主意图；覆盖率排名强制校验分子、统一分母、比率和排序"]),
    SkillCapability(name="metadata_definition", category="METADATA", maturity="IMPLEMENTED", runtime_mode="ORCHESTRATOR_PIPELINE", intents=[PrimaryIntent.METRIC_DEFINITION]),
    SkillCapability(name="data_lineage", category="METADATA", maturity="IMPLEMENTED", runtime_mode="ORCHESTRATOR_PIPELINE", intents=[PrimaryIntent.DATA_LINEAGE]),
    SkillCapability(name="trend_analysis", category="ANALYSIS", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.TREND_ANALYSIS]),
    SkillCapability(name="comparison_analysis", category="ANALYSIS", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.COMPARISON_ANALYSIS]),
    SkillCapability(name="composition_analysis", category="ANALYSIS", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.COMPOSITION_ANALYSIS]),
    SkillCapability(name="anomaly_analysis", category="ANALYSIS", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.ANOMALY_ANALYSIS]),
    SkillCapability(name="root_cause_analysis", category="ANALYSIS", maturity="PARTIAL", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.ROOT_CAUSE_ANALYSIS], limitations=["要求SQL返回维度贡献值；不能仅凭相关性认定因果"]),
    SkillCapability(name="forecast_analysis", category="ANALYSIS", maturity="PARTIAL", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.FORECAST_ANALYSIS], limitations=["当前为单步、多候选模型滚动回测；尚未接独立算法服务"]),
    SkillCapability(name="data_quality_analysis", category="ANALYSIS", maturity="PARTIAL", runtime_mode="DETERMINISTIC_ENGINE", intents=[PrimaryIntent.DATA_QUALITY], limitations=["已覆盖空值、重复值、字段类型、唯一值比例和数值分布；业务规则校验仍依赖语义层"]),
    SkillCapability(name="report_generation", category="OUTPUT", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_XLSX_DOCX_PDF_EXPORT", intents=[PrimaryIntent.REPORT_GENERATION], limitations=["Word/PDF 表格预览最多 200 行；完整明细使用 Excel"]),
    SkillCapability(name="dataset_follow_up", category="DATASET", maturity="PARTIAL", runtime_mode="MINIO_CONTROLLED_TRANSFORM", limitations=["支持筛选、排序、TopN、聚合、选列和派生列；复杂追问重新查询SQL"]),
    SkillCapability(name="multi_question_analysis", category="ANALYSIS", maturity="IMPLEMENTED", runtime_mode="CHECKPOINTED_VALIDATED_TASK_DAG", limitations=["单轮最多5个原子任务；同构查询按指纹复用；独立任务并行，依赖任务分层执行；Redis检查点支持服务重启续跑"]),
    SkillCapability(name="cross_dataset_analysis", category="DATASET", maturity="IMPLEMENTED", runtime_mode="MINIO_VALIDATED_INNER_JOIN", limitations=["当前仅允许明确且唯一的公共关联键；拒绝右表关联键重复导致的多对多数据膨胀；最多联合5个分支数据集"]),
    SkillCapability(name="excel_query", category="FILE", maturity="IMPLEMENTED", runtime_mode="MINIO_SPREADSHEET_DATASET", limitations=["支持 CSV/XLSX；复杂公式只读取 Excel 缓存结果"]),
    SkillCapability(name="excel_multi_sheet_analysis", category="FILE", maturity="IMPLEMENTED", runtime_mode="MULTI_SHEET_UNION_AND_FILTER", limitations=["支持按 Sheet 选择和跨 Sheet 联合分析；自动 Join 仍要求明确关联键"]),
    SkillCapability(name="database_excel_compare", category="FILE", maturity="PLANNED", runtime_mode="NOT_IMPLEMENTED", limitations=["缺少跨来源字段映射和快照校验"]),
    SkillCapability(name="chart_generation", category="OUTPUT", maturity="IMPLEMENTED", runtime_mode="DETERMINISTIC_CHART_SPEC", limitations=["当前返回通用ChartSpec，由前端使用ECharts或AntV渲染；不直接生成图片文件"]),
    SkillCapability(name="insight_generation", category="OUTPUT", maturity="IMPLEMENTED", runtime_mode="EVIDENCE_GROUNDED_QWEN", limitations=["Qwen只负责表达；失败时返回确定性算法结果"]),
)


_INTENT_TO_SKILL = {
    intent: skill.name
    for skill in _SKILLS
    for intent in skill.intents
}


def skill_for_intent(intent: PrimaryIntent) -> str | None:
    return _INTENT_TO_SKILL.get(intent)


def list_skill_capabilities(*, adapter_mode: str, minio_enabled: bool) -> list[SkillCapability]:
    result: list[SkillCapability] = []
    for definition in _SKILLS:
        available = definition.maturity != "PLANNED"
        reason: str | None = None
        if definition.category in {"QUERY", "ANALYSIS", "METADATA"} and adapter_mode == "mock":
            available = False
            reason = "当前运行配置使用mock数据，只可演示，未连接真实ASL/SQL/知识服务"
        if definition.name in {
            "dataset_follow_up", "excel_query", "excel_multi_sheet_analysis", "report_generation"
        } and not minio_enabled:
            available = False
            reason = "MinIO数据集能力未启用"
        if definition.maturity == "PLANNED":
            reason = "尚未实现"
        result.append(
            definition.model_copy(
                update={"runtime_available": available, "unavailable_reason": reason}
            )
        )
    return result
