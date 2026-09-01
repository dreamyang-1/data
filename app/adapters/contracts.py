"""外部服务的端点契约定义。

每个外部服务的请求/响应 Schema 用 Pydantic 模型定义，既是外部团队对接的
契约文档（字段、类型、必填性清晰可见），也被 http.py 适配器用于运行时
响应校验。契约变更必须在此文件显式修改，并通过 test_contracts.py 验证。

## 服务清单

| 服务 | 基址配置项 | 端点 |
|------|-----------|------|
| Semantic (Oagnet) | semantic_base_url | POST /v1/metrics:resolve, GET /v1/metrics/{id}/definition, POST /v1/metrics/{id}/lineage |
| Policy | policy_base_url | POST /v1/authorize |
| Query (NL2SQL) | query_base_url | POST /v1/query:compile, POST /v1/query:execute |
| Knowledge | knowledge_base_url | POST /v1/knowledge:verify |

## 错误响应

所有端点在失败时返回统一错误结构（见 `ErrorPayload`），HTTP 状态码遵循：
- 400: 请求 Schema 不合法
- 401/403: 身份未授权
- 404: 资源不存在（如 metric_id 未找到）
- 409: 冲突（如版本不一致）
- 422: 业务规则不满足（如指标口径不唯一）
- 5xx: 服务端故障
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class StrictContract(BaseModel):
    """契约基类：拒绝未知字段，保证响应与契约严格一致。"""

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# 通用错误响应
# ---------------------------------------------------------------------------


class ErrorPayload(StrictContract):
    """所有端点失败时返回的统一错误结构。"""

    error_code: str = Field(description="稳定错误码，如 METRIC_NOT_FOUND、FORBIDDEN")
    message: str = Field(description="人类可读的错误描述")
    retryable: bool = Field(default=False, description="调用方是否可重试")
    details: dict[str, Any] | None = Field(default=None, description="可选的附加上下文")


# ---------------------------------------------------------------------------
# Semantic Service (Oagnet) 契约
# ---------------------------------------------------------------------------


class ResolveMetricsRequest(StrictContract):
    """POST /v1/metrics:resolve — 把用户输入的指标名解析为权威指标引用。"""

    metrics: list[str] = Field(description="用户输入的指标名或别名列表")
    semantic_model_id: int | None = Field(
        default=None, description="语义模型 ID；服务端不应信任客户端传入，需二次校验"
    )
    tenant_id: str = Field(description="租户 ID，用于指标可见性过滤")


class ResolvedMetric(StrictContract):
    """单个指标的解析结果。"""

    input: str = Field(description="原始输入")
    metric_id: str = Field(description="权威指标主键，如 metric.sales_amount")
    version: str = Field(description="指标版本，如 v1")
    canonical_name: str = Field(description="标准名称")
    unit: str = Field(description="单位，如 元、%")


class ResolveMetricsResponse(StrictContract):
    """POST /v1/metrics:resolve 的响应。"""

    metrics: list[ResolvedMetric] = Field(description="解析结果，顺序与请求一致；未命中项不包含")


class MetricDefinitionResponse(StrictContract):
    """GET /v1/metrics/{id}/definition?version={version} 的响应。"""

    metric_id: str
    version: str
    name: str
    definition: str = Field(description="指标口径文本")
    formula: str | None = Field(default=None, description="计算公式")
    unit: str
    dimensions: list[str] = Field(default_factory=list, description="可用维度")
    applicable_filters: list[str] = Field(default_factory=list, description="可用过滤条件")
    valid_from: datetime | None = Field(default=None)
    valid_to: datetime | None = Field(default=None)


class LineageRequest(StrictContract):
    """POST /v1/metrics/{id}/lineage 的请求体。"""

    tenant_id: str
    user_id: str


class LineageResponse(StrictContract):
    """POST /v1/metrics/{id}/lineage 的响应。"""

    metric_id: str
    version: str
    business_lineage: list[str] = Field(description="业务域血缘链路")
    physical_sources: list[dict[str, Any]] | None = Field(
        default=None,
        description="物理表/字段/任务血缘；可能受更严格权限限制而省略",
    )
    lineage_version: str | None = None
    entity: dict[str, Any] | None = None
    source_tables: list[str] = Field(default_factory=list)
    source_fields: list[str] = Field(default_factory=list)
    dependency_metrics: list[dict[str, Any]] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    upstream_edges: list[dict[str, Any]] = Field(default_factory=list)
    downstream_edges: list[dict[str, Any]] = Field(default_factory=list)
    column_lineage: list[dict[str, Any]] = Field(default_factory=list)
    metadata_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Policy Service 契约
# ---------------------------------------------------------------------------


class AuthorizeRequest(StrictContract):
    """POST /v1/authorize — 行列权限与查询预算校验。"""

    intent: str = Field(description="主意图枚举值，如 METRIC_QUERY")
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)
    metrics: list[dict[str, str]] = Field(
        default_factory=list,
        description="指标引用列表，每项含 metric_id 与 version",
    )
    entity: str | None = Field(default=None, description="明细查询的实体，如 订单")
    fields: list[str] = Field(default_factory=list, description="请求的明细字段")
    risk_level: str = Field(default="LOW", description="风险等级 LOW/MEDIUM/HIGH")


class AuthorizeResponse(StrictContract):
    """POST /v1/authorize 的响应。"""

    allowed: bool
    policy_ref: str = Field(description="策略版本引用，用于审计")
    denied_reason: str | None = Field(default=None, description="拒绝时的原因码")
    row_policy: dict[str, Any] | None = Field(
        default=None,
        description="行级过滤条件，由查询服务在 SQL 层应用",
    )
    field_masking: list[dict[str, str]] | None = Field(
        default=None,
        description="字段脱敏规则",
    )
    query_budget: dict[str, Any] | None = Field(
        default=None,
        description="查询预算：最大行数、超时、成本上限",
    )


# ---------------------------------------------------------------------------
# Query Service (加固后的 NL2SQL) 契约
# ---------------------------------------------------------------------------


class CompileQueryRequest(StrictContract):
    """POST /v1/query:compile — 把意图规格编译为可执行的 QuerySpec。"""

    intent: str
    metric_ids: list[str] = Field(default_factory=list)
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class CompileQueryResponse(StrictContract):
    """POST /v1/query:compile 的响应，结构与 QuerySpec 一致。"""

    schema_version: str = "1.0"
    metric_ids: list[str] = Field(default_factory=list)
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None
    snapshot_requirement: str = "CONSISTENT"
    sql_candidate: str | None = Field(
        default=None,
        description="生成的 SQL 候选；服务端可选返回，用于审计而非执行",
    )


class ExecuteQueryRequest(StrictContract):
    """POST /v1/query:execute — 执行已编译的 QuerySpec。"""

    schema_version: str = "1.0"
    metric_ids: list[str] = Field(default_factory=list)
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None
    snapshot_requirement: str = "CONSISTENT"


class ExecuteQueryResponse(StrictContract):
    """POST /v1/query:execute 的响应，结构与 Dataset 一致。"""

    columns: list[str]
    rows: list[dict[str, Any]]
    snapshot_id: str
    data_as_of: datetime
    quality_status: str = "PASS"
    next_cursor: str | None = Field(default=None, description="分页游标")


# ---------------------------------------------------------------------------
# Knowledge Service 契约
# ---------------------------------------------------------------------------


class VerifyMetricRequest(StrictContract):
    """POST /v1/knowledge:verify — 校验指标口径与业务定义一致。"""

    metric_id: str
    version: str


class VerifyMetricResponse(StrictContract):
    """POST /v1/knowledge:verify 的响应。"""

    metric_id: str
    version: str
    verified: bool = Field(description="是否通过口径校验")
    caliber_text: str = Field(default="", description="知识库中的口径文本")
    conflicts: list[str] | None = Field(
        default=None,
        description="与指标中心定义的冲突点；为空表示一致",
    )


# ---------------------------------------------------------------------------
# Analysis Service 契约
# ---------------------------------------------------------------------------


class AnalysisRequest(StrictContract):
    """POST /v1/analysis:run — 高级分析统一入口。

    服务端按 analysis_type 分派到对应执行器：
      - TREND_ANALYSIS：基于时间分桶的趋势分析
      - COMPARISON_ANALYSIS：同比/环比/目标值/对象间对比
      - COMPOSITION_ANALYSIS：按维度的构成占比
      - ANOMALY_ANALYSIS：时序异常点检测
      - ROOT_CAUSE_ANALYSIS：异常归因分析
      - FORECAST_ANALYSIS：时序预测
      - REPORT_GENERATION：多节自动报告
      - DATA_QUALITY：数据质量评分
    """

    analysis_type: str = Field(description="PrimaryIntent 枚举值")
    tenant_id: str
    user_id: str
    metric_ids: list[str] = Field(default_factory=list)
    entity: str | None = Field(default=None)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    time_range: dict[str, Any] | None = None
    comparison_type: str | None = Field(
        default=None,
        description="对比类型：YOY/QOQ/TARGET/OBJECT，仅 COMPARISON_ANALYSIS 使用",
    )
    forecast_horizon: str | None = Field(
        default=None,
        description="预测时长，如 P30D（30天），仅 FORECAST_ANALYSIS 使用",
    )
    report_template_id: str | None = Field(
        default=None,
        description="报告模板 ID，仅 REPORT_GENERATION 使用",
    )
    operators: list[str] = Field(
        default_factory=list, description="用户显式声明的算子，如 TIME_BUCKET/DECOMPOSE"
    )


class AnalysisResponse(StrictContract):
    """POST /v1/analysis:run 的响应，结构与 AnalysisResult 一致。"""

    analysis_type: str
    summary: str
    insights: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.8, ge=0, le=1)
    caveats: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    data_as_of: datetime


# ---------------------------------------------------------------------------
# 契约注册表（供 Capability Handshake 与文档生成使用）
# ---------------------------------------------------------------------------


CONTRACTS: dict[str, dict[str, type[BaseModel]]] = {
    "semantic": {
        "POST /v1/metrics:resolve": ResolveMetricsResponse,
        "GET /v1/metrics/{id}/definition": MetricDefinitionResponse,
        "POST /v1/metrics/{id}/lineage": LineageResponse,
    },
    "policy": {
        "POST /v1/authorize": AuthorizeResponse,
    },
    "query": {
        "POST /v1/query:compile": CompileQueryResponse,
        "POST /v1/query:execute": ExecuteQueryResponse,
    },
    "knowledge": {
        "POST /v1/knowledge:verify": VerifyMetricResponse,
    },
    "analysis": {
        "POST /v1/analysis:run": AnalysisResponse,
    },
}
