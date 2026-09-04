from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Protocol
import re

import httpx

from app.domain.models import (
    CanonicalAnalysisRequest,
    SemanticAmbiguity,
    SemanticFilterBinding,
)
from app.services.entity_extraction import EntityCandidate, EntityCandidateExtractor


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RewriteEvent:
    original: str
    canonical: str
    kind: str
    confidence: float
    attribute_code: str | None = None


@dataclass(frozen=True)
class RewriteResult:
    original_question: str
    rewritten_question: str
    events: list[RewriteEvent] = field(default_factory=list)
    context_applied: bool = False
    degraded: bool = False
    # Current semantic-layer hits are retained even when no textual rewrite is
    # necessary.  Intent extraction can then bind user-facing roles (city,
    # brand, category, and so on) to the latest registered dimension labels
    # without guessing physical columns or relying on a stale local dictionary.
    semantic_matches: list[dict[str, Any]] = field(default_factory=list)
    semantic_ambiguities: list[SemanticAmbiguity] = field(default_factory=list)
    semantic_model_version: str | None = None


class EntityAttributeSearcher(Protocol):
    async def search(
        self,
        query: str,
        *,
        semantic_model_id: int,
        business_domain_id: int | None,
        business_domain_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def resolve_display_slots(
        self,
        candidates: list[dict[str, str]],
        *,
        semantic_model_id: int,
        business_domain_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]: ...


class HttpEntityAttributeSearcher:
    def __init__(
        self,
        *,
        base_url: str,
        path: str,
        timeout_seconds: float,
        top_k: int,
        score_threshold: float,
        display_resolve_path: str = "/vector/semantic-elements/resolve",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.display_resolve_path = display_resolve_path

    async def search(
        self,
        query: str,
        *,
        semantic_model_id: int,
        business_domain_id: int | None,
        business_domain_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.path,
                json={
                    "query": query,
                    "semantic_model_id": semantic_model_id,
                    "business_domain_id": business_domain_id,
                    "business_domain_ids": list(business_domain_ids or []),
                    "top_k": self.top_k,
                    "score_threshold": self.score_threshold,
                },
            )
        response.raise_for_status()
        payload = response.json()
        matches = payload.get("matches", []) if isinstance(payload, dict) else []
        if not isinstance(matches, list):
            raise ValueError("entity attribute search returned invalid matches")
        version = (
            payload.get("semantic_model_version")
            or payload.get("model_version")
            or payload.get("published_version")
            or payload.get("version")
        ) if isinstance(payload, dict) else None
        result: list[dict[str, Any]] = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            if version is not None and "semantic_model_version" not in normalized:
                normalized["semantic_model_version"] = str(version)
            result.append(normalized)
        return result

    async def resolve_display_slots(
        self,
        candidates: list[dict[str, str]],
        *,
        semantic_model_id: int,
        business_domain_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.display_resolve_path,
                json={
                    "semantic_model_id": semantic_model_id,
                    "business_domain_ids": list(business_domain_ids or []),
                    "candidates": candidates,
                },
            )
        response.raise_for_status()
        payload = response.json()
        matches = payload.get("matches", []) if isinstance(payload, dict) else []
        if not isinstance(matches, list):
            raise ValueError("semantic display resolver returned invalid matches")
        return [dict(item) for item in matches if isinstance(item, dict)]


class QuestionRewriter:
    """Conservative pre-intent question rewriting.

    Short-term context can add already-confirmed scope. Entity vectors only
    provide candidates; deterministic lexical checks decide whether a candidate
    is safe enough to replace. An upstream outage never blocks intent routing.
    """

    _STRONG_CONTEXT_MARKERS = (
        "上述", "刚才", "前面", "同样", "再看", "再查", "再查询", "那查询", "继续", "改成", "换成", "改查",
        "只看", "去掉", "取消", "恢复", "同时", "也要", "再加", "加上", "增加", "新增",
        "其他条件不变", "其余条件不变", "这些", "其中", "它", "第一个",
    )
    _BUSINESS_FOLLOWUP_TERMS = (
        "销售", "订单", "退款", "库存", "金额", "数量", "指标", "数据", "趋势",
        "同比", "环比", "占比", "异常", "下降", "增长", "上涨", "下跌", "波动",
        "区域", "地区", "门店", "渠道", "客户", "商品", "供应商", "科室",
        "华东", "华南", "华北", "华中", "东北", "西南", "西北",
        "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "成都", "武汉",
    )
    _INTENT_TERMS = (
        "同比", "环比", "增长率", "趋势", "走势", "预测", "预估", "原因", "归因",
        "异常", "占比", "构成", "明细", "口径", "血缘", "报表", "报告",
    )
    _EXPLICIT_METRIC_PATTERN = re.compile(
        r"销售额|订单量|销售量|客户数|客单价|退款额|退款率|"
        r"匹配度|覆盖率|转化率|毛利(?:额|率)?|利润(?:额|率)?|库存(?:量|金额)?"
    )
    _NEGATION_TERMS = ("不", "没", "无", "非", "不要", "排除", "剔除", "未")
    # High-frequency, unambiguous metric typos observed in real user traffic.
    # This local layer keeps basic normalization available while the optional
    # vector endpoint is unavailable; broad fuzzy matching remains delegated to
    # the scoped entity index and its ambiguity checks.
    _LOCAL_METRIC_TYPOS = {
        "销受额": "销售额",
        "定单量": "订单量",
        "客单介": "客单价",
        "退宽率": "退款率",
    }

    def __init__(
        self,
        searcher: EntityAttributeSearcher | None,
        *,
        candidate_extractor: EntityCandidateExtractor | None = None,
        candidate_mode: str = "off",
        auto_replace_threshold: float = 0.88,
        candidate_gap: float = 0.05,
        typo_similarity_threshold: float = 0.84,
    ) -> None:
        self.searcher = searcher
        if candidate_mode not in {"off", "shadow", "assist"}:
            raise ValueError("candidate_mode must be off, shadow or assist")
        self.candidate_extractor = candidate_extractor
        self.candidate_mode = candidate_mode
        self.auto_replace_threshold = auto_replace_threshold
        self.candidate_gap = candidate_gap
        self.typo_similarity_threshold = typo_similarity_threshold

    async def ground_display_slots(
        self,
        request: CanonicalAnalysisRequest,
    ) -> CanonicalAnalysisRequest:
        """Keep only user-visible slots proven by the current vector catalog.

        This method never mutates executable metrics, dimensions, fields, or
        filters. Resolver failures therefore hide diagnostics instead of
        changing query behavior.
        """
        request.semantic_display_slots = {}
        resolver = getattr(self.searcher, "resolve_display_slots", None)
        if not callable(resolver) or request.semantic_model_id is None:
            return request

        hidden_placeholders = {
            "未提取", "未识别", "未知", "无", "none", "null", "n/a", "-",
        }

        def visible_candidate(value: Any) -> str:
            text = str(value or "").strip()
            return "" if text.casefold() in hidden_placeholders else text

        candidates: list[dict[str, str]] = []
        for index, metric in enumerate(request.metrics):
            value = visible_candidate(metric.canonical_name or metric.input)
            if value:
                candidates.append({"candidate_id": f"metric:{index}", "slot": "metric", "value": value})
        entity_value = visible_candidate(request.entity)
        if entity_value:
            candidates.append({"candidate_id": "entity:0", "slot": "entity", "value": entity_value})
        for index, value in enumerate(request.dimensions):
            candidate_value = visible_candidate(value)
            if candidate_value:
                candidates.append({"candidate_id": f"dimension:{index}", "slot": "dimension", "value": candidate_value})
        for index, value in enumerate(request.fields):
            candidate_value = visible_candidate(value)
            if candidate_value:
                candidates.append({"candidate_id": f"field:{index}", "slot": "field", "value": candidate_value})
        for index, item in enumerate(request.filters):
            if not isinstance(item, dict):
                continue
            raw_value = item.get("value")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value_index, value in enumerate(values):
                candidate_value = visible_candidate(value).strip("%")
                if candidate_value:
                    candidate = {
                        "candidate_id": f"filter:{index}:{value_index}",
                        "slot": "filter",
                        "value": candidate_value,
                    }
                    field_name = visible_candidate(item.get("field"))
                    if field_name:
                        candidate["field_name"] = field_name
                    candidates.append(candidate)
        for index, value in enumerate(request.semantic_entity_mentions):
            candidate_value = visible_candidate(value)
            if candidate_value:
                candidates.append({
                    "candidate_id": f"mention:{index}",
                    "slot": "filter",
                    "value": candidate_value,
                })
        if not candidates:
            return request

        try:
            matches = await resolver(
                candidates,
                semantic_model_id=request.semantic_model_id,
                business_domain_ids=request.business_domain_ids,
            )
        except Exception as exc:
            logger.warning(
                "semantic display grounding unavailable; hiding unverified slots: %s",
                type(exc).__name__,
            )
            return request

        by_id = {
            str(item.get("candidate_id") or ""): item
            for item in matches
            if isinstance(item, dict) and str(item.get("candidate_id") or "")
        }
        display: dict[str, Any] = {}
        metrics = [
            str(by_id[f"metric:{index}"].get("canonical_name") or "").strip()
            for index in range(len(request.metrics))
            if f"metric:{index}" in by_id
            and str(by_id[f"metric:{index}"].get("canonical_name") or "").strip()
        ]
        if metrics:
            display["metrics"] = list(dict.fromkeys(metrics))
        entity_match = by_id.get("entity:0")
        if entity_match and str(entity_match.get("canonical_name") or "").strip():
            display["entity"] = str(entity_match["canonical_name"]).strip()
        for slot, values in (("dimensions", request.dimensions), ("fields", request.fields)):
            candidate_slot = slot[:-1]
            canonical = [
                str(by_id[f"{candidate_slot}:{index}"].get("canonical_name") or "").strip()
                for index in range(len(values))
                if f"{candidate_slot}:{index}" in by_id
                and str(by_id[f"{candidate_slot}:{index}"].get("canonical_name") or "").strip()
            ]
            if canonical:
                display[slot] = list(dict.fromkeys(canonical))

        display_filters: list[dict[str, Any]] = []
        entity_values: list[str] = []
        for index, item in enumerate(request.filters):
            if not isinstance(item, dict):
                continue
            raw_value = item.get("value")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            resolved = [by_id.get(f"filter:{index}:{value_index}") for value_index in range(len(values))]
            if not resolved or any(match is None for match in resolved):
                continue
            names = {str(match.get("canonical_name") or "").strip() for match in resolved if match}
            canonical_values = [str(match.get("canonical_value") or "").strip() for match in resolved if match]
            if len(names) != 1 or not next(iter(names), "") or any(not value for value in canonical_values):
                continue
            normalized = dict(item)
            normalized["field"] = next(iter(names))
            normalized["value"] = canonical_values if isinstance(raw_value, list) else canonical_values[0]
            display_filters.append(normalized)
        for index in range(len(request.semantic_entity_mentions)):
            match = by_id.get(f"mention:{index}")
            value = str((match or {}).get("canonical_value") or "").strip()
            if value:
                entity_values.append(value)
        if display_filters:
            display["filters"] = display_filters
        if entity_values:
            display["entity_values"] = list(dict.fromkeys(entity_values))
        request.semantic_display_slots = display
        return request

    async def ground_executable_filters(
        self,
        request: CanonicalAnalysisRequest,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
        business_domain_ids: list[int] | None = None,
    ) -> list[SemanticAmbiguity]:
        """Resolve each filter literal independently against the live catalog.

        Whole-question embeddings are intentionally broad and can crowd an
        exact brand/manufacturer hit out of a small top-k result set.  Querying
        the already extracted literal closes that recall gap while the existing
        family/gap gates still decide whether a binding is safe.  The model's
        provisional field is replaced atomically with the vector-catalog field
        and canonical value; ambiguous cross-attribute hits remain blocking.
        """

        if self.searcher is None or semantic_model_id is None:
            return []
        literals: list[str] = []
        for item in request.filters:
            if not isinstance(item, dict):
                continue
            raw = item.get("value")
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                literal = str(value or "").strip().strip("%")
                if (
                    literal
                    and len(literal) <= 500
                    and not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", literal)
                ):
                    literals.append(literal)
        literals = list(dict.fromkeys(literals))[:20]
        if not literals:
            return []

        results = await asyncio.gather(*[
            self.searcher.search(
                literal,
                semantic_model_id=semantic_model_id,
                business_domain_id=business_domain_id,
                business_domain_ids=business_domain_ids,
            )
            for literal in literals
        ], return_exceptions=True)
        matches: list[dict[str, Any]] = []
        ambiguities: list[SemanticAmbiguity] = []
        for literal, result in zip(literals, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "isolated entity filter grounding skipped safely: literal=%r error=%s",
                    literal,
                    type(result).__name__,
                )
                continue
            current = [dict(item) for item in result if isinstance(item, dict)]
            matches.extend(current)
            version = self._semantic_model_version(current)
            ambiguities.extend(self._detect_semantic_ambiguities(
                literal,
                current,
                semantic_model_id=semantic_model_id,
                semantic_model_version=version,
            ))

        if matches:
            # Stable deduplication prevents repeated refresh/index rows from
            # manufacturing a false ambiguity or changing the winning score.
            unique: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
            for item in matches:
                key = (
                    str(item.get("attribute_code") or ""),
                    str(item.get("attribute_name") or ""),
                    str(item.get("attribute_value") or item.get("canonical_value") or ""),
                    item.get("business_domain_id"),
                )
                previous = unique.get(key)
                if previous is None or float(item.get("score") or 0.0) > float(
                    previous.get("score") or 0.0
                ):
                    unique[key] = item
            self.ground_request_dimensions(request, list(unique.values()))
            versions = {
                binding.semantic_model_version
                for binding in request.semantic_filter_bindings
                if binding.semantic_model_version
            }
            if len(versions) == 1:
                request.semantic_model_version = next(iter(versions))
            resolved_domains = sorted({
                binding.business_domain_id
                for binding in request.semantic_filter_bindings
                if binding.business_domain_id is not None
            })
            if resolved_domains:
                request.resolved_business_domain_ids = resolved_domains

        by_id: dict[str, SemanticAmbiguity] = {}
        for item in ambiguities:
            by_id[item.ambiguity_id or f"{item.type}:{item.phrase}"] = item
        return list(by_id.values())[:5]

    async def rewrite(
        self,
        question: str,
        *,
        previous: CanonicalAnalysisRequest | None,
        semantic_model_id: int | None,
        business_domain_id: int | None,
        business_domain_ids: list[int] | None = None,
        force_context: bool = False,
    ) -> RewriteResult:
        original = question.strip()
        locally_normalized, local_events = self._normalize_local_metric_typos(original)
        locally_normalized, word_order_events = self._normalize_polite_word_order(
            locally_normalized
        )
        local_events.extend(word_order_events)
        locally_normalized, grouped_wording_events = (
            self._normalize_grouped_calculation_wording(locally_normalized)
        )
        local_events.extend(grouped_wording_events)
        locally_normalized, temporal_events = self._normalize_short_year(
            locally_normalized
        )
        local_events.extend(temporal_events)
        rewritten, context_applied = self._apply_context(
            locally_normalized,
            previous,
            semantic_model_id=semantic_model_id,
            business_domain_ids=business_domain_ids,
            force_context=force_context,
        )
        if previous is not None and self.is_deterministic_slot_update(locally_normalized):
            return RewriteResult(
                original, rewritten, local_events, context_applied=context_applied
            )
        candidates = await self._extract_candidates(locally_normalized)
        local_events.extend(
            RewriteEvent(
                item.text,
                item.text,
                f"ENTITY_CANDIDATE_{self.candidate_mode.upper()}:{item.label}",
                item.score,
            )
            for item in candidates
        )
        if self.searcher is None or semantic_model_id is None:
            return RewriteResult(
                original, rewritten, local_events, context_applied=context_applied
            )
        try:
            queries = [locally_normalized]
            if self.candidate_mode == "assist":
                queries.extend(item.text for item in candidates[:4])
            results = await asyncio.gather(*[
                self.searcher.search(
                    query,
                    semantic_model_id=semantic_model_id,
                    business_domain_id=business_domain_id,
                    business_domain_ids=business_domain_ids,
                )
                for query in dict.fromkeys(queries)
            ], return_exceptions=True)
            primary_result = results[0]
            if isinstance(primary_result, Exception):
                raise primary_result
            matches = list(primary_result)
            for result in results[1:]:
                if isinstance(result, Exception):
                    logger.warning(
                        "candidate entity catalog lookup skipped safely: %s", result
                    )
                    continue
                matches.extend(result)
        except Exception as exc:
            logger.warning("entity normalization degraded to original question: %s", exc)
            return RewriteResult(
                original, rewritten, local_events,
                context_applied=context_applied, degraded=True
            )

        normalized, events = self._normalize(rewritten, locally_normalized, matches)
        semantic_version = self._semantic_model_version(matches)
        semantic_ambiguities = self._detect_semantic_ambiguities(
            locally_normalized,
            matches,
            semantic_model_id=semantic_model_id,
            semantic_model_version=semantic_version,
        )
        if not self._critical_terms_preserved(original, normalized):
            logger.warning("unsafe rewrite rejected because intent/number/negation terms changed")
            return RewriteResult(
                original, rewritten, context_applied=context_applied, degraded=True,
                semantic_matches=[dict(item) for item in matches],
                semantic_ambiguities=semantic_ambiguities,
                semantic_model_version=semantic_version,
            )
        return RewriteResult(
            original,
            normalized,
            local_events + events,
            context_applied,
            semantic_matches=[dict(item) for item in matches],
            semantic_ambiguities=semantic_ambiguities,
            semantic_model_version=semantic_version,
        )

    @staticmethod
    def _semantic_model_version(matches: list[dict[str, Any]]) -> str | None:
        versions = {
            str(item.get("semantic_model_version") or item.get("model_version") or "").strip()
            for item in matches
            if str(item.get("semantic_model_version") or item.get("model_version") or "").strip()
        }
        return next(iter(versions)) if len(versions) == 1 else None

    def _detect_semantic_ambiguities(
        self,
        question: str,
        matches: list[dict[str, Any]],
        *,
        semantic_model_id: int | None,
        semantic_model_version: str | None,
    ) -> list[SemanticAmbiguity]:
        """Promote close live-semantic candidates instead of silently skipping them."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for match in matches:
            score = float(match.get("score") or 0.0)
            if score < self.auto_replace_threshold:
                continue
            aliases = self._aliases(match.get("entity_alias"))
            surfaces = [
                *aliases,
                str(match.get("attribute_value") or "").strip(),
                str(match.get("entity_name") or "").strip(),
            ]
            for surface in dict.fromkeys(value for value in surfaces if len(value) >= 2):
                if surface in question:
                    grouped.setdefault(surface, []).append(match)

        def unique_candidates(
            alternatives: list[dict[str, Any]],
        ) -> dict[tuple[str, str, str], dict[str, Any]]:
            unique: dict[tuple[str, str, str], dict[str, Any]] = {}
            for item in alternatives:
                key = (
                    str(item.get("attribute_code") or ""),
                    str(item.get("attribute_name") or ""),
                    str(item.get("entity_name") or item.get("attribute_value") or ""),
                )
                current = unique.get(key)
                if current is None or float(item.get("score") or 0.0) > float(
                    current.get("score") or 0.0
                ):
                    unique[key] = item
            return unique

        # A unique exact full name is stronger evidence than ambiguous aliases
        # nested inside it.  For example, 上海市口腔医院 must not be blocked merely
        # because the shorter surface 口腔医院 is shared by other catalog rows.
        uniquely_grounded_surfaces = {
            surface
            for surface, alternatives in grouped.items()
            if len(unique_candidates(alternatives)) == 1
        }

        result: list[SemanticAmbiguity] = []
        for surface, alternatives in grouped.items():
            if any(
                surface != longer
                and len(longer) > len(surface)
                and surface in longer
                for longer in uniquely_grounded_surfaces
            ):
                continue
            expected_family = self._expected_semantic_family(question, surface)
            if expected_family is not None:
                role_compatible = [
                    item
                    for item in alternatives
                    if self._semantic_match_family(item) == expected_family
                ]
                if role_compatible:
                    alternatives = role_compatible
            unique = unique_candidates(alternatives)
            ranked = sorted(unique.values(), key=lambda item: -float(item.get("score") or 0.0))
            if len(ranked) < 2:
                continue
            if float(ranked[0].get("score") or 0.0) - float(ranked[1].get("score") or 0.0) >= self.candidate_gap:
                continue
            details: list[dict[str, Any]] = []
            labels: list[str] = []
            for item in ranked[:5]:
                attribute_name = str(item.get("attribute_name") or "").strip()
                entity_name = str(item.get("entity_name") or "").strip()
                attribute_value = str(item.get("attribute_value") or surface).strip()
                label = "：".join(value for value in (attribute_name, entity_name or attribute_value) if value) or attribute_value
                if label in labels:
                    continue
                labels.append(label)
                details.append({
                    "semantic_id": str(item.get("attribute_code") or item.get("id") or ""),
                    "label": label,
                    "value": attribute_value,
                    "entity_name": entity_name,
                    "attribute_name": attribute_name,
                    "score": float(item.get("score") or 0.0),
                    "semantic_model_version": semantic_model_version,
                })
            if len(labels) < 2:
                continue
            digest = hashlib.sha256(
                f"{semantic_model_id}:{semantic_model_version}:{surface}:{'|'.join(labels)}".encode("utf-8")
            ).hexdigest()[:20]
            result.append(SemanticAmbiguity(
                ambiguity_id=f"semantic-rewrite-{digest}",
                type="entity_role",
                phrase=surface,
                affected_slots=["filters"],
                question=f"“{surface}”在当前语义层有多个业务含义，请确认本次指的是哪一个？",
                candidates=labels,
                candidate_details=details,
                material_impact="不同选择会改变筛选字段或关联实体",
                blocking=True,
                semantic_model_id=semantic_model_id,
                semantic_model_version=semantic_model_version,
            ))
        return result[:5]

    @staticmethod
    def _semantic_match_family(match: dict[str, Any]) -> str | None:
        reference = " ".join(
            str(match.get(key) or "").casefold()
            for key in (
                "attribute_name", "attribute_code", "dimension_name",
                "field_name", "entity_name",
            )
        )
        families = (
            ("category", ("商品分类", "产品分类", "商品品类", "品类", "category", "class")),
            ("brand", ("商品品牌", "品牌", "parent_brand", "brand")),
            ("manufacturer", ("厂家", "制造商", "厂商", "manufacturer", "maker", "producer")),
            ("hospital", ("医院", "护理院", "卫生服务中心", "hospital")),
            ("partner", ("经销商", "供应商", "dealer", "supplier", "vendor")),
            ("region", ("地区", "区域", "省份", "城市", "region", "province", "city")),
            ("product", ("商品名称", "产品名称", "商品", "产品", "product", "goods", "sku")),
        )
        return next(
            (name for name, aliases in families if any(alias in reference for alias in aliases)),
            None,
        )

    @staticmethod
    def _expected_semantic_family(question: str, surface: str) -> str | None:
        compact_surface = re.sub(r"\s+", "", surface).casefold()
        if any(
            marker in compact_surface
            for marker in ("医院", "护理院", "卫生服务中心", "卫生院")
        ):
            return "hospital"
        legal_manufacturer = bool(re.search(
            r"公司|有限责任|股份|集团|"
            r"(?:inc\.?|gmbh|company|corp\.?|co\.?|ltd\.?|llc|"
            r"s\.?a\.?|ag|plc)|surgical",
            surface,
            re.I,
        ))
        if legal_manufacturer:
            tail = question[question.find(surface) + len(surface):]
            if re.search(r".{0,60}(?:产品|商品)", tail, re.S):
                return "manufacturer"
        return None

    @staticmethod
    def _normalize_polite_word_order(text: str) -> tuple[str, list[RewriteEvent]]:
        """Normalize harmless polite-particle inversions before semantic recall.

        Users and generated paraphrases may write ``按月请计算`` or
        ``以月份为粒度请统计``.  Moving ``请`` to the front preserves every
        business token while avoiding an unstable parse of the temporal phrase.
        """
        pattern = re.compile(
            r"^(?P<scope>(?:按|以)[^，,。；;！？!?]{1,20}?)"
            r"请(?P<verb>查询|计算|统计|分析|汇总|列出|展示|返回)"
        )
        match = pattern.search(text)
        if match is None:
            return text, []
        replacement = f"请{match.group('scope')}{match.group('verb')}"
        normalized = pattern.sub(replacement, text, count=1)
        return normalized, [RewriteEvent(
            match.group(0), replacement, "POLITE_WORD_ORDER", 1.0
        )]

    @staticmethod
    def _normalize_grouped_calculation_wording(
        text: str,
    ) -> tuple[str, list[RewriteEvent]]:
        """Canonicalize temporal grouped-query verbs without changing scope."""
        pattern = re.compile(
            r"^(?:请)?按(?P<grain>日|天|周|月|季度|年)"
            r"(?P<verb>计算|查询|汇总)"
        )
        match = pattern.search(text)
        if match is None:
            return text, []
        replacement = f"按{match.group('grain')}统计"
        normalized = pattern.sub(replacement, text, count=1)
        return normalized, [RewriteEvent(
            match.group(0), replacement, "GROUPED_CALCULATION_WORDING", 1.0
        )]

    @classmethod
    def ground_request_dimensions(
        cls,
        request: CanonicalAnalysisRequest,
        matches: list[dict[str, Any]],
    ) -> CanonicalAnalysisRequest:
        """Bind canonical filter roles to current semantic dimension labels.

        The vector endpoint is scoped by semantic model and business domain, so
        its attribute names are fresher than application-side aliases.  A hit is
        accepted only when both its semantic family and its literal value agree
        with a caller-grounded filter.  Physical codes remain with Oagnet; this
        method changes user-facing semantic labels and, after a literal match,
        replaces the model-extracted value with the canonical value stored by
        the vector catalog.  The model span is only a retrieval candidate; it
        must not survive beside the catalog value once the hit is accepted.
        """

        def family(value: Any) -> str | None:
            text = str(value or "").strip().casefold()
            if not text:
                return None
            # More specific compound concepts must win before the generic
            # product family (``product_category`` contains both tokens).
            families = (
                ("category", ("商品分类", "产品分类", "商品品类", "品类", "类目", "类别", "category", "class")),
                ("brand", ("商品品牌", "品牌", "brand")),
                ("manufacturer", ("厂家", "制造商", "厂商", "manufacturer", "maker", "producer")),
                ("hospital", ("医院", "护理院", "卫生服务中心", "hospital")),
                ("region", ("地区", "区域", "省份", "城市", "region", "province", "city")),
                ("partner", ("经销商", "供应商", "dealer", "supplier", "vendor")),
                ("product", ("商品名称", "产品名称", "商品", "产品", "product", "goods", "sku")),
            )
            return next(
                (name for name, aliases in families if any(alias in text for alias in aliases)),
                None,
            )

        candidates: list[dict[str, Any]] = []
        for item in matches:
            if not isinstance(item, dict):
                continue
            label = str(
                item.get("attribute_name")
                or item.get("dimension_name")
                or item.get("field_name")
                or ""
            ).strip()
            if not label or len(label) > 100 or any(ord(char) < 32 for char in label):
                continue
            reference = " ".join(
                str(item.get(key) or "")
                for key in (
                    "attribute_name", "attribute_code", "dimension_name",
                    "field_name", "entity_name",
                )
            )
            candidate_family = family(reference)
            if candidate_family is None:
                continue
            canonical_value = str(
                item.get("canonical_value")
                or item.get("attribute_value")
                or ""
            ).strip()
            candidate_values = {
                str(item.get(key) or "").strip()
                for key in ("attribute_value", "canonical_value")
                if str(item.get(key) or "").strip()
            }
            if not canonical_value:
                continue
            candidates.append({
                "family": candidate_family,
                "label": label,
                "values": candidate_values,
                "canonical_value": canonical_value,
                "score": float(item.get("score") or 0.0),
                "attribute_code": str(item.get("attribute_code") or "").strip(),
                "record_id": str(item.get("record_id") or item.get("id") or "").strip() or None,
                "business_domain_id": item.get("business_domain_id"),
                "semantic_model_version": str(
                    item.get("semantic_model_version") or item.get("model_version") or ""
                ).strip() or None,
            })

        grounded_by_family: dict[str, str] = {}
        grounded_filters: list[dict[str, Any]] = []
        canonicalized_literals: dict[str, str] = {}
        family_rebound = False

        def equivalent_literal(
            required: str,
            candidate_value: str,
            candidate_family: str,
        ) -> bool:
            required_folded = required.casefold()
            candidate_folded = candidate_value.casefold()
            if (
                required_folded in candidate_folded
                or candidate_folded in required_folded
            ):
                return True
            if candidate_family not in {"manufacturer", "hospital"}:
                return False

            # Legal organization names frequently vary only in whitespace and
            # punctuation (Inc./Inc, B. Braun/B.Braun, Chinese/English comma).
            # Normalize those separators for catalog comparison while keeping
            # the original literal untouched in the executable request.
            def organization_key(value: str) -> str:
                return re.sub(
                    r"[\s,，.。;；:：()（）\[\]{}'\"“”‘’_\-]+",
                    "",
                    value.casefold(),
                )

            required_key = organization_key(required)
            candidate_key = organization_key(candidate_value)
            return bool(
                required_key
                and candidate_key
                and (
                    required_key in candidate_key
                    or candidate_key in required_key
                )
            )

        semantic_bindings: list[SemanticFilterBinding] = []
        for filter_index, item in enumerate(request.filters):
            if not isinstance(item, dict):
                grounded_filters.append(item)
                continue
            current = dict(item)
            current_family = family(current.get("field"))
            raw_value = current.get("value")
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            required_values = {
                str(value).strip().strip("%")
                for value in values
                if value not in (None, "")
            }
            value_matches = lambda candidate: (
                candidate["score"] >= 0.70
                and required_values
                and all(
                    any(
                        equivalent_literal(
                            required,
                            candidate_value,
                            candidate["family"],
                        )
                        for candidate_value in candidate["values"]
                    )
                    for required in required_values
                )
            )
            ranked = sorted(
                (
                    candidate for candidate in candidates
                    if candidate["family"] == current_family
                    and value_matches(candidate)
                ),
                key=lambda candidate: (-candidate["score"], candidate["label"]),
            )
            if not ranked:
                # The completion model can identify the right literal but
                # attach a provisional family (for example 商品名称=费森尤斯).
                # Rebind only from high-confidence, current-model catalog
                # evidence and only when the best family is unambiguous.  This
                # keeps changing semantic dimensions authoritative without
                # guessing a brand/product distinction from a static wordlist.
                cross_family = sorted(
                    (candidate for candidate in candidates if value_matches(candidate)),
                    key=lambda candidate: (-candidate["score"], candidate["label"]),
                )
                if cross_family:
                    best = cross_family[0]
                    competing_families = {
                        candidate["family"]
                        for candidate in cross_family
                        if best["score"] - candidate["score"] < 0.05
                    }
                    if len(competing_families) == 1:
                        ranked = [best]
                        family_rebound = best["family"] != current_family
            if ranked and current_family is not None:
                selected = ranked[0]
                current["field"] = selected["label"]
                grounded_by_family[selected["family"]] = selected["label"]
                canonical_value = selected["canonical_value"]
                for required in required_values:
                    canonicalized_literals[required.casefold()] = canonical_value
                if isinstance(raw_value, list):
                    current["value"] = list(dict.fromkeys(
                        canonicalized_literals.get(
                            str(value).strip().strip("%").casefold(),
                            value,
                        )
                        for value in raw_value
                    ))
                elif raw_value not in (None, ""):
                    current["value"] = canonical_value
                attribute_code = selected["attribute_code"]
                if attribute_code:
                    for required in sorted(required_values):
                        semantic_bindings.append(SemanticFilterBinding(
                            filter_index=filter_index,
                            input_value=required,
                            canonical_value=canonical_value,
                            canonical_name=selected["label"],
                            attribute_code=attribute_code,
                            record_id=selected["record_id"],
                            score=selected["score"],
                            business_domain_id=selected["business_domain_id"],
                            semantic_model_version=selected["semantic_model_version"],
                        ))
            grounded_filters.append(current)

        if not grounded_by_family and not canonicalized_literals:
            return request
        request.filters = grounded_filters
        request.semantic_filter_bindings = semantic_bindings
        request.dimensions = list(dict.fromkeys(
            grounded_by_family.get(family(value) or "", value)
            for value in request.dimensions
        ))
        request.assumptions.append("SEMANTIC_DIMENSIONS_GROUNDED_FROM_CURRENT_MODEL")
        if canonicalized_literals:
            normalized_mentions: list[str] = []
            for mention in request.semantic_entity_mentions:
                literal = str(mention or "").strip()
                if not literal:
                    continue
                replacement = canonicalized_literals.get(literal.casefold())
                if replacement is None:
                    ranked_mentions = sorted(
                        (
                            candidate for candidate in candidates
                            if candidate["score"] >= 0.70
                            and any(
                                equivalent_literal(
                                    literal,
                                    candidate_value,
                                    candidate["family"],
                                )
                                for candidate_value in candidate["values"]
                            )
                        ),
                        key=lambda candidate: (
                            -candidate["score"],
                            candidate["label"],
                            candidate["canonical_value"],
                        ),
                    )
                    if ranked_mentions:
                        replacement = ranked_mentions[0]["canonical_value"]
                normalized_mentions.append(replacement or literal)
            request.semantic_entity_mentions = list(dict.fromkeys(normalized_mentions))
            request.assumptions.append(
                "SEMANTIC_ENTITY_VALUES_CANONICALIZED_FROM_CURRENT_MODEL"
            )
        if family_rebound:
            request.assumptions.append(
                "SEMANTIC_FILTER_FAMILY_REBOUND_FROM_CURRENT_MODEL"
            )
        cls._remove_filter_subject_from_trend_grouping(request)
        return request

    @staticmethod
    def _remove_filter_subject_from_trend_grouping(
        request: CanonicalAnalysisRequest,
    ) -> None:
        """Keep ``某品牌产品`` as scope unless product grouping was explicit."""

        if str(request.primary_intent) != "TREND_ANALYSIS":
            return
        has_brand_scope = any(
            any(token in binding.attribute_code.casefold() for token in (
                "brand", "manufacturer",
            ))
            for binding in request.semantic_filter_bindings
        )
        if not has_brand_scope:
            return
        compact = re.sub(r"\s+", "", request.original_question or "")
        explicit_product_grouping = bool(re.search(
            r"(?:按|分|各|每个|分别(?:按)?)(?:商品|产品|SKU|货品)"
            r"|(?:商品|产品|SKU|货品)(?:维度|分别|各自)",
            compact,
            re.I,
        ))
        if explicit_product_grouping:
            return
        filtered = [
            value for value in request.dimensions
            if value not in {"商品", "产品", "商品名称", "产品名称"}
        ]
        if filtered != request.dimensions:
            request.dimensions = filtered
            request.assumptions.append(
                "FILTER_SUBJECT_REMOVED_FROM_TREND_GROUPING"
            )

    async def _extract_candidates(self, question: str) -> list[EntityCandidate]:
        if self.candidate_extractor is None or self.candidate_mode == "off":
            return []
        try:
            return await self.candidate_extractor.extract(question)
        except Exception as exc:
            logger.warning("entity candidate extraction degraded safely: %s", exc)
            return []

    @classmethod
    def is_deterministic_slot_update(cls, question: str) -> bool:
        """Return true only for closed-form context replacements needing no entity search."""
        compact = re.sub(r"\s+", "", question).strip("，,。.!！?？")
        if cls.is_deterministic_time_update(compact):
            return True
        if re.fullmatch(
            r"(?:不按[^，,]+[,，]?)?(?:改成|换成|还是)?按(?:日|周|月|季度|年)"
            r"(?:统计|汇总|分析|看|给我|吧)?",
            compact,
        ):
            return True
        time_value = (
            r"(?:(?:19|20)\d{2}年)?(?:1[0-2]|0?[1-9])月份?"
            r"|今天|昨天|本周|上周|本月|上月|本季度|上季度|今年|去年"
        )
        if re.fullmatch(rf"(?:那|那么|改成|换成|再看)?(?:{time_value})(?:呢|怎么样)?", compact):
            return True
        metric = cls._EXPLICIT_METRIC_PATTERN.pattern
        if re.fullmatch(rf"(?:那|那么|改成|换成|不是[^，,]+[,，]?是|再看|再加)?(?:{metric})(?:呢|怎么样)?", compact):
            return True
        if re.fullmatch(
            r"(?:改成|换成|不按[^，,]+[,，]?按|按)"
            r"(?:区域|地区|门店|渠道|客户|商品|供应商|科室)(?:拆分|分析|看)?",
            compact,
        ):
            return True
        return bool(re.fullmatch(r"(?:改成|换成)?(?:同比|环比|目标值|对象间比较)", compact))

    @staticmethod
    def is_deterministic_time_update(question: str) -> bool:
        """Recognize a self-contained time-slot answer without model inference."""
        normalized_question, _ = QuestionRewriter._normalize_short_year(question)
        compact = re.sub(r"\s+", "", normalized_question).strip("，,。.!！?？")
        # Natural follow-ups often wrap an otherwise closed-form time value in
        # a lightweight query verb.  Remove that wrapper before matching; the
        # remaining text must still be a complete time expression, so this does
        # not turn arbitrary new questions into deterministic slot updates.
        compact = re.sub(
            r"^(?:那|那么)?(?:再)?(?:查询|查|看)(?:一下)?",
            "",
            compact,
        )
        prefixes = (
            "三个分析维度都按", "全部分析维度都按", "所有分析维度都按",
            "各分析维度都按", "这份报告统一按", "这份报告按",
            "报告统一按", "报告按", "时间范围是", "时间范围为",
            "时间范围", "那么改成", "那么换成", "改成", "换成",
            "再看", "那么", "那", "统一按", "都按", "按", "从",
        )
        for prefix in prefixes:
            if compact.startswith(prefix):
                compact = compact[len(prefix):]
                break
        for suffix in ("这个范围", "该范围", "期间", "范围", "呢", "怎么样", "的"):
            if compact.endswith(suffix):
                compact = compact[:-len(suffix)]
                break

        day = (
            r"(?:(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月(?:3[01]|[12]\d|0?[1-9])[日号]"
            r"|(?:19|20)\d{2}[-/.](?:1[0-2]|0?[1-9])[-/.](?:3[01]|[12]\d|0?[1-9]))"
        )
        same_month_period = (
            r"(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月"
            r"(?:3[01]|[12]\d|0?[1-9])[日号]"
            r"(?:至|到|~|～|—|–|－)(?:3[01]|[12]\d|0?[1-9])[日号]"
        )
        month = r"(?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月份?"
        period = (
            rf"{same_month_period}|{day}(?:至|到|~|～|—|–|－){day}"
            rf"|{month}(?:至|到|~|～|—|–|－){month}"
            r"|(?:19|20)\d{2}年(?:至|到|~|～|—|–|－)(?:19|20)\d{2}年"
            rf"|{day}|{month}|(?:19|20)\d{{2}}年"
            r"|今天|昨天|本周|上周|本月|上月|本季度|上季度|今年|去年"
            r"|(?:本|上)?(?:一|二|三|四|1|2|3|4)季度|(?:本|上|下)半年"
            r"|(?:最近|近|过去)(?:\d{1,3}|一|二|两|三|四|五|六|七|八|九|十|十二|半年)"
            r"(?:个)?(?:天|日|周|星期|月|年)?"
        )
        if re.fullmatch(period, compact):
            return True
        # Users commonly repeat the already-known metric while changing only
        # the period: “那2025年12月份的含税销售总额是多少”。  This is still a
        # deterministic time refinement and must rebind the verified ASL
        # template instead of reusing its previous relative-month predicate.
        metric = (
            r"(?:含税)?(?:销售总额|销售额)|订单(?:量|笔数)|销售(?:总数量|数量|量)|"
            r"客户数|客单价|退款(?:额|率)|毛利(?:额|率)?|利润(?:额|率)?"
        )
        return bool(re.fullmatch(
            rf"(?:{period})(?:的)?(?:{metric})(?:是)?(?:多少|怎么样|呢)?",
            compact,
        ))

    @classmethod
    def _normalize_local_metric_typos(
        cls, question: str
    ) -> tuple[str, list[RewriteEvent]]:
        normalized = question
        events: list[RewriteEvent] = []
        for surface, canonical in cls._LOCAL_METRIC_TYPOS.items():
            if surface not in normalized:
                continue
            normalized = normalized.replace(surface, canonical)
            events.append(RewriteEvent(surface, canonical, "TYPO_LOCAL", 1.0))
        return normalized, events

    @staticmethod
    def _normalize_short_year(question: str) -> tuple[str, list[RewriteEvent]]:
        """Expand ``25年12月`` to ``2025年12月`` before context merging."""
        match = re.search(r"(?<!\d)(?P<year>\d{2})年(?=\d{1,2}月)", question)
        if not match:
            return question, []
        full_year = 2000 + int(match.group("year"))
        normalized = question[:match.start()] + f"{full_year}年" + question[match.end():]
        return normalized, [
            RewriteEvent(match.group(0), f"{full_year}年", "TEMPORAL_SHORTHAND", 1.0)
        ]

    def _apply_context(
        self,
        question: str,
        previous: CanonicalAnalysisRequest | None,
        *,
        semantic_model_id: int | None,
        business_domain_ids: list[int] | None,
        force_context: bool = False,
    ) -> tuple[str, bool]:
        if previous is None:
            return question, False
        if (
            previous.semantic_model_id is not None
            and semantic_model_id is not None
            and previous.semantic_model_id != semantic_model_id
        ):
            return question, False
        current_domains = set(business_domain_ids or [])
        previous_domains = set(previous.business_domain_ids)
        if current_domains and previous_domains and current_domains != previous_domains:
            return question, False
        strong_reference = any(
            marker in question for marker in self._STRONG_CONTEXT_MARKERS
        )
        modification_reference = bool(re.search(
            r"(?:^|[，,])(?:再)?按[^，,。]{1,30}(?:拆分|分组|排序|展示|显示|查看|统计)"
            r"|(?:不按[^，,]+[,，]?)?(?:改成|换成|还是)?按(?:日|周|月|季度|年)(?:统计|汇总|分析|看|给我|吧)?"
            r"|(?:展示|显示|只看|保留)?(?:前|后)(?:\d+|[一二三四五六七八九十]{1,3})(?:名|个|条)?",
            question,
        )) and not self._EXPLICIT_METRIC_PATTERN.search(question)
        weak_reference = (
            any(marker in question for marker in ("这个", "这些", "那个", "那", "呢", "怎么样", "为什么"))
            and (
                any(term in question for term in self._BUSINESS_FOLLOWUP_TERMS)
                or bool(re.search(
                    r"(?:19|20)\d{2}年|(?:1[0-2]|0?[1-9])月份?|"
                    r"今天|昨天|本周|上周|本月|上月|今年|去年",
                    question,
                ))
            )
        )
        if (
            not force_context
            and not strong_reference
            and not weak_reference
            and not modification_reference
        ):
            return question, False
        # Expand the most common elliptical period switch into a standalone
        # business question. This is both easier for semantic retrieval and
        # prevents the previous period from being accidentally unioned in.
        bare_month = re.fullmatch(r"(?:那|再看)?(1[0-2]|0?[1-9])月份?(?:呢|怎么样)?[？?]?", question)
        if bare_month and previous.metrics and previous.time_range:
            metric_names = [
                item.canonical_name or item.input for item in previous.metrics
            ]
            rewritten = (
                f"查询{previous.time_range.start.year}年{int(bare_month.group(1))}月"
                + "、".join(dict.fromkeys(metric_names))
            )
            return rewritten, True
        context: list[str] = []
        relationship_followup = bool(re.search(
            r"(?:哪些|什么)(?:产品|商品|医院|经销商|供应商|等级|科室)"
            r"|(?:主要|次要|主|次)(?:适用)?科室"
            r"|(?:卖给|销售给)(?:了)?哪些|(?:医院|产品|商品).{0,12}(?:等级|级别)",
            question,
        ))
        # A follow-up may replace the metric rather than inherit it, e.g.
        # “那订单量呢”. Additive wording is different: “再加上订单笔数” must
        # keep the previous metrics and append the new one. Rendering the
        # confirmed previous metrics gives the model the complete requested
        # measure set without hard-coding any particular business metric.
        additive_metric_reference = bool(re.search(
            r"(?:再|同时|并)?(?:加上|增加|新增|补充|带上|显示|返回).{0,20}"
            r"(?:指标|金额|销售|数量|笔数|次数|均价|单价|利润|成本|收入)",
            question,
        ))
        if (
            previous.metrics
            and not relationship_followup
            and (
                not self._EXPLICIT_METRIC_PATTERN.search(question)
                or additive_metric_reference
            )
        ):
            names = [item.canonical_name or item.input for item in previous.metrics]
            context.append("指标=" + "、".join(dict.fromkeys(names)))
        if previous.entity:
            context.append("实体=" + previous.entity)
        if previous.semantic_entity_mentions:
            context.append(
                "业务实体值="
                + "、".join(dict.fromkeys(previous.semantic_entity_mentions))
            )
        if previous.dimensions and not relationship_followup:
            context.append("维度=" + "、".join(previous.dimensions))
        elif (
            previous.entity
            and re.search(r"(?:其中|这些|哪个|哪一个).{0,20}(?:最高|最低|最大|最小)", question)
        ):
            context.append("维度=" + previous.entity)
        if previous.time_range:
            # An explicit time in the follow-up replaces, rather than widens,
            # the previous range. Appending both made “那6月呢” look like
            # 6月至7月 and returned a two-month total.
            has_current_time = bool(re.search(
                r"(?:19|20)\d{2}年|(?:1[0-2]|0?[1-9])月份?|"
                r"今天|昨天|本周|上周|本月|上月|本季度|上季度|今年|去年|"
                r"最近(?:一|两|三|四|五|六|七|八|九|十|\d+)(?:天|周|个月|月|年)",
                question,
            ))
            if not has_current_time:
                context.append(
                    f"时间={previous.time_range.start.isoformat()}至"
                    f"{previous.time_range.end_exclusive.isoformat()}（结束日不含）"
                )
        if previous.filters:
            context.append(
                "过滤条件=" + json.dumps(previous.filters, ensure_ascii=False, separators=(",", ":"))
            )
        if not context:
            return question, False
        return f"{question}\n已确认的上一轮上下文（当前问题明确内容优先）：{'；'.join(context)}", True

    def _normalize(
        self, rewritten: str, original: str, matches: list[dict[str, Any]]
    ) -> tuple[str, list[RewriteEvent]]:
        candidates: list[tuple[float, str, str, str, str | None]] = []
        for match in matches:
            vector_score = float(match.get("score") or 0.0)
            if vector_score < self.auto_replace_threshold:
                continue
            canonical_value = str(match.get("attribute_value") or "").strip()
            canonical_entity = str(match.get("entity_name") or "").strip()
            aliases = self._aliases(match.get("entity_alias"))
            attribute_code = str(match.get("attribute_code") or "").strip() or None
            for surface, canonical, kind in [
                *((alias, canonical_entity, "ALIAS") for alias in aliases if canonical_entity),
                (canonical_value, canonical_value, "CANONICAL"),
                (canonical_entity, canonical_entity, "CANONICAL"),
            ]:
                if not surface or not canonical:
                    continue
                # One-character aliases are inherently ambiguous in natural
                # language and must never trigger an automatic global rewrite.
                if len(surface) < 2:
                    continue
                if surface in original and surface != canonical:
                    candidates.append((vector_score, surface, canonical, kind, attribute_code))
                    continue
                typo = self._best_typo(original, surface)
                typo_is_safe = bool(
                    typo
                    and typo[0] != canonical
                    and (
                        typo[1] >= self.typo_similarity_threshold
                        or self._single_substitution(typo[0], surface)
                    )
                )
                if typo and typo_is_safe and self._safe_typo_replacement(typo[0], canonical):
                    confidence = min(vector_score, typo[1])
                    candidates.append((confidence, typo[0], canonical, "TYPO", attribute_code))

        # Prefer a complete business term over a shorter alias contained in it;
        # otherwise "北经区域" would first rewrite "区域" and destroy the safer
        # whole-term correction to "北京区域".
        candidates.sort(key=lambda item: (-len(item[1]), -item[0], item[1]))
        grouped: dict[str, list[tuple[float, str, str, str, str | None]]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate[1], []).append(candidate)

        # Check ambiguity for every surface, not merely the first vector hit.
        unambiguous: list[tuple[float, str, str, str, str | None]] = []
        for alternatives in grouped.values():
            best_by_canonical: dict[str, tuple[float, str, str, str, str | None]] = {}
            for item in alternatives:
                current = best_by_canonical.get(item[2])
                if current is None or item[0] > current[0]:
                    best_by_canonical[item[2]] = item
            ranked = sorted(best_by_canonical.values(), key=lambda item: -item[0])
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < self.candidate_gap:
                continue
            unambiguous.append(ranked[0])

        unambiguous.sort(key=lambda item: (-len(item[1]), -item[0], item[1]))

        # Rewrite only the original user span. Confirmed context appended by
        # _apply_context is trusted state and must not be changed by an alias.
        user_text = original
        context_suffix = rewritten[len(original):] if rewritten.startswith(original) else ""
        events: list[RewriteEvent] = []
        occupied_spans: list[tuple[int, int]] = []
        replacements: list[tuple[int, int, str]] = []
        for confidence, surface, canonical, kind, attribute_code in unambiguous:
            overlaps_prior = any(
                surface in event.original or surface in event.canonical
                for event in events
            )
            if overlaps_prior or surface not in user_text or surface == canonical:
                continue
            spans = [
                (match.start(), match.end())
                for match in re.finditer(re.escape(surface), user_text)
                if not any(match.start() < end and match.end() > start for start, end in occupied_spans)
            ]
            if not spans:
                continue
            replacements.extend((start, end, canonical) for start, end in spans)
            occupied_spans.extend(spans)
            events.append(RewriteEvent(surface, canonical, kind, confidence, attribute_code))
        for start, end, canonical in sorted(replacements, reverse=True):
            user_text = user_text[:start] + canonical + user_text[end:]
        return user_text + context_suffix, events

    @staticmethod
    def _aliases(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except (TypeError, ValueError):
            pass
        return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]

    @staticmethod
    def _best_typo(text: str, target: str) -> tuple[str, float] | None:
        if len(target) < 2 or len(text) < 2:
            return None
        best: tuple[str, float] | None = None
        min_len = max(2, len(target) - 1)
        max_len = min(len(text), len(target) + 1)
        for width in range(min_len, max_len + 1):
            for start in range(0, len(text) - width + 1):
                fragment = text[start:start + width]
                score = SequenceMatcher(None, fragment, target).ratio()
                if best is None or score > best[1]:
                    best = (fragment, score)
        return best

    @staticmethod
    def _single_substitution(left: str, right: str) -> bool:
        """Allow one typo in a four-or-more-character business term.

        Short Chinese strings otherwise receive deceptively low SequenceMatcher
        scores (one wrong character in four is only 0.75).
        """
        return (
            len(left) == len(right)
            and len(right) >= 4
            and sum(a != b for a, b in zip(left, right)) == 1
        )

    @classmethod
    def _safe_typo_replacement(cls, surface: str, canonical: str) -> bool:
        """A fuzzy correction must not invent identifiers or negation."""
        if re.findall(r"\d+(?:[./:-]\d+)*", surface) != re.findall(
            r"\d+(?:[./:-]\d+)*", canonical
        ):
            return False
        return all((term in surface) == (term in canonical) for term in cls._NEGATION_TERMS)

    def _critical_terms_preserved(self, original: str, rewritten: str) -> bool:
        user_part = rewritten.split("\n", 1)[0]
        if not all(term in user_part for term in self._INTENT_TERMS if term in original):
            return False
        if re.findall(r"\d+(?:[./:-]\d+)*", original) != re.findall(
            r"\d+(?:[./:-]\d+)*", user_part
        ):
            return False
        return all(
            original.count(term) == user_part.count(term)
            for term in self._NEGATION_TERMS
        )
