"""Raw-turn admission and provenance-aware context inheritance policy.

The gate intentionally runs before contextual rewriting.  It never reads raw
history or summaries: only the current raw utterance, its deterministic parse,
and the active structured task are used to decide whether inheritance is legal.
"""
from __future__ import annotations

import re
from typing import Any

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ContextMode,
    CurrentTurnFacts,
    PrimaryIntent,
    SlotProvenance,
    SlotSource,
    TurnAdmissionDecision,
    TurnRelation,
)


_REFERENCE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("HISTORICAL", r"最开始|最早|之前|前面|上次|早些时候|回到|恢复"),
    ("PRONOUN", r"这个|那个|这些|那些|它|上述|前述|其中"),
    ("ORDINAL", r"第[一二三四五六七八九十\d]+个|前\d+个|前十个|前三个|前几个"),
    ("ELLIPSIS", r"呢[？?。]?$|怎么样[？?。]?$|继续[。！!？?]?$"),
)
_FOLLOWUP_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "RESULT_PERIOD_COMPARISON",
        r"(?:(?:19|20)\d{2}年)?(?:1[0-2]|0?[1-9])月"
        r"(?:较|比|相比|对比|比较)"
        r"(?:(?:19|20)\d{2}年)?(?:1[0-2]|0?[1-9])月"
        r".{0,12}(?:下降|减少|下跌|增长|增加|上涨|变化|差)(?:了)?多少",
    ),
    (
        "RESULT_PERIOD_RECOVERY",
        r"(?:(?:19|20)\d{2}年)?(?:1[0-2]|0?[1-9])月"
        r".{0,8}(?:恢复|回升|反弹)(?:了)?多少",
    ),
    (
        "RESULT_DELTA_ELLIPSIS",
        r"^(?:下降|减少|下跌|增长|增加|上涨|恢复|回升|变化|相差)(?:了)?多少[？?。]?$",
    ),
    ("REGION_ELLIPSIS", r"^(?:那|那么)?(?:北京|上海|天津|重庆|广东|江苏|浙江|安徽|山东)(?:市|省)?呢"),
    ("DIMENSION_DRILLDOWN", r"^(?:再|也)?按(?:医院|经销商|供应商|地区|区域|省份|城市|科室).{0,12}(?:看看|看|分析|拆分|分组)?"),
    ("GRANULARITY_ONLY", r"^(?:改成|换成|再)?按(?:日|天|周|月|季度|年)(?:统计|汇总|分析|看|给我|吧)?[。！!？?]?$"),
    ("LIMIT_ONLY", r"^(?:只)?(?:保留|显示|展示|返回)?前(?:\d+|[一二三四五六七八九十]+)(?:个|名|条)?[。！!？?]?$"),
    ("TIME_ONLY", r"^(?:那|改成|换成)?(?:今天|昨天|本周|上周|本月|上月|本季度|上季度|今年|去年|近(?:一|二|三|四|五|六|七|八|九|十|\d+)年)[。！!？?]?$"),
    ("LIMIT_REPLACEMENT", r"前(?:\d+|[一二三四五六七八九十]+)(?:个|名|条)?"),
    ("WHY", r"为什么|为何|怎么会"),
    ("CONTINUE", r"继续|接着|再看|再查|同样"),
    (
        "ADDITIVE_METRIC",
        r"(?:再|同时|并)?(?:加上|增加|新增|补充|带上|显示|返回).{0,20}"
        r"(?:指标|金额|销售|数量|笔数|次数|均价|单价|利润|成本|收入)",
    ),
    ("MODIFY", r"改成|改为|换成|只看|只保留|去掉|取消"),
)
_TOPIC_SHIFT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("EXPLICIT_NEW_TASK", r"换个问题|新问题|新任务|重新开始|另外(?:查询|分析|统计)"),
)
_ACTION_PATTERN = re.compile(
    r"查询|查找|列出|统计|计算|分析|比较|对比|排名|排行|趋势|走势|"
    r"明细|名单|清单|报告|预测|归因|异常|占比|构成|"
    r"有哪些|哪些|哪几家|都有谁|有谁"
)
_DRILLDOWN_PATTERN = re.compile(
    r"按(?:医院|经销商|供应商|地区|区域|省份|城市|科室).{0,16}"
    r"(?:看|看看|分析|拆分|分组)|第[一二三四五六七八九十\d]+个|展开|下钻"
)
_CORRECTION_PATTERN = re.compile(r"不是|更正|改成|改为|我说错了|应为|应该是")
_HISTORICAL_PATTERN = re.compile(r"最开始|最早|之前|前面|上次|早些时候|回到|恢复")

_CORE_FILTER_FIELDS = {
    "商品名称": "product",
    "产品名称": "product",
    "商品": "product",
    "产品": "product",
    "商品分类": "category",
    "产品分类": "category",
    "商品品类": "category",
    "品类": "category",
    "类目": "category",
    "类别": "category",
    "品牌名称": "brand",
    "商品品牌": "brand",
    "母品牌": "brand",
    "厂家名称": "manufacturer",
    "制造商名称": "manufacturer",
    "医院名称": "hospital",
    "经销商名称": "dealer",
    "供应商名称": "dealer",
    "地区": "region",
    "业务城市": "region",
    "业务省份": "region",
}


class StaleContextConflictError(ValueError):
    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("STALE_CONTEXT_CONFLICT")
        self.conflicts = conflicts


