from __future__ import annotations

import json
import logging
import re
import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ConversationControl,
    IntentCandidate,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent.classifier import (
    RuleBasedIntentClassifier,
    render_execution_question,
    safe_semantic_confirmation,
)


logger = logging.getLogger(__name__)


class StructuredIntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_intent: PrimaryIntent
    secondary_intents: list[PrimaryIntent] = Field(default_factory=list)
    operators: list[AnalysisOperator] = Field(default_factory=list)
    conversation_control: ConversationControl = ConversationControl.NEW_REQUEST
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    current_entity_values: list[str] = Field(default_factory=list, max_length=20)
    comparison_type: str | None = None
    ambiguities: list[str] = Field(default_factory=list)
    completed_question: str | None = Field(default=None, max_length=4000)


SYSTEM_PROMPT = """你是企业数据分析系统的意图分类器，只分类和抽取，不回答问题。输出必须是符合给定字段定义的 JSON 对象，不得包含 Markdown 或额外文字。
必须遵守：
1. 只能使用 Schema 中给定的枚举，不创造意图。
2. TREND_ANALYSIS 只描述历史；明确未来时间或预测表达才是 FORECAST_ANALYSIS。
3. 排名、过滤、分组是 operators，不是主意图。
4. 用户最终交付物是主意图；其他目标放 secondary_intents。
5. 否定表达必须按用户真正需要分类，例如“不要预测，只看趋势”是 TREND_ANALYSIS。
6. 不确定时降低 confidence 并写入 ambiguities，绝不猜测指标。
7. evidence 只填写用户原话中的短语，不输出推理过程。
8. “下月计划值/预算值/目标值”是已存在数据查询，不是预测；只有要求推算未知未来结果才是 FORECAST_ANALYSIS。
9. 同时包含多个诉求时，最终交付物作为 primary_intent，其余放 secondary_intents；例如“分析下降原因并生成报告”主意图为 REPORT_GENERATION、次意图为 ROOT_CAUSE_ANALYSIS。
10. 指标名称优先保持用户原话中的完整业务度量，不得改写成臆造的标准指标编码，也不得只截取“销售、订单、业务、数据、金额、数量、趋势”等泛化名词充当指标。
11. 只查询一个时间段的汇总数值是 METRIC_QUERY；出现“最近30天、某月、某季度、某日”本身不代表趋势。只有要求走势、升降、按时间观察变化才是 TREND_ANALYSIS。
12. 预测必须要求推算尚未发生的结果。明确的历史日期、月份、季度，即使带年份，也不能分类为 FORECAST_ANALYSIS。
13. 同比、环比、同期比、较上期、增长率属于 COMPARISON_ANALYSIS，不能归为普通指标查询。
14. 询问来源表、来源字段、加工链路属于 DATA_LINEAGE；询问指标含义、公式、统计范围属于 METRIC_DEFINITION。
15. 询问刷新频率、延迟、缺失、重复、空值或跨系统对账属于 DATA_QUALITY。
16. 不要因为句子中出现“报表”就判为 REPORT_GENERATION；“报表中的指标来源/对账”仍分别属于 DATA_LINEAGE/DATA_QUALITY。
17. completed_question 必须把当前问题补全成一条可以独立理解和执行的业务问题；如果输入中包含“已确认的上一轮上下文”，只继承当前问题省略的内容，当前问题明确表达的实体、指标、时间、筛选和排序永远优先。
18. completed_question 不得回答问题、不得生成 SQL、不得添加输入及已确认上下文中不存在的业务值；独立完整问题只做必要规范化，不得擅自引用上一轮。
19. entity 表示用户本轮要查询、分组或返回的业务对象（如产品、经销商、医院），不得把作为筛选值的具体产品名直接当成 entity；dimensions 和 fields 必须按用户实际要求抽取。
20. 实体、维度、字段和指标都要由语义理解给出；规则或关键词只能作为证据，不能因为句式常见就省略抽取。
21. “销售趋势、销售走势、销售变化”是已登记的业务省略表达：用户未明确“销量、销售量、销售数量”时，metrics 填“销售额”，completed_question 补成“销售额趋势/走势/变化”；用户明确数量口径时必须填“销售量”。“订单趋势、业务趋势”等没有已登记默认口径的泛化表达仍需指出歧义，不得照抄“订单、业务”作为指标。
22. metrics 中的每一项都必须能独立表示可计算度量；completed_question 中的指标口径必须与 metrics 一致。
23. current_entity_values 只提取当前用户问题（“已确认的上一轮上下文”之前）明确出现的具体业务实体值，例如产品名、品牌名、厂家名、经销商名、医院名或地区名；不得填写“产品、经销商、医院”等对象类别，不得复制只存在于上一轮上下文中的值，也不得包含“那、呢、换成”等语气或操作词。例如“那费森尤斯呢”必须提取为 ["费森尤斯"]。
24. 当前问题出现新的实体值时，completed_question 必须用新值替换上一轮同一筛选槽，不得同时保留冲突旧值，也不得把新实体值臆造成查询对象或指标。
"""


class StructuredIntentModelClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    async def classify(self, question: str) -> StructuredIntentOutput:
        if not self.settings.intent_model_api_key:
            raise RuntimeError("intent model API key is not configured")
        schema = StructuredIntentOutput.model_json_schema()
        response_format: dict[str, Any]
        if self.settings.intent_model_response_format == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "data_analysis_intent",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        schema_instruction = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        business_today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        body = {
            "model": self.settings.intent_model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{SYSTEM_PROMPT}\n"
                        f"当前业务日期（Asia/Shanghai）是 {business_today}。早于该日期的明确时间是历史，不是预测。\n"
                        f"必须严格遵守以下 JSON Schema：{schema_instruction}"
                    ),
                },
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "enable_thinking": self.settings.intent_model_enable_thinking,
            "response_format": response_format,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.intent_model_api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=self.settings.intent_model_base_url.rstrip("/"),
            timeout=self.settings.intent_model_timeout_seconds,
            transport=self._transport,
        ) as client:
            for attempt in range(self.settings.intent_model_max_retries + 1):
                try:
                    response = await client.post("/chat/completions", headers=headers, json=body)
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt >= self.settings.intent_model_max_retries:
                        raise
                    await asyncio.sleep(0.2 * (2**attempt))
                except httpx.HTTPStatusError as exc:
                    retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                    if not retryable or attempt >= self.settings.intent_model_max_retries:
                        raise
                    await asyncio.sleep(0.2 * (2**attempt))
        content = payload["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("intent model returned empty content")
        return StructuredIntentOutput.model_validate(json.loads(content))


class HybridIntentClassifier:
    """Rules provide a safe baseline; structured model enriches it under deterministic gates."""

    _generic_metric_nouns = frozenset({
        "销售", "订单", "业务", "经营", "业绩", "数据", "指标", "度量",
        "金额", "数量", "数值", "情况", "表现", "趋势", "变化",
    })

    def __init__(
        self,
        settings: Settings,
        rules: RuleBasedIntentClassifier | None = None,
        model_client: StructuredIntentModelClient | None = None,
    ) -> None:
        self.settings = settings
        self.rules = rules or RuleBasedIntentClassifier()
        self.model_client = model_client or StructuredIntentModelClient(settings)

    async def classify(
        self, question: str, identity: TrustedIdentity, conversation_id: str
    ) -> CanonicalAnalysisRequest:
        request = self.rules.classify(question, identity, conversation_id)
        request.intent_candidates = [
            IntentCandidate(intent=request.primary_intent, confidence=0.65, evidence=[])
        ]
        if not self.settings.intent_model_enabled:
            return request
        if self._should_skip_model(request, question):
            request.intent_source = "RULE"
            request.intent_confidence = 0.95
            request.intent_candidates[0].confidence = 0.95
            request.assumptions.append("STRONG_RULE_MODEL_SKIPPED")
            return request
        try:
            model = await self.model_client.classify(question)
        except (httpx.HTTPError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            logger.warning("structured intent model unavailable; using rule baseline: %s", type(exc).__name__)
            request.assumptions.append("STRUCTURED_MODEL_UNAVAILABLE_RULE_FALLBACK")
            return request

        request.intent_candidates.insert(
            0,
            IntentCandidate(
                intent=model.primary_intent,
                confidence=model.confidence,
                evidence=model.evidence,
            ),
        )
        if model.confidence < self.settings.intent_model_min_confidence:
            request.assumptions.append("LOW_MODEL_CONFIDENCE_RULE_FALLBACK")
            return request
        if not self._passes_deterministic_constraints(model, question):
            request.assumptions.append("MODEL_INTENT_FAILED_CONSTRAINT_RULE_FALLBACK")
            return request
        if (
            model.primary_intent != request.primary_intent
            and self._has_strong_rule_signal(request.primary_intent, question)
        ):
            request.assumptions.append("MODEL_CONFLICT_STRONG_RULE_FALLBACK")
            return request

        deterministic_control = request.conversation_control
        request.primary_intent = model.primary_intent
        request.secondary_intents = model.secondary_intents
        request.conversation_control = (
            deterministic_control
            if deterministic_control != ConversationControl.NEW_REQUEST
            else model.conversation_control
        )
        request.operators = model.operators
        request.intent_source = "STRUCTURED_MODEL"
        request.intent_confidence = model.confidence
        semantic_extraction_applied = False
        current_entity_candidates = list(model.current_entity_values)
        current_fragment = question.split(
            "\n已确认的上一轮上下文", 1
        )[0].strip()
        if (
            not current_entity_candidates
            and re.fullmatch(
                r"(?:那|那么|再看|换成|改成)?[^，,。；;？?]{1,100}"
                r"(?:呢|怎么样)[。！!？?]*",
                re.sub(r"\s+", "", current_fragment),
            )
        ):
            # Some compatible models put the current value in ``evidence``
            # despite the dedicated schema field.  Evidence is still model
            # extraction, but it is accepted only for a tightly-scoped
            # elliptical turn and must pass the same literal grounding gate.
            current_entity_candidates = list(model.evidence)
            if current_entity_candidates:
                request.assumptions.append(
                    "CURRENT_ENTITY_VALUE_RECOVERED_FROM_MODEL_EVIDENCE"
                )
        if current_entity_candidates:
            current_entity_values = self._grounded_current_entity_values(
                current_entity_candidates,
                question,
            )
            if current_entity_values:
                request.semantic_entity_mentions = current_entity_values
                semantic_extraction_applied = True
            if len(current_entity_values) != len(current_entity_candidates):
                request.assumptions.append(
                    "UNGROUNDED_CURRENT_ENTITY_VALUE_DROPPED"
                )
        if model.metrics:
            grounded_metrics = self._grounded_metric_names(model.metrics, question)
            if grounded_metrics:
                request.metrics = [MetricRef(input=name) for name in grounded_metrics]
            if len(grounded_metrics) != len(model.metrics):
                request.assumptions.append("UNGROUNDED_MODEL_METRIC_DROPPED")
            if any(
                self._normalize_metric_text(name) in self._generic_metric_nouns
                for name in model.metrics
            ):
                request.assumptions.append("GENERIC_MODEL_METRIC_DROPPED")
        if model.dimensions:
            grounded_dimensions = self._grounded_text_values(model.dimensions, question)
            grounded_dimensions.extend(
                value
                for value in model.dimensions
                if value not in grounded_dimensions
                and self._supported_entity_category(value, question, request)
            )
            grounded_dimensions = list(dict.fromkeys(grounded_dimensions))
            if grounded_dimensions:
                request.dimensions = grounded_dimensions
                semantic_extraction_applied = True
            if len(grounded_dimensions) != len(model.dimensions):
                request.assumptions.append("UNGROUNDED_MODEL_DIMENSION_DROPPED")
        if model.entity:
            grounded_entity = self._grounded_text_value(model.entity, question)
            if grounded_entity is None and self._supported_entity_category(
                model.entity, question, request
            ):
                grounded_entity = model.entity.strip()
            if grounded_entity:
                request.entity = grounded_entity
                semantic_extraction_applied = True
            else:
                request.assumptions.append("UNGROUNDED_MODEL_ENTITY_DROPPED")
        if model.fields:
            grounded_fields = self._grounded_text_values(model.fields, question)
            if grounded_fields:
                request.fields = grounded_fields
                semantic_extraction_applied = True
            if len(grounded_fields) != len(model.fields):
                request.assumptions.append("UNGROUNDED_MODEL_FIELD_DROPPED")
        if model.comparison_type:
            grounded_comparison = self._grounded_comparison_type(
                model.comparison_type, question
            )
            if grounded_comparison:
                request.comparison_type = grounded_comparison
            else:
                request.assumptions.append("UNGROUNDED_MODEL_COMPARISON_DROPPED")
        if semantic_extraction_applied:
            request.assumptions.append("MODEL_ENTITY_EXTRACTION_APPLIED")
        if model.completed_question:
            completed_question = self._safe_completed_question(
                model.completed_question,
                question,
            )
            if completed_question is not None:
                request.rewritten_question = completed_question
                request.assumptions.append("MODEL_QUESTION_COMPLETION_APPLIED")
            else:
                request.assumptions.append("UNSAFE_MODEL_QUESTION_COMPLETION_DROPPED")
        # Re-apply deterministic result-shape rules after model enrichment.
        # The structured model may otherwise downgrade a supplier list to a
        # metric query or treat recommendation wording as out of scope.
        self.rules.apply_business_query_shapes(request, question)
        request.risk_level = (
            "HIGH"
            if request.primary_intent == PrimaryIntent.DETAIL_QUERY
            else "MEDIUM"
        )
        request.missing_slots = self.rules.required_missing_slots(request)
        # Model ambiguities are advisory and are only safe after the complete model
        # result has passed every deterministic gate.  Keep only ambiguities that
        # describe a slot which is still actually missing; otherwise stale or
        # speculative model text can make the assistant ask for information that
        # the deterministic extractors already found.
        request.ambiguities = self._filter_ambiguities(
            model.ambiguities, request.missing_slots
        )
        return request

    def merge_clarification(
        self, pending: CanonicalAnalysisRequest, answer: str
    ) -> CanonicalAnalysisRequest:
        before = pending.model_copy(deep=True)
        prior_missing = set(pending.missing_slots)
        merged = self.rules.merge_clarification(pending, answer)

        # A normal clarification is a patch for the requested missing slots, not
        # permission to silently replace already confirmed values. Explicit
        # correction/follow-up wording may intentionally change an existing slot.
        allow_existing_slot_changes = merged.conversation_control in {
            ConversationControl.CORRECTION,
            ConversationControl.FOLLOW_UP,
        }
        explicit_relationship_detail = bool(re.search(
            r"(?:它|该产品|这个产品|这些产品)?.{0,16}(?:卖给|销售给)(?:了)?哪些医院"
            r"|哪些医院.{0,16}(?:购买|采购|使用)(?:了)?(?:它|该产品|这个产品)"
            r"|(?:这些|上述|前述)?医院.{0,16}(?:什么|哪些|分别是)?(?:等级|级别)",
            re.sub(r"\s+", "", answer),
        ))
        allow_existing_slot_changes = (
            allow_existing_slot_changes or explicit_relationship_detail
        )
        if not allow_existing_slot_changes:
            if "metric" not in prior_missing:
                merged.metrics = before.metrics
            if (
                "time_range" not in prior_missing
                and not (
                    "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in before.assumptions
                    and merged.time_range != before.time_range
                )
            ):
                merged.time_range = before.time_range
            if "entity" not in prior_missing:
                merged.entity = before.entity
            explicit_field_change = bool(re.search(
                r"(?:只|仅)(?:保留|显示|返回|要|看)(?:字段|列)?"
                r"|(?:再|还|并|同时|另外)(?:加上?|添加|附带|显示|返回)(?:字段|列)?",
                re.sub(r"\s+", "", answer),
            ))
            if "fields" not in prior_missing and not explicit_field_change:
                merged.fields = before.fields
            if "dimension" not in prior_missing:
                merged.dimensions = before.dimensions
            if "comparison_type" not in prior_missing:
                merged.comparison_type = before.comparison_type

        if "comparison_type" in prior_missing:
            comparison = self._comparison_candidate(answer)
            if comparison:
                merged.comparison_type = comparison
        if "dimension" in prior_missing:
            dimensions = self._dimension_candidates(answer)
            if dimensions:
                merged.dimensions = dimensions
        if "fields" in prior_missing:
            fields = self._field_candidates(answer)
            if fields:
                merged.fields = fields
        if "entity" in prior_missing and not merged.entity:
            entity = self._entity_candidate(answer)
            if entity:
                merged.entity = entity

        # The rule baseline intentionally knows only a small built-in metric list.
        # During an explicit metric clarification, preserve a plausible custom
        # metric name as a candidate so that the semantic service can resolve and
        # verify it later.  This does not make the metric trusted or executable.
        if "metric" in prior_missing:
            time_was_resolved = (
                merged.time_range is not None
                and (
                    merged.time_range != before.time_range
                    or (
                        "time_range" in prior_missing
                        and "time_range" not in merged.missing_slots
                    )
                )
            )
            metric_text = self._correction_value(answer) or answer
            candidates = self._custom_metric_candidates(
                metric_text,
                prior_missing,
                strip_temporal_context=time_was_resolved,
            )
            if candidates:
                merged.metrics = [MetricRef(input=value) for value in candidates]
                merged.assumptions.append("CUSTOM_METRIC_CANDIDATE_FROM_CLARIFICATION")
            elif merged.conversation_control == ConversationControl.CORRECTION:
                # A correction that names no valid replacement metric must not
                # retain a negated old metric extracted from “不是销售额”.
                merged.metrics = []

        if merged.conversation_control == ConversationControl.CORRECTION:
            corrected_text = self._correction_value(answer)
            if corrected_text:
                known = [
                    name
                    for name in self.rules._known_metrics
                    if name in corrected_text
                ]
                candidates = known or self._custom_metric_candidates(
                    corrected_text,
                    {"metric"},
                    strip_temporal_context=False,
                )
                if candidates:
                    merged.metrics = [MetricRef(input=value) for value in candidates]

        merged.missing_slots = self.rules.required_missing_slots(merged)
        merged.ambiguities = self._filter_ambiguities(
            merged.ambiguities, merged.missing_slots
        )
        represents_structured_change = bool(
            merged.metrics != before.metrics
            or merged.time_range != before.time_range
            or merged.entity != before.entity
            or merged.fields != before.fields
            or merged.dimensions != before.dimensions
            or merged.filters != before.filters
            or merged.comparison_type != before.comparison_type
            or merged.ranking_limit != before.ranking_limit
            or merged.forecast_horizon_periods != before.forecast_horizon_periods
        )
        confirmation = (
            safe_semantic_confirmation(answer)
            if "semantic_ambiguity" in prior_missing
            and not represents_structured_change
            and merged.conversation_control == ConversationControl.CLARIFICATION_RESPONSE
            else None
        )
        if (
            "semantic_ambiguity" in prior_missing
            and not represents_structured_change
            and confirmation is None
            and "semantic_ambiguity" not in merged.missing_slots
        ):
            merged.missing_slots.append("semantic_ambiguity")
        # Rule merging already canonicalizes the executable question. Render
        # once more because the structured layer may have grounded a custom
        # metric or an explicit correction after the rule pass.
        merged.rewritten_question = render_execution_question(
            merged, confirmation=confirmation
        )
        return merged

    @classmethod
    def _custom_metric_candidates(
        cls,
        answer: str,
        prior_missing: set[str],
        *,
        strip_temporal_context: bool,
    ) -> list[str]:
        candidate = cls._custom_metric_candidate(
            answer,
            prior_missing,
            strip_temporal_context=strip_temporal_context,
        )
        if not candidate:
            return []
        parts = [
            item.strip("，,。.!！?？;；：:的")
            for item in re.split(r"(?:、|,|，|和|与|及)", candidate)
        ]
        result: list[str] = []
        for item in parts:
            if item and cls._looks_like_metric(item) and item not in result:
                result.append(item)
        return result[:10]

    @staticmethod
    def _comparison_candidate(answer: str) -> str | None:
        normalized = re.sub(r"\s+", "", answer)
        if any(value in normalized for value in ("同比", "同期", "去年同期")):
            return "同比"
        if any(
            value in normalized
            for value in ("环比", "上月比", "和上月比", "与上月比", "较上月")
        ):
            return "环比"
        if any(value in normalized for value in ("目标值", "与目标比", "对目标")):
            return "目标值"
        if any(value in normalized for value in ("对象间", "两者", "门店之间", "区域之间")):
            return "对象间比较"
        return None

    @staticmethod
    def _dimension_candidates(answer: str) -> list[str]:
        normalized = re.sub(r"\s+", "", answer).strip("，,。.!！?？;；：:")
        match = re.search(
            r"(?:按照|按|分)([^，,。;；]{1,50}?)(?:维度)?(?:分析|统计|查看|看|拆分|分组|占比)?$",
            normalized,
        )
        candidate = match.group(1) if match else normalized
        candidate = candidate.strip("的维度")
        if (
            not candidate
            or len(candidate) > 50
            or any(term in candidate for term in ("时间", "日期", "指标", "取消", "不知道"))
            or re.search(r"20\d{2}|\d{1,2}[日号]", candidate)
        ):
            return []
        values = [
            value.strip()
            for value in re.split(r"(?:、|,|，|和|与|及)", candidate)
            if value.strip()
        ]
        return list(dict.fromkeys(values))[:10]

    @staticmethod
    def _field_candidates(answer: str) -> list[str]:
        normalized = re.sub(r"\s+", "", answer).strip("，,。.!！?？;；：:")
        normalized = re.sub(
            r"^(?:显示|展示|需要|查看|查询|字段(?:是|包括)?|要)", "", normalized
        )
        values = [
            value.strip("字段的")
            for value in re.split(r"(?:、|,|，|和|与|及)", normalized)
            if value.strip("字段的")
        ]
        blocked = {"全部", "所有", "随便", "不知道", "默认", "都行"}
        if not values or any(
            value in blocked
            or len(value) > 50
            or any(term in value for term in ("取消", "不用", "时间范围"))
            for value in values
        ):
            return []
        return list(dict.fromkeys(values))[:20]

    @classmethod
    def _entity_candidate(cls, answer: str) -> str | None:
        candidate = re.sub(r"\s+", "", answer).strip("，,。.!！?？;；：:")
        candidate = re.sub(r"^(?:查询|查看|看|我要|我想看)", "", candidate)
        candidate = re.sub(r"(?:的)?明细(?:数据)?$", "", candidate)
        if (
            not candidate
            or len(candidate) > 50
            or cls._looks_like_metric(candidate)
            or any(term in candidate for term in ("时间", "日期", "本月", "取消", "不知道"))
        ):
            return None
        return candidate

    @staticmethod
    def _correction_value(answer: str) -> str | None:
        normalized = re.sub(r"\s+", "", answer)
        patterns = (
            r"不是.+?(?:而是|是)(.+)$",
            r"(?:改成|改为|更正为|应该是)(.+)$",
        )
        for pattern in patterns:
            if match := re.search(pattern, normalized):
                value = match.group(1).strip("，,。.!！?？;；：:")
                return value or None
        return None

    @staticmethod
    def _looks_like_metric(candidate: str) -> bool:
        if not candidate or len(candidate) > 50 or candidate.startswith("按"):
            return False
        return bool(
            re.search(
                r"(?:额|率|量|数|价|成本|利润|收入|营收|销量|单量|人数|时长|频次|"
                r"GMV|DAU|MAU|UV|PV)$",
                candidate,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _filter_ambiguities(
        ambiguities: list[str], missing_slots: list[str]
    ) -> list[str]:
        """Return unique model hints that correspond to genuinely missing slots."""
        if not ambiguities or not missing_slots:
            return []

        slot_signals = {
            "metric": ("指标", "度量", "销售额", "订单量", "金额", "数量"),
            "time_range": ("时间", "日期", "范围", "周期", "哪天", "月份", "年度"),
            "entity": ("实体", "对象", "业务明细", "哪类", "哪种", "数据对象"),
            "fields": ("字段", "列", "明细项", "展示项"),
            "comparison_type": ("同比", "环比", "比较", "对比", "目标值", "基准"),
            "dimension": ("维度", "分组", "按什么", "拆分"),
            "semantic_ambiguity": ("口径", "歧义", "候选", "语义"),
        }
        active_signals = tuple(
            signal
            for slot in missing_slots
            for signal in slot_signals.get(slot, ())
        )
        if not active_signals:
            return []

        result: list[str] = []
        seen: set[str] = set()
        for value in ambiguities:
            text = re.sub(r"\s+", " ", value).strip()
            normalized = text.rstrip("。！？!?；; ")
            if (
                not normalized
                or normalized in seen
                or not any(signal in normalized for signal in active_signals)
            ):
                continue
            # The orchestrator already has one canonical prompt per missing slot.
            # Do not append a model paraphrase such as “未说明时间范围”, which
            # would present the same clarification twice.  Preserve only extra,
            # actionable ambiguity detail (for example an unclear period edge).
            if any(
                marker in normalized
                for marker in (
                    "未说明", "未提供", "未指定", "未明确", "没有说明",
                    "没有提供", "缺少", "需要补充",
                )
            ):
                continue
            seen.add(normalized)
            result.append(text)
        return result

    @staticmethod
    def _custom_metric_candidate(
        answer: str,
        prior_missing: set[str],
        *,
        strip_temporal_context: bool = False,
    ) -> str | None:
        """Conservatively extract an unverified metric name from a slot reply."""
        candidate = re.sub(r"\s+", "", answer).strip("，,。.!！?？;；：:\"'“”‘’")
        candidate = re.sub(
            r"^(?:(?:我想|我要|请|麻烦|帮我)(?:查|查询|看|查看|分析)?|"
            r"(?:查|查询|看|查看|分析))(?:一下)?",
            "",
            candidate,
        ).strip("，,。.!！?？;；：:")
        if strip_temporal_context:
            # The deterministic date parser has already validated this answer.
            # Remove only well-known temporal spans so one concise reply such as
            # “2026年7月1号到30号的支付转化率” can fill both missing slots.
            candidate = re.sub(
                r"20\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日|号)?"
                r"(?:到|至|~|～|—|–|－|-)"
                r"(?:(?:20\d{2}(?:年|[-/.]))?\d{1,2}(?:月|[-/.]))?"
                r"\d{1,2}(?:日|号)?",
                "",
                candidate,
            )
            candidate = re.sub(
                r"20\d{2}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日|号)?",
                "",
                candidate,
            )
            candidate = re.sub(r"20\d{2}年\d{1,2}月", "", candidate)
            candidate = re.sub(
                r"(?:最近|近)\d{1,3}天|本月|这个月|上个月?|下个月?|今天|昨天|"
                r"今年|本年|去年|本季度|这个季度|上个季度?",
                "",
                candidate,
            )
            candidate = candidate.strip("，,。.!！?？;；：:的在从期间内")
        if not candidate or len(candidate) > 100:
            return None

        if candidate in {
            "这个", "那个", "全部", "所有", "都行", "随便", "不知道", "不清楚",
            "好的", "是的", "继续", "就这样", "没有", "无",
        }:
            return None
        if any(term in candidate for term in ("取消", "不用了", "停止", "算了")):
            return None

        # A pure date/time reply must never be mistaken for a metric when several
        # slots are being collected in the same clarification round.
        temporal_signals = (
            "今天", "昨天", "本月", "上月", "下月", "今年", "去年", "季度",
            "最近", "日期", "时间", "到", "至",
        )
        has_date_shape = bool(
            re.search(r"(?:20\d{2}年|\d{1,2}月|\d{1,2}[日号]|20\d{2}[-/.]\d{1,2})", candidate)
        )
        if has_date_shape or any(term in candidate for term in temporal_signals):
            return None

        # Even when metric is the only missing slot, require a business-measure
        # lexical shape. Otherwise short location/dimension replies such as
        # “北京” or “按地区” would be executed as invented metrics.
        return candidate if HybridIntentClassifier._looks_like_metric(candidate) else None

    @staticmethod
    def _normalize_metric_text(value: str) -> str:
        return re.sub(r"\s+", "", value).strip(
            "，,。.!！?？;；：:\"'“”‘’"
        ).casefold()

    @classmethod
    def _grounded_metric_names(cls, names: list[str], question: str) -> list[str]:
        """Keep grounded, independently computable model metric spans.

        A model can copy a substring such as ``销售`` from ``销售趋势``.  The
        substring is lexically grounded but is not a complete measure and must
        not overwrite a canonical rule/semantic convention such as ``销售额``.
        Other grounded custom metric names remain eligible for semantic-layer
        resolution; this gate rejects only known generic nouns.
        """
        normalized_question = re.sub(r"\s+", "", question).casefold()
        result: list[str] = []
        seen: set[str] = set()
        for value in names:
            name = re.sub(r"\s+", "", value).strip("，,。.!！?？;；：:\"'“”‘’")
            normalized = name.casefold()
            if (
                not name
                or len(name) > 100
                or normalized in seen
                or normalized in cls._generic_metric_nouns
                or normalized not in normalized_question
            ):
                continue
            seen.add(normalized)
            result.append(name)
        return result

    @staticmethod
    def _grounded_text_values(values: list[str], question: str) -> list[str]:
        normalized_question = re.sub(r"\s+", "", question).casefold()
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = re.sub(r"\s+", "", value).strip("，,。.!！?？;；：:\"'“”‘’")
            normalized = item.casefold()
            if (
                not item
                or len(item) > 100
                or normalized in seen
                or normalized not in normalized_question
            ):
                continue
            seen.add(normalized)
            result.append(item)
        return result

    @classmethod
    def _grounded_text_value(cls, value: str, question: str) -> str | None:
        grounded = cls._grounded_text_values([value], question)
        return grounded[0] if grounded else None

    @staticmethod
    def _grounded_comparison_type(value: str, question: str) -> str | None:
        normalized = re.sub(r"\s+", "", question)
        signals = {
            "同比": ("同比", "同期", "去年同期"),
            "环比": ("环比", "上月比", "较上月", "与上月", "和上月"),
            "目标值": ("目标值", "与目标", "对目标"),
            "对象间比较": (
                "对象间", "之间比较", "两者比较", "对比", "比较",
            ),
            "指定时段对比": ("对比", "比较", "相比", "较"),
        }
        canonical = value.strip()
        return canonical if any(
            signal in normalized for signal in signals.get(canonical, ())
        ) else None

    @staticmethod
    def _grounded_current_entity_values(
        values: list[str], question: str
    ) -> list[str]:
        """Keep only model values literally grounded in the current raw turn."""

        current = question.split("\n已确认的上一轮上下文", 1)[0]
        compact = re.sub(r"\s+", "", current)
        generic = {
            "产品", "商品", "经销商", "供应商", "医院", "客户", "门店",
            "厂家", "实体", "对象", "指标", "销售额", "销售量", "订单量",
        }
        grounded: list[str] = []
        for value in values:
            candidate = re.sub(r"\s+", "", str(value or ""))
            candidate = re.sub(r"^(?:那|那么|再看|换成|改成)", "", candidate)
            candidate = re.sub(r"(?:呢|怎么样)[？?。！!]*$", "", candidate)
            candidate = candidate.strip("，,。；;：:！？?、")
            if (
                1 <= len(candidate) <= 100
                and candidate not in generic
                and candidate in compact
                and candidate not in grounded
            ):
                grounded.append(candidate)
        return grounded

    @staticmethod
    def _passes_deterministic_constraints(model: StructuredIntentOutput, question: str) -> bool:
        if model.primary_intent == PrimaryIntent.FORECAST_ANALYSIS:
            future_signals = (
                "预测", "预计", "预估", "推算", "估算", "推测", "未来", "下月",
                "下个月", "下季度", "明年", "后续", "月底", "季末", "年末", "能达到",
                "会达到", "能不能突破",
            )
            if not any(signal in question for signal in future_signals):
                return False
        if model.primary_intent == PrimaryIntent.DETAIL_QUERY:
            detail_signals = (
                "明细", "名单", "清单", "列表", "记录", "逐笔", "每一条",
                "联系方式", "属于哪些", "适用于哪些", "筛选出", "哪些产品",
                "哪些商品", "哪些医院", "卖给了哪些", "销售给哪些",
            )
            # A model-proposed entity is not evidence that the user requested
            # row-level data; require a literal detail signal in the question.
            if not any(signal in question for signal in detail_signals):
                return False
        return True

    @staticmethod
    def _has_strong_rule_signal(intent: PrimaryIntent, question: str) -> bool:
        signals = {
            PrimaryIntent.OUT_OF_SCOPE: ("修改数据库", "删除数据", "写入数据库"),
            PrimaryIntent.REPORT_GENERATION: (
                "报表", "报告", "月报", "导出excel", "导出xlsx", "导出pdf",
                "导出word", "导出docx", "生成excel", "生成xlsx", "生成pdf",
                "生成word", "生成docx", "下载excel", "下载xlsx", "下载pdf",
                "下载word", "下载docx",
            ),
            PrimaryIntent.DATA_LINEAGE: ("血缘", "来源表", "来自哪", "表和字段"),
            PrimaryIntent.METRIC_DEFINITION: ("口径", "定义", "怎么算", "怎么计算", "如何计算", "公式"),
            PrimaryIntent.DATA_QUALITY: ("数据质量", "数据不全", "刷新", "对账"),
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: ("归因", "下降原因", "为什么", "主要因素"),
            PrimaryIntent.FORECAST_ANALYSIS: ("预测", "预计", "未来", "下个月", "下月"),
            PrimaryIntent.ANOMALY_ANALYSIS: ("异常", "突增", "突降", "突然"),
            PrimaryIntent.COMPOSITION_ANALYSIS: ("占比", "构成", "份额"),
            PrimaryIntent.COMPARISON_ANALYSIS: (
                "同比", "环比", "对比", "比较", "增长率", "推荐", "画像", "自动筛选",
            ),
            PrimaryIntent.DETAIL_QUERY: (
                "明细", "名单", "清单", "列表", "逐笔", "联系方式",
                "属于哪些", "适用于哪些", "筛选出", "哪些产品",
                "哪些商品", "哪些医院", "卖给了哪些", "销售给哪些",
            ),
            PrimaryIntent.TREND_ANALYSIS: (
                "趋势", "走势", "历史变化", "按日统计", "按周统计",
                "按月统计", "按季度统计", "按年统计", "按月给我",
                "改成按季度", "不按月了，按季度",
            ),
            PrimaryIntent.CAPABILITY_HELP: ("能做什么", "支持什么", "怎么提问"),
            PrimaryIntent.CHAT: ("你好", "您好", "谢谢", "再见"),
        }
        if intent == PrimaryIntent.FORECAST_ANALYSIS and any(
            term in question for term in ("不是要预测", "不要预测", "无需预测", "不需要预测")
        ):
            return False
        return any(signal in question for signal in signals.get(intent, ()))

    @classmethod
    def _should_skip_model(
        cls, request: CanonicalAnalysisRequest, question: str
    ) -> bool:
        # Business questions always use the structured model when it is
        # enabled. Rules remain the deterministic baseline and safety guard,
        # but must not suppress model-based completion/entity extraction merely
        # because a familiar metric or intent phrase was recognized.
        if request.primary_intent in {
            PrimaryIntent.OUT_OF_SCOPE, PrimaryIntent.CHAT, PrimaryIntent.CAPABILITY_HELP
        } and cls._has_strong_rule_signal(request.primary_intent, question):
            return True
        return False

    @staticmethod
    def _safe_completed_question(value: str, source: str) -> str | None:
        completed = re.sub(r"\s+", " ", value).strip()
        if not completed or len(completed) > 4000:
            return None
        if any(
            marker in completed.lower()
            for marker in ("select ", "insert ", "update ", "delete ", "```", "已确认的上一轮上下文")
        ):
            return None
        current = source.split("\n已确认的上一轮上下文", 1)[0].strip()
        current_numbers = set(re.findall(r"\d+(?:\.\d+)?", current))
        completed_numbers = set(re.findall(r"\d+(?:\.\d+)?", completed))
        if not current_numbers.issubset(completed_numbers):
            return None
        for term in ("不要", "排除", "剔除", "不含", "不是"):
            if term in current and term not in completed:
                return None
        return completed

    @staticmethod
    def _supported_entity_category(
        value: str,
        question: str,
        baseline: CanonicalAnalysisRequest,
    ) -> bool:
        """Allow model-inferred object categories backed by current-turn evidence.

        A user often names a concrete medical device without appending “产品”.
        Requiring the category label itself to occur verbatim would discard the
        model's useful semantic extraction and fall back to keywords again.
        Only a closed set of business object categories can use this inferred
        path; concrete names remain filters and cannot become query objects.
        """
        category = re.sub(r"\s+", "", value)
        compact = re.sub(r"\s+", "", question).split(
            "已确认的上一轮上下文", 1
        )[0]
        current_fields = {
            str(item.get("field") or "")
            for item in baseline.filters
            if isinstance(item, dict)
        }
        evidence = {
            "产品": bool(
                {"商品名称", "产品名称", "商品", "产品"} & current_fields
                or re.search(
                    r"(?:导管|透析器|口罩|套件|球囊|支架|导丝|耗材|器械|设备)",
                    compact,
                )
            ),
            "商品": bool(
                {"商品名称", "产品名称", "商品", "产品"} & current_fields
                or "商品" in compact
            ),
            "经销商": "经销商" in compact,
            "供应商": "供应商" in compact,
            "医院": "医院" in compact,
            "客户": "客户" in compact,
            "门店": "门店" in compact,
            "厂家": bool("厂家" in compact or "制造商" in compact),
            "制造商": bool("厂家" in compact or "制造商" in compact),
            "区域": bool("区域" in compact or "地区" in compact),
            "地区": bool("区域" in compact or "地区" in compact),
            "订单": "订单" in compact,
        }
        return evidence.get(category, False)