class TurnAdmissionGate:
    """Classify the raw turn before allowing any business-context inheritance."""

    def evaluate(
        self,
        *,
        question: str,
        current: CanonicalAnalysisRequest,
        previous: CanonicalAnalysisRequest | None,
        message_id: str,
        pending: bool = False,
    ) -> TurnAdmissionDecision:
        facts = self.extract_current_turn_facts(
            question=question,
            current=current,
            message_id=message_id,
        )
        before = self.context_snapshot(previous)
        previous_subjects = self._core_subjects(previous)
        subject_changed = any(
            key in previous_subjects and previous_subjects[key] != value
            for key, value in facts.core_subjects.items()
        )
        has_reference = bool(facts.reference_signals)
        context_dependent = bool(has_reference or facts.followup_signals)
        has_historical = bool(_HISTORICAL_PATTERN.search(question))
        has_topic_shift = bool(facts.topic_shift_signals)

        if previous is None and not pending:
            if has_reference or facts.followup_signals:
                # The raw fragment itself can prove that context is required
                # even when the in-memory task frame is unavailable.  This
                # permits the existing bounded chat-history recovery path to
                # run without exposing that history to the admission parser.
                relation = TurnRelation.CURRENT_TOPIC_FOLLOWUP
                confidence = 0.9
                reasons = ["CONTEXT_DEPENDENT_FRAGMENT", "NO_ACTIVE_TASK_FRAME"]
            else:
                relation = TurnRelation.STANDALONE_NEW_TOPIC
                confidence = 0.99
                reasons = ["NO_ACTIVE_TASK", "CURRENT_QUERY_ONLY"]
        elif has_historical:
            relation = TurnRelation.HISTORICAL_TOPIC_RETURN
            confidence = 0.96
            reasons = ["EXPLICIT_HISTORICAL_REFERENCE"]
        elif has_topic_shift and facts.is_self_contained:
            relation = TurnRelation.STANDALONE_NEW_TOPIC
            confidence = 0.99
            reasons = ["EXPLICIT_TOPIC_SHIFT", "SELF_CONTAINED_QUERY"]
        elif facts.is_self_contained and subject_changed:
            relation = TurnRelation.STANDALONE_NEW_TOPIC
            confidence = 0.99
            reasons = [
                "SELF_CONTAINED_QUERY",
                "EXPLICIT_NEW_CORE_SUBJECT",
                "EXPLICIT_ANALYSIS_ACTION",
                "NO_REFERENCE_DEPENDENCY",
            ]
        elif facts.is_self_contained and not has_reference:
            relation = TurnRelation.STANDALONE_NEW_TOPIC
            confidence = 0.97
            reasons = [
                "SELF_CONTAINED_QUERY",
                "EXPLICIT_ANALYSIS_ACTION",
                "NO_REFERENCE_DEPENDENCY",
            ]
        elif pending:
            relation = TurnRelation.CLARIFICATION_RESPONSE
            confidence = 0.95
            reasons = ["ACTIVE_CLARIFICATION", "INCOMPLETE_WITHOUT_PENDING_STATE"]
        elif _CORRECTION_PATTERN.search(question):
            relation = TurnRelation.CORRECTION
            confidence = 0.95
            reasons = ["EXPLICIT_CORRECTION_LANGUAGE"]
        elif previous is not None and "top_n" in facts.explicit_slots:
            # A quantity stated in the current raw turn replaces the inherited
            # presentation/ranking limit.  Treating it as a generic follow-up
            # allowed an earlier LIMIT 5/result slice to survive "给我前10个".
            relation = TurnRelation.CURRENT_TOPIC_MODIFICATION
            confidence = 0.99
            reasons = ["EXPLICIT_TOP_N_REPLACEMENT", "CURRENT_EXPLICIT_WINS"]
        elif _DRILLDOWN_PATTERN.search(question):
            relation = TurnRelation.CURRENT_TOPIC_DRILLDOWN
            confidence = 0.94
            reasons = ["DRILLDOWN_LANGUAGE", "CONTEXT_REQUIRED"]
        elif subject_changed and has_reference:
            relation = TurnRelation.CURRENT_TOPIC_MODIFICATION
            confidence = 0.96
            reasons = ["EXPLICIT_SUBJECT_REPLACEMENT", "ELLIPTICAL_REFERENCE"]
        elif context_dependent:
            relation = TurnRelation.CURRENT_TOPIC_FOLLOWUP
            confidence = 0.93
            reasons = ["CONTEXT_DEPENDENT_UTTERANCE"]
        elif current.missing_slots:
            relation = TurnRelation.AMBIGUOUS_RELATION
            confidence = 0.72
            reasons = ["CURRENT_QUERY_INCOMPLETE", "NO_STRONG_RELATION_SIGNAL"]
        else:
            # A complete business utterance without reference language must not
            # inherit merely because its domain resembles the previous turn.
            relation = TurnRelation.STANDALONE_NEW_TOPIC
            confidence = 0.92
            reasons = ["COMPLETE_BUSINESS_UTTERANCE", "NO_REFERENCE_DEPENDENCY"]

        inheritance_allowed = relation in {
            TurnRelation.CURRENT_TOPIC_FOLLOWUP,
            TurnRelation.CURRENT_TOPIC_MODIFICATION,
            TurnRelation.CURRENT_TOPIC_DRILLDOWN,
            TurnRelation.HISTORICAL_TOPIC_RETURN,
            TurnRelation.CLARIFICATION_RESPONSE,
            TurnRelation.CORRECTION,
        }
        context_mode = (
            ContextMode.HISTORICAL_THREAD
            if relation == TurnRelation.HISTORICAL_TOPIC_RETURN
            else ContextMode.CLARIFICATION_RESUME
            if relation == TurnRelation.CLARIFICATION_RESPONSE
            else ContextMode.CURRENT_THREAD
            if inheritance_allowed
            else ContextMode.NONE
        )
        protected = sorted(facts.explicit_slots)
        cleared = (
            self._present_business_slots(previous)
            if relation == TurnRelation.STANDALONE_NEW_TOPIC
            else []
        )
        inherited = (
            [
                slot for slot in self._present_business_slots(previous)
                if slot not in protected
            ]
            if inheritance_allowed else []
        )
        return TurnAdmissionDecision(
            relation=relation,
            confidence=confidence,
            context_mode=context_mode,
            current_turn_facts=facts,
            context_dependent=context_dependent,
            core_subject_changed=subject_changed,
            inherit_business_context=inheritance_allowed,
            create_new_analysis_thread=(
                relation == TurnRelation.STANDALONE_NEW_TOPIC
            ),
            historical_recall_required=(
                relation == TurnRelation.HISTORICAL_TOPIC_RETURN
            ),
            reason_codes=reasons,
            protected_slots=protected,
            cleared_slots=cleared,
            inheritance_slots=inherited,
            previous_thread_id=(previous.analysis_thread_id if previous else None),
            previous_episode_id=(str(previous.request_id) if previous else None),
            context_before=before,
            context_delta=self._facts_delta(facts),
        )

    def extract_current_turn_facts(
        self,
        *,
        question: str,
        current: CanonicalAnalysisRequest,
        message_id: str,
    ) -> CurrentTurnFacts:
        compact = re.sub(r"\s+", "", question)
        explicit: dict[str, SlotProvenance] = {}
        inferred: dict[str, SlotProvenance] = {}

        def put(
            target: dict[str, SlotProvenance],
            key: str,
            value: Any,
            source: SlotSource,
            confidence: float = 1.0,
        ) -> None:
            target[key] = SlotProvenance(
                value=value,
                source=source,
                source_turn=message_id,
                confidence=confidence,
            )

        reference_signals = self._signals(question, _REFERENCE_PATTERNS)
        followup_signals = self._signals(question, _FOLLOWUP_PATTERNS)
        topic_shift_signals = self._signals(question, _TOPIC_SHIFT_PATTERNS)
        temporal_references = self._temporal_references(compact)
        action_explicit = bool(_ACTION_PATTERN.search(compact))
        pure_granularity_update = bool(re.fullmatch(
            r"(?:改成|换成|再)?按(?:日|天|周|月|季度|年)"
            r"(?:统计|汇总|分析|看|给我|吧)?[。！!？?]?",
            compact,
        ))
        if action_explicit and not pure_granularity_update:
            put(
                explicit,
                "analysis_type",
                current.primary_intent.value,
                SlotSource.CURRENT_EXPLICIT,
            )

        filters = [dict(item) for item in current.filters]
        core_subjects = self._core_subjects(current)
        lexical_product = self._lexical_product_from_raw_turn(compact)
        if lexical_product and not (
            {"product", "category"} & core_subjects.keys()
        ):
            # Elliptical entity changes such as ``外周插管中心静脉导管呢？``
            # are intentionally absent from a standalone deterministic parse:
            # the analytical action must come from the active thread.  The
            # product itself is nevertheless explicit in the *current* turn
            # and therefore has to be protected before any context merge.
            #
            # A catalog category is different: in a compound scope such as
            # ``江苏苏云品牌低值耗材的经销商`` the raw lexical pattern can see the
            # trailing ``耗材`` and mistake the whole brand/category phrase for
            # one product name.  The deterministic classifier has already
            # normalized that phrase into brand + category filters.  Treat the
            # normalized category as authoritative so the admission gate does
            # not re-introduce a contradictory synthetic product filter.
            core_subjects["product"] = lexical_product
            filters = [
                item
                for item in filters
                if str(item.get("field") or "") not in {
                    "商品名称", "产品名称", "商品", "产品",
                }
            ]
            filters.append({
                "field": "商品名称",
                "operator": "EQ",
                "value": lexical_product,
            })
        lexical_negative_maker = re.search(
            r"(?:排除|剔除|不含|不要|不包括|除去)"
            r"(?P<maker>[^，,。；;？?]{1,80}?)(?:厂家|制造商)",
            compact,
        )
        if lexical_negative_maker is not None:
            maker = lexical_negative_maker.group("maker").strip("的，,。；;、")
            filters = [
                item for item in filters
                if not (
                    str(item.get("field") or "") == "厂家名称"
                    and str(item.get("operator") or "").upper() in {
                        "NE", "!=", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE",
                    }
                )
            ]
            filters.append({
                "field": "厂家名称",
                "operator": "NE",
                "value": maker,
            })
            core_subjects["manufacturer"] = maker
        if filters:
            put(explicit, "filters", filters, SlotSource.CURRENT_EXPLICIT)
        for name, value in core_subjects.items():
            put(explicit, name, value, SlotSource.CURRENT_EXPLICIT)

        metric_names = [item.canonical_name or item.input for item in current.metrics]
        metric_is_explicit = any(name and name in compact for name in metric_names)
        if metric_names:
            put(
                explicit if metric_is_explicit else inferred,
                "metrics",
                metric_names,
                (
                    SlotSource.CURRENT_EXPLICIT
                    if metric_is_explicit else SlotSource.CURRENT_INFERRED
                ),
                1.0 if metric_is_explicit else 0.9,
            )

        filter_dimension_families = {
            self._semantic_field_family(str(item.get("field") or ""))
            for item in filters
            if isinstance(item, dict)
        }
        explicit_dimensions = [
            value for value in current.dimensions
            if re.search(
                rf"(?:按|各|每个|分)(?:[^，,。；;]{{0,8}})?{re.escape(value)}",
                compact,
            )
            or (
                value in compact
                and any(
                    marker in compact
                    for marker in ("名单", "清单", "列表", "列出", "显示", "展示")
                )
            )
            or (
                self._semantic_field_family(value) is not None
                and self._semantic_field_family(value) in filter_dimension_families
            )
            or (
                value == current.entity
                and bool(current.metrics)
                and value in compact
                and any(
                    operator in current.operators
                    for operator in (
                        AnalysisOperator.GROUP_BY,
                        AnalysisOperator.SORT,
                        AnalysisOperator.TOP_N,
                        AnalysisOperator.BOTTOM_N,
                    )
                )
            )
        ]
        if explicit_dimensions:
            put(
                explicit,
                "dimensions",
                explicit_dimensions,
                SlotSource.CURRENT_EXPLICIT,
            )

        object_candidates = [
            value for value in current.dimensions
            if value not in {"产品", "商品", "时间", "日期", "年", "季度", "月", "周", "日"}
            and value in compact
        ]
        explicit_query_object = current.entity or (
            object_candidates[-1] if object_candidates else None
        )
        if explicit_query_object and (
            explicit_query_object in compact
            or any(
                marker in compact
                for marker in ("名单", "清单", "明细", "哪些", "列表", "排名", "排行", "排序")
            )
        ):
            put(
                explicit,
                "query_object",
                explicit_query_object,
                SlotSource.CURRENT_EXPLICIT,
            )
        if current.fields:
            put(
                explicit,
                "projection",
                list(current.fields),
                SlotSource.CURRENT_EXPLICIT,
            )
        if any(
            operator in current.operators
            for operator in (
                AnalysisOperator.SORT,
                AnalysisOperator.TOP_N,
                AnalysisOperator.BOTTOM_N,
            )
        ) and re.search(r"排序|排名|排行|最高|最低|最大|最小|前\d+|top\d+", compact):
            put(
                explicit,
                "sorting",
                {
                    "required": True,
                    "direction": (
                        "ASC"
                        if AnalysisOperator.BOTTOM_N in current.operators
                        or re.search(r"最低|最小|升序|从低到高", compact)
                        else "DESC"
                    ),
                    "limit": current.ranking_limit,
                },
                SlotSource.CURRENT_EXPLICIT,
            )

        contextual_bare_time = bool(
            temporal_references
            and not any(item.get("year") for item in temporal_references)
            and (reference_signals or followup_signals)
        )
        if contextual_bare_time:
            put(
                explicit,
                "temporal_reference",
                temporal_references,
                SlotSource.CURRENT_EXPLICIT,
            )
        elif current.time_range is not None and not any(
            item.startswith("DEFAULT_TIME_RANGE=") for item in current.assumptions
        ):
            put(
                explicit,
                "time_range",
                current.time_range.model_dump(mode="json"),
                SlotSource.CURRENT_EXPLICIT,
            )
        elif current.time_range is not None:
            put(
                inferred,
                "time_range",
                current.time_range.model_dump(mode="json"),
                SlotSource.CURRENT_INFERRED,
                0.8,
            )

        inferred_grain = next(
            (
                value.split("=", 1)[1]
                for value in current.assumptions
                if value.startswith("DEFAULT_TIME_GRANULARITY=")
            ),
            None,
        )
        raw_grain = re.search(r"按(日|天|周|月|季度|年)", compact)
        if raw_grain is not None:
            grain = {
                "日": "day", "天": "day", "周": "week", "月": "month",
                "季度": "quarter", "年": "year",
            }[raw_grain.group(1)]
            put(explicit, "time_grain", grain, SlotSource.CURRENT_EXPLICIT)
        elif inferred_grain:
            put(
                inferred,
                "time_grain",
                inferred_grain,
                SlotSource.CURRENT_INFERRED,
                0.9,
            )
        if current.comparison_type:
            put(
                explicit,
                "comparison",
                current.comparison_type,
                SlotSource.CURRENT_EXPLICIT,
            )
        if current.ranking_limit is not None:
            put(
                explicit,
                "top_n",
                current.ranking_limit,
                SlotSource.CURRENT_EXPLICIT,
            )

        no_reference_dependency = not reference_signals and not followup_signals
        payload_complete = bool(
            core_subjects
            or (
                (metric_names or current.entity or current.dimensions)
                and action_explicit
            )
        )
        self_contained = bool(
            not current.missing_slots
            and action_explicit
            and payload_complete
            and no_reference_dependency
        )
        omitted_slots: list[str] = []
        if reference_signals or followup_signals:
            if not core_subjects:
                omitted_slots.append("subject")
            if not metric_is_explicit:
                omitted_slots.append("metric")
            if temporal_references and not any(
                item.get("year") for item in temporal_references
            ):
                omitted_slots.append("time_year")
            if not filters:
                omitted_slots.append("business_filters")
        return CurrentTurnFacts(
            raw_query=question,
            explicit_slots=explicit,
            inferred_slots=inferred,
            reference_signals=reference_signals,
            followup_signals=followup_signals,
            topic_shift_signals=topic_shift_signals,
            core_subjects=core_subjects,
            omitted_slots=list(dict.fromkeys(omitted_slots)),
            temporal_references=temporal_references,
            is_self_contained=self_contained,
        )

    @staticmethod
    def apply_explicit_slot_protection(
        request: CanonicalAnalysisRequest,
        current: CanonicalAnalysisRequest,
        decision: TurnAdmissionDecision,
    ) -> CanonicalAnalysisRequest:
        """Apply current explicit values after merge using code-level priority."""
        facts = decision.current_turn_facts
        if decision.relation == TurnRelation.STANDALONE_NEW_TOPIC:
            # A standalone turn starts a new semantic frame.  Remove only
            # values that are byte-for-byte carryovers from the prior context;
            # keep any current-turn model enrichment that differs from that
            # context.  Explicit lexical facts are applied immediately below.
            previous = decision.context_before
            current_metrics = [
                item.canonical_name or item.input for item in current.metrics
            ]
            request_metrics = [
                item.canonical_name or item.input for item in request.metrics
            ]
            if (
                request.primary_intent.value == previous.get("intent")
                and request.primary_intent != current.primary_intent
            ):
                request.primary_intent = current.primary_intent
                request.secondary_intents = list(current.secondary_intents)
                request.operators = list(current.operators)
            if (
                request_metrics == previous.get("metrics")
                and request_metrics != current_metrics
            ):
                request.metrics = [
                    item.model_copy(deep=True) for item in current.metrics
                ]
            for attribute in ("entity", "fields", "dimensions"):
                request_value = getattr(request, attribute)
                current_value = getattr(current, attribute)
                if (
                    request_value == previous.get(attribute)
                    and request_value != current_value
                ):
                    setattr(
                        request,
                        attribute,
                        list(current_value)
                        if isinstance(current_value, list)
                        else current_value,
                    )
            previous_filters = previous.get("filters") or []
            explicit_filters = facts.explicit_slots.get("filters")
            current_fact_filters = (
                explicit_filters.value
                if explicit_filters is not None
                and isinstance(explicit_filters.value, list)
                else []
            )
            request.filters = [
                dict(item)
                for item in request.filters
                if item not in previous_filters or item in current_fact_filters
            ]
            request_time = (
                request.time_range.model_dump(mode="json")
                if request.time_range is not None
                else None
            )
            current_time = (
                current.time_range.model_dump(mode="json")
                if current.time_range is not None
                else None
            )
            if (
                request_time == previous.get("time_range")
                and request_time != current_time
            ):
                request.time_range = (
                    current.time_range.model_copy(deep=True)
                    if current.time_range is not None
                    else None
                )
            if (
                request.comparison_type == previous.get("comparison")
                and request.comparison_type != current.comparison_type
            ):
                request.comparison_type = current.comparison_type
            if (
                request.ranking_limit == previous.get("top_n")
                and request.ranking_limit != current.ranking_limit
            ):
                request.ranking_limit = current.ranking_limit
        request.turn_relation = decision.relation
        request.context_mode = decision.context_mode
        request.slot_provenance.update(facts.inferred_slots)
        request.slot_provenance.update(facts.explicit_slots)
        for slot in decision.inheritance_slots:
            if slot in request.slot_provenance or slot in facts.explicit_slots:
                continue
            value = TurnAdmissionGate._slot_value(request, slot)
            if value not in (None, [], {}):
                request.slot_provenance[slot] = SlotProvenance(
                    value=value,
                    source=SlotSource.ACTIVE_THREAD_STATE,
                    source_thread=decision.previous_thread_id,
                    confidence=1.0,
                )

        if "analysis_type" in facts.explicit_slots:
            request.primary_intent = current.primary_intent
            request.secondary_intents = list(current.secondary_intents)
            request.operators = list(current.operators)
        if "metrics" in facts.explicit_slots:
            request.metrics = [item.model_copy(deep=True) for item in current.metrics]
        if "query_object" in facts.explicit_slots:
            request.entity = current.entity
            request.fields = list(current.fields)
        if "dimensions" in facts.explicit_slots:
            explicit_dimensions = facts.explicit_slots["dimensions"].value
            request.dimensions = (
                list(explicit_dimensions)
                if isinstance(explicit_dimensions, list)
                else []
            )
        if "filters" in facts.explicit_slots:
            explicit_filters = facts.explicit_slots["filters"].value
            for current_filter in (
                explicit_filters if isinstance(explicit_filters, list) else []
            ):
                if not isinstance(current_filter, dict):
                    continue
                field = str(current_filter.get("field") or "")
                if not field:
                    continue
                request.filters = [
                    item for item in request.filters
                    if str(item.get("field") or "") != field
                ]
                request.filters.append(dict(current_filter))
        if "time_range" in facts.explicit_slots:
            request.time_range = current.time_range
            request.assumptions = [
                item for item in request.assumptions
                if not item.startswith("DEFAULT_TIME_RANGE=")
            ]
        if "time_grain" in facts.explicit_slots:
            request.assumptions = [
                item for item in request.assumptions
                if not item.startswith("DEFAULT_TIME_GRANULARITY=")
            ]
            current_grain_assumptions = [
                item for item in current.assumptions
                if item.startswith("DEFAULT_TIME_GRANULARITY=")
            ]
            request.assumptions.extend(
                current_grain_assumptions
                or [
                    "DEFAULT_TIME_GRANULARITY="
                    + str(facts.explicit_slots["time_grain"].value)
                ]
            )
            if (
                request.primary_intent == PrimaryIntent.METRIC_QUERY
                and request.metrics
            ):
                # A scalar metric plus an explicit time bucket is a trend
                # execution shape.  The grain-only phrase depends on the
                # previous metric, so its isolated METRIC_QUERY label must not
                # overwrite the merged trend semantics.
                request.primary_intent = PrimaryIntent.TREND_ANALYSIS
                if AnalysisOperator.TIME_BUCKET not in request.operators:
                    request.operators.append(AnalysisOperator.TIME_BUCKET)
                explicit_intent = facts.explicit_slots.get("analysis_type")
                if (
                    explicit_intent is not None
                    and explicit_intent.value == PrimaryIntent.METRIC_QUERY.value
                ):
                    facts.explicit_slots["analysis_type"] = (
                        explicit_intent.model_copy(
                            update={"value": PrimaryIntent.TREND_ANALYSIS.value}
                        )
                    )
                    request.slot_provenance["analysis_type"] = (
                        facts.explicit_slots["analysis_type"]
                    )
        if "comparison" in facts.explicit_slots:
            request.comparison_type = current.comparison_type
        if "top_n" in facts.explicit_slots:
            request.ranking_limit = current.ranking_limit

        if decision.relation == TurnRelation.STANDALONE_NEW_TOPIC:
            request.asl_template = None
            request.source_dataset_id = None
            request.conversation_control = current.conversation_control
        elif decision.core_subject_changed:
            # A semantic subject replacement invalidates both the prior query
            # plan and its materialized dataset.  Reusing either would execute
            # the old entity filter even though the canonical request already
            # contains the new value (for example, product A -> brand B).
            request.asl_template = None
            request.source_dataset_id = None
            if "CORE_SUBJECT_CHANGE_REPLAN_REQUIRED" not in request.assumptions:
                request.assumptions.append(
                    "CORE_SUBJECT_CHANGE_REPLAN_REQUIRED"
                )
        return request

    @classmethod
    def promote_model_entity_replacement(
        cls,
        *,
        decision: TurnAdmissionDecision,
        current: CanonicalAnalysisRequest,
        previous: CanonicalAnalysisRequest | None,
        raw_question: str,
    ) -> None:
        """Promote an LLM-extracted elliptical entity to a protected slot.

        The raw-turn gate intentionally cannot guess a bare value in ``那X呢``.
        Once the structured model has extracted a value that is literally
        grounded in the current text, replace the only compatible active
        filter and mark it as current-turn reference resolution.  This runs
        before contextual merging, so stale active-thread values cannot win.
        """

        if (
            previous is None
            or not decision.inherit_business_context
            or len(current.semantic_entity_mentions) != 1
            or not re.fullmatch(
                r"(?:那|那么|再看|换成|改成)?[^，,。；;？?]{1,100}"
                r"(?:呢|怎么样)[。！!？?]*",
                re.sub(r"\s+", "", raw_question),
            )
        ):
            return
        value = current.semantic_entity_mentions[0].strip()
        if not value or value not in re.sub(r"\s+", "", raw_question):
            return

        candidates = [
            dict(item)
            for item in previous.filters
            if isinstance(item, dict)
            and cls._semantic_field_family(str(item.get("field") or ""))
            and str(item.get("operator") or "EQ").upper() in {"EQ", "=", "IN"}
        ]
        if len(candidates) > 1:
            looks_region = bool(re.search(
                r"(?:省|市|自治区|特别行政区|地区)$", value
            ))
            looks_company = value.endswith(
                ("公司", "有限公司", "有限责任公司", "股份有限公司")
            )
            preferred_families = (
                {"region"}
                if looks_region
                else {"dealer", "manufacturer"}
                if looks_company
                else {"product", "brand", "category"}
            )
            candidates = [
                item for item in candidates
                if cls._semantic_field_family(str(item.get("field") or ""))
                in preferred_families
            ]
        if len(candidates) != 1:
            return

        prior = candidates[0]
        if str(prior.get("value") or "").strip() == value:
            return
        replacement = {
            "field": str(prior.get("field") or ""),
            "operator": "EQ",
            "value": value,
        }
        current.filters = [
            dict(item) for item in previous.filters
            if str(item.get("field") or "") != replacement["field"]
        ]
        current.filters.append(replacement)
        facts = decision.current_turn_facts
        facts.explicit_slots["filters"] = SlotProvenance(
            value=[dict(item) for item in current.filters],
            source=SlotSource.CURRENT_REFERENCE_RESOLUTION,
            source_turn=next(
                (
                    slot.source_turn for slot in facts.explicit_slots.values()
                    if slot.source_turn
                ),
                None,
            ),
            confidence=max(current.intent_confidence, 0.8),
        )
        family = cls._semantic_field_family(replacement["field"])
        if family:
            facts.core_subjects[family] = value
            facts.explicit_slots[family] = SlotProvenance(
                value=value,
                source=SlotSource.CURRENT_REFERENCE_RESOLUTION,
                source_turn=facts.explicit_slots["filters"].source_turn,
                confidence=max(current.intent_confidence, 0.8),
            )
        decision.relation = TurnRelation.CURRENT_TOPIC_MODIFICATION
        decision.core_subject_changed = True
        decision.reason_codes = list(dict.fromkeys([
            *decision.reason_codes,
            "MODEL_GROUNDED_ENTITY_REPLACEMENT",
            "CURRENT_EXPLICIT_WINS",
        ]))
        decision.protected_slots = list(dict.fromkeys([
            *decision.protected_slots,
            "filters",
            *( [family] if family else [] ),
        ]))
        decision.inheritance_slots = [
            slot for slot in decision.inheritance_slots
            if slot not in {"filters", family}
        ]
        decision.context_delta = cls._facts_delta(facts)

    @classmethod
    def rebind_current_semantic_shape(
        cls,
        decision: TurnAdmissionDecision,
        current: CanonicalAnalysisRequest,
    ) -> None:
        """Refresh slot labels after current-model semantic grounding.

        Turn relation is intentionally left untouched: the raw-turn gate has
        already decided whether history may be inherited.  Only field and
        dimension labels are refreshed, while literal values and provenance
        remain current-turn facts.  This keeps strict downstream alignment in
        sync when semantic metadata renames a dimension.
        """

        facts = decision.current_turn_facts
        source_turn = next(
            (
                slot.source_turn
                for slot in facts.explicit_slots.values()
                if slot.source_turn
            ),
            None,
        )
        if current.filters:
            prior = facts.explicit_slots.get("filters")
            facts.explicit_slots["filters"] = SlotProvenance(
                value=[dict(item) for item in current.filters],
                source=(prior.source if prior is not None else SlotSource.CURRENT_EXPLICIT),
                source_turn=(prior.source_turn if prior is not None else source_turn),
                confidence=(prior.confidence if prior is not None else 1.0),
            )
        if current.dimensions:
            prior = facts.explicit_slots.get("dimensions")
            facts.explicit_slots["dimensions"] = SlotProvenance(
                value=list(current.dimensions),
                source=(prior.source if prior is not None else SlotSource.CURRENT_EXPLICIT),
                source_turn=(prior.source_turn if prior is not None else source_turn),
                confidence=(prior.confidence if prior is not None else 1.0),
            )
        facts.core_subjects = cls._core_subjects(current)
        refreshed_slots = list(decision.protected_slots)
        if current.filters:
            refreshed_slots.append("filters")
        if current.dimensions:
            refreshed_slots.append("dimensions")
        decision.protected_slots = list(dict.fromkeys(refreshed_slots))
        decision.context_delta = cls._facts_delta(facts)

    @classmethod
    def validate_context_consistency(
        cls,
        request: CanonicalAnalysisRequest,
        decision: TurnAdmissionDecision,
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        explicit_filters = decision.current_turn_facts.explicit_slots.get("filters")
        if explicit_filters is not None and isinstance(explicit_filters.value, list):
            final_by_field = {
                str(item.get("field") or ""): item.get("value")
                for item in request.filters
                if isinstance(item, dict) and item.get("field")
            }
            for item in explicit_filters.value:
                if not isinstance(item, dict) or not item.get("field"):
                    continue
                field = str(item["field"])
                if final_by_field.get(field) != item.get("value"):
                    conflicts.append({
                        "slot": f"filters.{field}",
                        "current_explicit": item.get("value"),
                        "context_after": final_by_field.get(field),
                        "code": "STALE_CONTEXT_CONFLICT",
                    })
        expected_intent = decision.current_turn_facts.explicit_slots.get(
            "analysis_type"
        )
        if (
            expected_intent is not None
            and request.primary_intent.value != expected_intent.value
        ):
            conflicts.append({
                "slot": "analysis_type",
                "current_explicit": expected_intent.value,
                "context_after": request.primary_intent.value,
                "code": "STALE_CONTEXT_CONFLICT",
            })
        return conflicts

    @staticmethod
    def context_snapshot(
        request: CanonicalAnalysisRequest | None,
    ) -> dict[str, Any]:
        if request is None:
            return {}
        return {
            "analysis_thread_id": request.analysis_thread_id,
            "intent": request.primary_intent.value,
            "entity": request.entity,
            "metrics": [item.canonical_name or item.input for item in request.metrics],
            "dimensions": list(request.dimensions),
            "fields": list(request.fields),
            "filters": [dict(item) for item in request.filters],
            "time_range": (
                request.time_range.model_dump(mode="json")
                if request.time_range else None
            ),
            "comparison": request.comparison_type,
            "top_n": request.ranking_limit,
            "source_dataset_id": request.source_dataset_id,
            "has_asl_template": request.asl_template is not None,
        }

    @staticmethod
    def _signals(
        question: str, patterns: tuple[tuple[str, str], ...]
    ) -> list[str]:
        return [name for name, pattern in patterns if re.search(pattern, question)]

    @staticmethod
    def _temporal_references(compact: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for match in re.finditer(
            r"(?:(?P<year>(?:19|20)\d{2})年)?"
            r"(?P<month>1[0-2]|0?[1-9])月",
            compact,
        ):
            values.append({
                "surface": match.group(0),
                "year": int(match.group("year")) if match.group("year") else None,
                "month": int(match.group("month")),
            })
        return values

    @staticmethod
    def _slot_value(request: CanonicalAnalysisRequest, slot: str) -> Any:
        values: dict[str, Any] = {
            "analysis_type": request.primary_intent.value,
            "entity": request.entity,
            "metrics": [
                item.canonical_name or item.input for item in request.metrics
            ],
            "fields": list(request.fields),
            "dimensions": list(request.dimensions),
            "filters": [dict(item) for item in request.filters],
            "time_range": (
                request.time_range.model_dump(mode="json")
                if request.time_range else None
            ),
            "comparison": request.comparison_type,
            "top_n": request.ranking_limit,
            "previous_sql_plan": request.asl_template,
            "result_reference": request.source_dataset_id,
        }
        return values.get(slot)

    @staticmethod
    def _lexical_product_from_raw_turn(compact: str) -> str | None:
        """Extract a product explicitly named in the raw turn.

        This is deliberately narrow and never reads history.  It supplements
        the deterministic classifier for (a) elliptical entity replacement
        and (b) synthetic/abbreviated names such as ``A产品`` that are useful in
        conversation-policy regression tests.
        """
        product_suffix = r"(?:产品|商品|导管|透析器|口罩|套件|球囊|支架|导丝|耗材)"
        match = re.fullmatch(
            rf"(?:那|那么)?(?P<product>[^，,。；;？?]{{1,100}}?{product_suffix})"
            r"呢[。！!？?]?",
            compact,
        )
        if match is None:
            match = re.search(
                rf"^(?:请|帮我|给我)?(?:按(?:日|天|周|月|季度|年))?"
                rf"(?:查询|查找|列出|统计|分析|查看|看看)"
                rf"(?P<product>[^，,。；;？?]{{1,100}}?{product_suffix})"
                r"(?:(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月"
                r"(?:至|到|-)(?:1[0-2]|0?[1-9])月|"
                r"(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月)?"
                r"(?:的)?(?:合作的?)?(?:经销商|供应商|医院|客户|门店|"
                r"销售趋势|销售走势|销售变化|销售情况|销售额|"
                r"医院销量|医院销售量|销量|销售量)",
                compact,
            )
        if match is None:
            match = re.fullmatch(
                rf"(?P<product>[^，,。；;？?]{{1,100}}?{product_suffix})"
                r"(?:销售)?(?:趋势|走势)[。！!？?]?",
                compact,
            )
        if match is None:
            return None
        product = match.group("product").strip("的，,；;、")
        product = re.sub(
            r"^(?:北京|上海|天津|重庆)(?:市|地区)?|"
            r"^(?:广东|江苏|浙江|安徽|山东|河南|湖北|湖南|四川|福建|河北|辽宁)"
            r"(?:省|地区)?",
            "",
            product,
        ).strip("的，,；;、")
        product = re.sub(r"(?:产品|商品)$", "", product).strip("的，,；;、")
        if not 1 <= len(product) <= 100:
            return None
        return product

    @staticmethod
    def _core_subjects(
        request: CanonicalAnalysisRequest | None,
    ) -> dict[str, str]:
        if request is None:
            return {}
        result: dict[str, str] = {}
        for item in request.filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            subject_type = TurnAdmissionGate._semantic_field_family(field)
            value = item.get("value")
            if subject_type and isinstance(value, (str, int, float)):
                text = str(value).strip()
                if text:
                    result[subject_type] = text
        return result

    @staticmethod
    def _semantic_field_family(field: str) -> str | None:
        direct = _CORE_FILTER_FIELDS.get(field)
        if direct is not None:
            return direct
        folded = str(field or "").strip().casefold()
        families = (
            ("category", ("商品分类", "产品分类", "商品品类", "品类", "类目", "类别", "category", "class")),
            ("brand", ("商品品牌", "品牌", "brand")),
            ("region", ("地区", "区域", "省份", "城市", "region", "province", "city")),
            ("dealer", ("经销商", "供应商", "dealer", "supplier", "vendor")),
            ("product", ("商品名称", "产品名称", "product_name", "goods_name")),
        )
        return next(
            (name for name, aliases in families if any(alias in folded for alias in aliases)),
            None,
        )

    @staticmethod
    def _facts_delta(facts: CurrentTurnFacts) -> dict[str, Any]:
        return {
            key: provenance.model_dump(mode="json")
            for key, provenance in {
                **facts.inferred_slots,
                **facts.explicit_slots,
            }.items()
        }

    @staticmethod
    def _present_business_slots(
        request: CanonicalAnalysisRequest | None,
    ) -> list[str]:
        if request is None:
            return []
        present: list[str] = []
        for name, value in (
            ("analysis_type", request.primary_intent),
            ("entity", request.entity),
            ("metrics", request.metrics),
            ("fields", request.fields),
            ("dimensions", request.dimensions),
            ("filters", request.filters),
            ("time_range", request.time_range),
            ("comparison", request.comparison_type),
            ("top_n", request.ranking_limit),
            ("previous_sql_plan", request.asl_template),
            ("result_reference", request.source_dataset_id),
        ):
            if value not in (None, [], {}):
                present.append(name)
        return present
