import hashlib
import json
import re
from datetime import date
from difflib import SequenceMatcher
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from pymysql.err import InterfaceError as MySQLInterfaceError
from pymysql.err import OperationalError as MySQLOperationalError

from asl_contract import ASLValidationError
from scope_contract import normalize_domains, require_candidate_scope

from config import (
    API_KEY,
    BASE_URL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
    require_runtime_secret,
)
from prompt_build import PromptBuilder
from mysql_tool import (
    get_registered_entity_attributes,
    get_metric_evidence,
    get_table_field_by_scope,
    load_published_entity_attribute_candidates,
    resolve_entity_attribute_catalog_matches,
    resolve_entity_code_by_table,
    resolve_exact_entity_attribute_value_fields,
    resolve_exact_entity_value_fields,
)


def _normalize_exploration_time_granularity(ast: dict, query: str) -> None:
    """Prevent an exploratory trend query from collapsing into too few time buckets."""
    if re.search(r"按年|逐年|年度|yearly|by\s+year", query, re.IGNORECASE):
        return
    time_context = ast.get("time_context")
    if not isinstance(time_context, dict):
        return
    try:
        start = date.fromisoformat(str(time_context.get("start")))
        end = date.fromisoformat(str(time_context.get("end")))
    except (TypeError, ValueError):
        return
    span_days = (end - start).days + 1
    if span_days <= 0:
        return
    recommended = (
        "day" if span_days <= 45 else
        "month" if span_days <= 730 else
        "quarter" if span_days <= 1460 else
        "year"
    )
    rank = {"day": 0, "month": 1, "quarter": 2, "year": 3}
    changed = False
    for dimension in ast.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        current = dimension.get("granularity")
        if current in rank and rank[current] > rank[recommended]:
            dimension["granularity"] = recommended
            changed = True
    if changed:
        time_context["unit"] = recommended


def _explicit_time_granularity(query: str) -> str | None:
    for pattern, granularity in (
        (r"按小时|每小时|逐小时|小时(?:级|粒度)|时间段", "hour"),
        (r"按日|每日|每天|逐日|日度|按天|天级", "day"),
        (
            r"按月|每月|月度|逐月|"
            r"\d{1,2}月(?:份)?(?:至|到|-)\d{1,2}月(?:份)?",
            "month",
        ),
        (r"按季|每季|季度|逐季", "quarter"),
        (r"按年|每年|年度|逐年", "year"),
    ):
        if re.search(pattern, str(query or ""), re.IGNORECASE):
            return granularity
    return None


def _normalize_explicit_time_granularity(
    content: str,
    knowledge: dict,
    execution_query: str,
) -> str:
    """Make an explicit/caller-required grain authoritative over model output.

    ``retrieval_query`` is intentionally narrower than the execution question,
    so it may omit a follow-up's ``按月`` wording or the orchestrator's bounded
    trend contract.  This pass reads only the grain from the execution text;
    it does not interpret dates or filters, avoiding the negated-time examples
    that semantic recall deliberately excludes.
    """
    granularity = _explicit_time_granularity(execution_query)
    if granularity is None:
        return content
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(ast, dict) or not isinstance(ast.get("dimensions"), list):
        return content

    metric_names = {
        str(item.get("name"))
        for item in ast.get("metrics", [])
        if isinstance(item, dict) and item.get("name")
    }
    candidates: list[dict] = []
    for result in knowledge.get("dimensions", []):
        metadata = getattr(result, "metadata", {}) or {}
        dim_type = str(metadata.get("dim_type") or "").casefold()
        granularities = _json_value(metadata.get("granularity_support")) or []
        if (
            "time" in dim_type
            or "date" in dim_type
            or "时间" in dim_type
            or granularities
        ) and (not granularities or granularity in set(map(str, granularities))):
            candidates.append(metadata)
    if not candidates:
        metric_anchors = {
            str(time_caliber.get("time_anchor"))
            for result in knowledge.get("metrics", [])
            for metadata in [getattr(result, "metadata", {}) or {}]
            for time_caliber in [_json_value(metadata.get("time_caliber")) or {}]
            if isinstance(time_caliber, dict)
            and isinstance(time_caliber.get("time_anchor"), str)
            and _PHYSICAL_FIELD.fullmatch(str(time_caliber.get("time_anchor")))
        }
        if len(metric_anchors) != 1:
            return content
        anchor = next(iter(metric_anchors))
        replaced = False
        for dimension in ast["dimensions"]:
            if not isinstance(dimension, dict) or not dimension.get("granularity"):
                continue
            old_name = dimension.get("name")
            dimension.update({
                "name": anchor,
                "attr": None,
                "level": None,
                "granularity": granularity,
            })
            sort = ast.get("sort")
            if (
                isinstance(sort, dict)
                and sort.get("field_type") == "dimension"
                and sort.get("field") == old_name
            ):
                sort["field"] = anchor
            replaced = True
        if not replaced:
            ast["dimensions"].append({
                "name": anchor, "attr": None, "level": None,
                "granularity": granularity,
            })
        time_context = ast.get("time_context")
        if isinstance(time_context, dict):
            time_context["anchor"] = anchor
            time_context["unit"] = granularity
        ambiguities = ast.get("ambiguity")
        if isinstance(ambiguities, list):
            resolved_types = {"dimension"}
            if "调用方已确认的强制时间范围" in execution_query:
                resolved_types.add("time_anchor")
            ast["ambiguity"] = [
                item for item in ambiguities
                if not isinstance(item, dict)
                or str(item.get("type") or "") not in resolved_types
            ]
        return json.dumps(ast, ensure_ascii=False)

    time_context = ast.get("time_context")
    anchor = (
        str(time_context.get("anchor") or "")
        if isinstance(time_context, dict)
        else ""
    )

    def bindings(metadata: dict) -> list[dict]:
        value = _json_value(metadata.get("bind_entities")) or []
        return [item for item in value if isinstance(item, dict)]

    def binding_field(binding: dict) -> str:
        table = binding.get("mappingTable")
        column = binding.get("mappingColumn")
        return f"{table}.{column}" if table and column else ""

    def select(current_name: str = "") -> dict | None:
        exact = [
            metadata
            for metadata in candidates
            if str(metadata.get("dim_code") or "") == current_name
            or current_name in {binding_field(item) for item in bindings(metadata)}
        ]
        if len(exact) == 1:
            return exact[0]
        anchored = [
            metadata
            for metadata in candidates
            if anchor and anchor in {binding_field(item) for item in bindings(metadata)}
        ]
        if len(anchored) == 1:
            return anchored[0]
        bound = [
            metadata
            for metadata in candidates
            if metric_names.intersection(
                map(str, _json_value(metadata.get("bind_metrics")) or [])
            )
        ]
        if len(bound) == 1:
            return bound[0]
        return candidates[0] if len(candidates) == 1 else None

    normalized_time_dimension = False
    for dimension in ast["dimensions"]:
        if not isinstance(dimension, dict):
            continue
        current_name = str(dimension.get("name") or "")
        selected = select(current_name)
        is_time_projection = bool(
            dimension.get("granularity")
            or any(
                str(metadata.get("dim_code") or "") == current_name
                or current_name in {
                    binding_field(item) for item in bindings(metadata)
                }
                for metadata in candidates
            )
        )
        if selected is None or not is_time_projection:
            continue
        old_name = current_name
        dimension["name"] = str(selected.get("dim_code") or current_name)
        dimension["granularity"] = granularity
        selected_bindings = bindings(selected)
        binding = next(
            (
                item for item in selected_bindings
                if anchor and binding_field(item) == anchor
            ),
            selected_bindings[0] if len(selected_bindings) == 1 else None,
        )
        dimension["attr"] = (
            str(binding.get("attr"))
            if isinstance(binding, dict) and binding.get("attr")
            else None
        )
        sort = ast.get("sort")
        if (
            isinstance(sort, dict)
            and sort.get("field_type") == "dimension"
            and str(sort.get("field") or "") == old_name
        ):
            sort["field"] = dimension["name"]
        normalized_time_dimension = True

    if not normalized_time_dimension:
        selected = select()
        if selected is not None:
            selected_bindings = bindings(selected)
            binding = next(
                (
                    item for item in selected_bindings
                    if anchor and binding_field(item) == anchor
                ),
                selected_bindings[0] if len(selected_bindings) == 1 else None,
            )
            ast["dimensions"].append({
                "name": str(selected.get("dim_code")),
                "attr": (
                    str(binding.get("attr"))
                    if isinstance(binding, dict) and binding.get("attr")
                    else None
                ),
                "level": None,
                "granularity": granularity,
            })
    if isinstance(time_context, dict):
        time_context["unit"] = granularity
    return json.dumps(ast, ensure_ascii=False)


def _strip_code_fence(text: str) -> str:
    """剥离 markdown 代码块包裹与多余空白，返回纯 JSON 文本。

    LLM 经常忽略 prompt 中"不要包裹代码块"的指令，输出形如:
        ```json
        { ... }
        ```
        ````
        { ... }
        ````
    这里统一后处理剥离。
    """
    if not isinstance(text, str):
        return text
    s = text.strip()
    # 匹配开头的 ``` 或 ```` 或 ```json 等
    m = re.match(r"^`{3,}\s*(json)?\s*\n?(.*?)\n?`{3,}\s*$", s, re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(2).strip()
    # Accept a single JSON object preceded by harmless explanatory text.
    # The semantic validator still rejects invented or unsafe content.
    if not s.startswith("{"):
        start = s.find("{")
        if start >= 0:
            try:
                _, end = json.JSONDecoder().raw_decode(s[start:])
            except (TypeError, ValueError):
                pass
            else:
                s = s[start : start + end]
    return s


def _json_value(value):
    """Decode JSON stored in vector metadata while accepting native values."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _canonical_json(value) -> str:
    """Serialize evidence inputs deterministically for stable fingerprints."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_signature(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_formula(value) -> str | None:
    """Normalize SQL/formula metadata without trying to reinterpret it."""
    value = _json_value(value)
    if isinstance(value, (dict, list)):
        return _canonical_json(value) if value else None
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _positive_domain_id(value) -> int | None:
    try:
        domain_id = int(value)
    except (TypeError, ValueError):
        return None
    return domain_id if domain_id > 0 else None


def _metric_vector_metadata(knowledge: dict, metric_code: str):
    """Return the exact retrieved record which authorized an ASL metric code."""
    for result in knowledge.get("metrics", []):
        metadata = getattr(result, "metadata", {}) or {}
        if str(metadata.get("metric_code") or "") == metric_code:
            return result, metadata
    return None, {}


def _choose_sql_metric_row(
    metric_code: str,
    rows: list[dict],
    preferred_domain_id: int | None,
) -> dict:
    """Resolve duplicate metric codes without silently crossing a domain."""
    candidates = [
        row for row in rows
        if str(row.get("indicator_code") or "") == metric_code
    ]
    if preferred_domain_id is not None:
        scoped = [
            row for row in candidates
            if _positive_domain_id(row.get("business_domain_id")) == preferred_domain_id
        ]
        if len(scoped) == 1:
            return scoped[0]
        if len(scoped) > 1:
            candidates = scoped
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(
            f"selected metric is missing from current SQL semantic metadata: {metric_code}"
        )

    # Identical duplicate rows are harmless, but materially different metric
    # definitions under the same code cannot be represented unambiguously by
    # ASL and therefore must not be guessed.
    definitions = {
        _canonical_json({
            "canonical_name": row.get("indicator_name"),
            "business_domain_id": _positive_domain_id(row.get("business_domain_id")),
            "calculation_formula": _normalize_formula(row.get("calculation_formula")),
            "indicator_logic": _normalize_formula(row.get("indicator_logic")),
            "global_filters": _json_value(row.get("global_filters")),
        })
        for row in candidates
    }
    if len(definitions) > 1:
        raise ValueError(
            f"selected metric has ambiguous SQL semantic definitions: {metric_code}"
        )
    return sorted(candidates, key=lambda row: str(row.get("id") or ""))[0]


def _build_semantic_evidence(
    validated_asl: str,
    knowledge: dict,
    semantic_model_id: int,
    requested_business_domain_ids: list[int] | tuple[int, ...] | None,
    sql_loader=None,
) -> dict:
    """Build Oagnet-owned provenance for the exact validated ASL response.

    SQL semantic metadata is authoritative when reachable. A vector fallback
    is permitted only for a dependency outage and is marked ``sql_verified``
    false. If SQL is reachable but no longer contains a selected vector metric,
    the vector snapshot is stale and the request is rejected.
    """
    ast = json.loads(validated_asl)
    sql_loader = sql_loader or get_metric_evidence
    selected_codes = [
        str(metric["name"])
        for metric in ast.get("metrics", [])
        if isinstance(metric, dict) and metric.get("name")
    ]
    requested_domains = normalize_domains(business_domain_ids=requested_business_domain_ids)

    sql_rows: list[dict] = []
    sql_available = True
    if selected_codes:
        try:
            sql_rows = list(sql_loader(
                semantic_model_id,
                selected_codes,
                requested_domains or None,
            ))
        except (
            ConnectionError,
            TimeoutError,
            OSError,
            RuntimeError,
            MySQLInterfaceError,
            MySQLOperationalError,
        ) as exc:
            sql_available = False
            logger.warning(
                "metric evidence SQL lookup unavailable; using marked vector fallback: "
                "sm=%s, metric_count=%s, error_type=%s",
                semantic_model_id,
                len(selected_codes),
                type(exc).__name__,
            )

    selected_metrics: list[dict] = []
    resolved_domains: list[int] = []
    for metric_code in selected_codes:
        vector_result, vector_meta = _metric_vector_metadata(knowledge, metric_code)
        if vector_result is None:
            # This should already be impossible after _validate_asl_output, but
            # keep the evidence builder independently fail-closed.
            raise ValueError(
                f"selected metric has no retrieved semantic evidence: {metric_code}"
            )
        vector_domain_id = _positive_domain_id(
            vector_meta.get("business_domain_id")
            or vector_meta.get("business_domain")
        )
        require_candidate_scope(vector_meta, semantic_model_id, requested_domains)

        if sql_available:
            row = _choose_sql_metric_row(metric_code, sql_rows, vector_domain_id)
            require_candidate_scope(row, semantic_model_id, requested_domains)
            canonical_name = str(row.get("indicator_name") or "").strip()
            domain_id = _positive_domain_id(row.get("business_domain_id"))
            formula = _normalize_formula(row.get("calculation_formula"))
            formula_source = "calculation_formula"
            if formula is None:
                formula = _normalize_formula(row.get("indicator_logic"))
                formula_source = "indicator_logic"
            global_filters = _json_value(row.get("global_filters"))
            metadata_source = "MYSQL_SEMANTIC_LAYER"
            sql_verified = True
        else:
            canonical_name = str(vector_meta.get("metric_name") or "").strip()
            domain_id = vector_domain_id
            calculation_rule = _json_value(vector_meta.get("calculation_rule")) or {}
            formula = (
                _normalize_formula(calculation_rule.get("calc_formula"))
                if isinstance(calculation_rule, dict)
                else _normalize_formula(calculation_rule)
            )
            formula_source = "vector_calculation_rule"
            global_filters = (
                _json_value(calculation_rule.get("global_filters"))
                if isinstance(calculation_rule, dict)
                else None
            )
            metadata_source = "VECTOR_INDEX_FALLBACK"
            sql_verified = False

        if not canonical_name:
            raise ValueError(
                f"selected metric is missing a canonical name in semantic metadata: {metric_code}"
            )
        if formula is None:
            raise ValueError(
                f"selected metric is missing calculation formula/logic: {metric_code}"
            )
        if (
            requested_domains
            and domain_id not in requested_domains
        ):
            raise ValueError(
                f"selected metric resolved outside requested business domains: {metric_code}"
            )
        if domain_id is not None and domain_id not in resolved_domains:
            resolved_domains.append(domain_id)

        stable_metadata = {
            "semantic_model_id": semantic_model_id,
            "business_domain_id": domain_id,
            "canonical_code": metric_code,
            "canonical_name": canonical_name,
            "calculation_formula": formula,
            "global_filters": global_filters,
        }
        selected_metrics.append({
            **stable_metadata,
            "formula_source": formula_source,
            "formula_signature": _sha256_signature(formula),
            "metadata_fingerprint": _sha256_signature(
                _canonical_json(stable_metadata)
            ),
            "metadata_source": metadata_source,
            "sql_verified": sql_verified,
            "retrieval_record_id": str(getattr(vector_result, "id", "")),
            "retrieval_score": float(getattr(vector_result, "score", 0.0)),
        })

    if not selected_metrics:
        # Clarification ASL has no selected metric. Preserve an explicit caller
        # scope, but never invent an AUTO-mode domain resolution.
        resolved_domains = requested_domains.copy()

    evidence = {
        "evidence_version": "1.0",
        "producer": "OAGNET",
        "semantic_model_id": semantic_model_id,
        "requested_business_domain_ids": requested_domains,
        "resolved_business_domain_ids": resolved_domains,
        "selected_metrics": selected_metrics,
        "asl_signature": _sha256_signature(validated_asl),
    }
    evidence["evidence_fingerprint"] = _sha256_signature(
        _canonical_json(evidence)
    )
    return evidence


def _normalize_semantic_references(
    content: str,
    knowledge: dict,
    user_query: str = "",
    semantic_model_id: int | None = None,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    exact_value_resolver=None,
    exact_attribute_value_resolver=None,
    catalog_value_resolver=None,
) -> str:
    """Correct uniquely resolvable semantic/physical references from recall."""
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(ast, dict) or not isinstance(ast.get("dimensions"), list):
        return content

    metric_names = {
        str(item.get("name"))
        for item in ast.get("metrics", [])
        if isinstance(item, dict) and item.get("name")
    }
    _normalize_subject_reference(ast, knowledge)
    known_dimension_codes = _known_codes(knowledge, "dimensions", "dim_code")
    attribute_fields = _known_attribute_field_mappings(knowledge)
    for dimension in ast["dimensions"]:
        if not isinstance(dimension, dict):
            continue
        old_name = str(dimension.get("name") or "").strip()
        # Models occasionally emit the recalled attr_code (for example
        # ``dept_name``) although ASL requires its physical table.column.  A
        # correction is safe only when the recalled, authorized scope contains
        # exactly one physical mapping.  Ambiguous or absent mappings are left
        # untouched for the validator to reject.
        if old_name and "." not in old_name and old_name not in known_dimension_codes:
            candidates = attribute_fields.get(old_name, set())
            if len(candidates) == 1:
                dimension["name"] = next(iter(candidates))
                dimension["attr"] = None
                sort = ast.get("sort")
                if (
                    isinstance(sort, dict)
                    and sort.get("field_type") == "dimension"
                    and str(sort.get("field") or "") == old_name
                ):
                    sort["field"] = dimension["name"]
    _normalize_unretrieved_contact_dimensions(ast, knowledge, user_query)
    _normalize_unretrieved_dimensions(ast, knowledge, user_query)
    # The subject is another semantic identifier and must come from the metric
    # dependency rather than from a shortened name invented by the model.
    for result in knowledge.get("metrics", []):
        metadata = getattr(result, "metadata", {}) or {}
        if str(metadata.get("metric_code")) not in metric_names:
            continue
        dependency = _json_value(metadata.get("source_dependency")) or {}
        entities = dependency.get("bind_entity") if isinstance(dependency, dict) else None
        entities = _json_value(entities) or []
        if entities:
            subject = ast.setdefault("subject", {})
            if isinstance(subject, dict):
                subject["entity"] = str(entities[0])
        break
    time_context = ast.get("time_context") or {}
    metric_time_anchors: set[str] = set()
    for result in knowledge.get("metrics", []):
        metadata = getattr(result, "metadata", {}) or {}
        if str(metadata.get("metric_code")) not in metric_names:
            continue
        time_caliber = _json_value(metadata.get("time_caliber")) or {}
        metric_anchor = (
            time_caliber.get("time_anchor")
            if isinstance(time_caliber, dict)
            else None
        )
        if isinstance(metric_anchor, str) and _PHYSICAL_FIELD.fullmatch(metric_anchor):
            metric_time_anchors.add(metric_anchor)
    # A single canonical metric anchor is authoritative. This prevents the
    # model from turning a semantic date name such as dim_date into a physical
    # column that does not exist.
    if isinstance(time_context, dict) and len(metric_time_anchors) == 1:
        time_context["anchor"] = next(iter(metric_time_anchors))
    anchor = time_context.get("anchor") if isinstance(time_context, dict) else None
    anchor_table = anchor.split(".", 1)[0] if isinstance(anchor, str) and "." in anchor else None
    query_granularity = _explicit_time_granularity(user_query)
    candidates = []
    for result in knowledge.get("dimensions", []):
        metadata = getattr(result, "metadata", {}) or {}
        dim_type = str(metadata.get("dim_type") or "").lower()
        granularities = _json_value(metadata.get("granularity_support")) or []
        if "time" in dim_type or "date" in dim_type or "\u65f6\u95f4" in dim_type or granularities:
            candidates.append(metadata)

    for dimension in ast["dimensions"]:
        if not isinstance(dimension, dict) or not dimension.get("granularity"):
            continue
        current_name = str(dimension.get("name") or "")
        selected = next((m for m in candidates if str(m.get("dim_code")) == current_name), None)
        if selected is None:
            selected = next(
                (m for m in candidates if metric_names.intersection(
                    map(str, _json_value(m.get("bind_metrics")) or [])
                )),
                candidates[0] if len(candidates) == 1 else None,
            )
        if selected is None:
            continue

        bindings = _json_value(selected.get("bind_entities")) or []
        binding = next(
            (item for item in bindings if isinstance(item, dict) and anchor_table
             and item.get("mappingTable") == anchor_table),
            bindings[0] if len(bindings) == 1 else None,
        )
        old_name = dimension.get("name")
        dimension["name"] = selected.get("dim_code")
        if query_granularity:
            dimension["granularity"] = query_granularity
            if isinstance(time_context, dict):
                time_context["unit"] = query_granularity
        dimension["attr"] = (
            str(binding.get("attr"))
            if isinstance(binding, dict) and binding.get("attr") else None
        )
        sort = ast.get("sort")
        if (isinstance(sort, dict) and sort.get("field_type") == "dimension"
                and sort.get("field") == old_name):
            sort["field"] = dimension["name"]
    _normalize_explicit_grouping_dimensions(ast, knowledge, user_query)
    _dedupe_equivalent_dimensions(ast, knowledge)
    _normalize_grouping_quantifier_filters(ast, knowledge, user_query)
    _normalize_unretrieved_literal_filters(
        ast,
        knowledge,
        user_query,
        semantic_model_id=semantic_model_id,
        business_domain_id=business_domain_id,
        exact_attribute_value_resolver=exact_attribute_value_resolver,
    )
    _normalize_relation_name_filters(
        ast,
        knowledge,
        user_query,
        semantic_model_id=semantic_model_id,
        business_domain_id=business_domain_id,
        exact_value_resolver=exact_value_resolver,
        exact_attribute_value_resolver=exact_attribute_value_resolver,
        catalog_value_resolver=catalog_value_resolver,
    )
    _split_composite_brand_filter(
        ast,
        knowledge,
        user_query,
        semantic_model_id=semantic_model_id,
        business_domain_id=business_domain_id,
        catalog_value_resolver=catalog_value_resolver,
    )
    _normalize_entity_attribute_filters(
        ast,
        knowledge,
        user_query,
        semantic_model_id=semantic_model_id,
        business_domain_id=business_domain_id,
        exact_attribute_value_resolver=exact_attribute_value_resolver,
        catalog_value_resolver=catalog_value_resolver,
    )
    _normalize_time_context(ast, knowledge, user_query)
    _normalize_activity_semantics(ast, knowledge, user_query)
    _normalize_generic_sales_metric(ast, knowledge, user_query)
    return json.dumps(ast, ensure_ascii=False)


def _known_codes(knowledge: dict, section: str, key: str) -> set[str]:
    return {
        str((getattr(item, "metadata", {}) or {}).get(key))
        for item in knowledge.get(section, [])
        if (getattr(item, "metadata", {}) or {}).get(key)
    }


def _known_subject_codes(knowledge: dict, selected_metrics: set[str]) -> set[str]:
    codes = _known_codes(knowledge, "entities", "entity_code")
    for item in knowledge.get("metrics", []):
        metadata = getattr(item, "metadata", {}) or {}
        if str(metadata.get("metric_code")) not in selected_metrics:
            continue
        dependency = _json_value(metadata.get("source_dependency")) or {}
        values = dependency.get("bind_entity") if isinstance(dependency, dict) else []
        for value in _json_value(values) or []:
            if value:
                codes.add(str(value))
    codes.update(map(str, knowledge.get("_resolved_subjects", [])))
    return codes


def _entity_reference_labels(metadata: dict) -> set[str]:
    """Return deterministic labels that may identify one recalled entity.

    Besides the registered code/name/aliases, accept the conventional
    ``ent_``/``entity_`` wrapper sometimes emitted by an LLM.  The caller still
    requires the resulting label to identify exactly one recalled entity, so a
    shorthand can never widen the semantic scope.
    """
    labels: set[str] = set()
    for value in (
        metadata.get("entity_code"),
        metadata.get("entity_name"),
        *PromptBuilder._term_values(metadata.get("entity_alias")),
    ):
        label = _semantic_label(value)
        if not label:
            continue
        labels.add(label)
        for prefix in ("entity", "ent"):
            if label.startswith(prefix) and len(label) > len(prefix):
                labels.add(label[len(prefix):])
    code_tokens = re.split(
        r"[^0-9A-Za-z]+", str(metadata.get("entity_code") or "").casefold()
    )
    for marker in ("entity", "ent"):
        if marker in code_tokens:
            suffix = _semantic_label("".join(code_tokens[code_tokens.index(marker) + 1:]))
            if suffix:
                labels.add(suffix)
    return labels


def _normalize_subject_reference(ast: dict, knowledge: dict) -> None:
    """Resolve an LLM subject alias only when recall proves one entity."""
    subject = ast.get("subject")
    if not isinstance(subject, dict):
        return
    raw = str(subject.get("entity") or "").strip()
    if not raw:
        return
    known = _known_codes(knowledge, "entities", "entity_code")
    if raw in known:
        return
    raw_labels = {_semantic_label(raw)}
    for prefix in ("entity", "ent"):
        label = _semantic_label(raw)
        if label.startswith(prefix) and len(label) > len(prefix):
            raw_labels.add(label[len(prefix):])
    matches = {
        str((getattr(item, "metadata", {}) or {}).get("entity_code"))
        for item in knowledge.get("entities", [])
        if (getattr(item, "metadata", {}) or {}).get("entity_code")
        and raw_labels.intersection(
            _entity_reference_labels(getattr(item, "metadata", {}) or {})
        )
    }
    if len(matches) != 1:
        return
    resolved = next(iter(matches))
    subject["entity"] = resolved
    values = knowledge.setdefault("_resolved_subjects", [])
    if resolved not in values:
        values.append(resolved)


def _physical_field_from_mapping(value) -> str | None:
    value = _json_value(value)
    if isinstance(value, dict):
        table = value.get("mappingTable") or value.get("table")
        column = value.get("mappingColumn") or value.get("column")
        value = f"{table}.{column}" if table and column else None
    if not isinstance(value, str):
        return None
    field = value.strip()
    return field if _PHYSICAL_FIELD.fullmatch(field) else None


def _known_physical_fields(knowledge: dict) -> set[str]:
    """Collect table.column mappings from the recalled semantic metadata."""
    fields: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str) and _PHYSICAL_FIELD.fullmatch(value.strip()):
            fields.add(value.strip())

    # A bounded repair may consult the authoritative current SQL catalog when
    # vector top-k omitted an otherwise published field.  These fields are
    # request-local evidence populated only after metadata/source validation;
    # they are not accepted from the model or caller.
    for value in knowledge.get("_published_authorized_fields", []):
        add(value)

    for item in knowledge.get("entities", []):
        metadata = getattr(item, "metadata", {}) or {}
        attributes = _json_value(metadata.get("attributes")) or []
        for attribute in attributes if isinstance(attributes, list) else []:
            if not isinstance(attribute, dict):
                continue
            add(_physical_field_from_mapping(attribute.get("field_mapping")))

    # Standalone attribute vectors are direct semantic evidence.  They used to
    # be indexed but never queried, making field authorization accidentally
    # depend on whether their whole parent entity survived entity top-k.
    for item in knowledge.get("attributes", []):
        metadata = getattr(item, "metadata", {}) or {}
        add(_physical_field_from_mapping(metadata.get("field_mapping")))

    # Only relation records selected by relational completion are present here.
    # Their join keys are necessary to execute the recalled path, but unrelated
    # fields from completed entities remain absent and therefore unauthorized.
    for item in knowledge.get("relations", []):
        metadata = getattr(item, "metadata", {}) or {}
        join_key = _json_value(metadata.get("join_key")) or {}
        if not isinstance(join_key, dict):
            continue
        add(_physical_field_from_mapping(join_key.get("source_field")))
        add(_physical_field_from_mapping(join_key.get("target_field")))

    for item in knowledge.get("metrics", []):
        metadata = getattr(item, "metadata", {}) or {}
        calculation = _json_value(metadata.get("calculation_rule")) or {}
        formula = calculation.get("calc_formula") if isinstance(calculation, dict) else None
        for value in re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
            str(formula or ""),
        ):
            add(value)
        time_caliber = _json_value(metadata.get("time_caliber")) or {}
        if isinstance(time_caliber, dict):
            add(time_caliber.get("time_anchor"))

    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        # A published dimension may own its physical mapping without repeating
        # it as an entity attribute.  Treat every current dimension mapping as
        # first-class authorization; otherwise a recalled field such as
        # ``dim_city.city_name`` appears in candidates but is then incorrectly
        # rejected as "unregistered".
        field_mapping = _json_value(metadata.get("field_mapping")) or {}
        if isinstance(field_mapping, str):
            add(_physical_field_from_mapping(field_mapping))
        elif isinstance(field_mapping, dict):
            for key in (
                "fact_table_field",
                "dim_table_field",
                "factTableField",
                "dimTableField",
            ):
                add(_physical_field_from_mapping(field_mapping.get(key)))
        attribute_mappings = (
            _json_value(metadata.get("attribute_mappings")) or []
        )
        for attribute in (
            attribute_mappings
            if isinstance(attribute_mappings, list)
            else []
        ):
            if not isinstance(attribute, dict):
                continue
            add(_physical_field_from_mapping(attribute.get("field_path")))
            table = (
                attribute.get("mapping_table")
                or attribute.get("mappingTable")
            )
            column = (
                attribute.get("mapping_column")
                or attribute.get("mappingColumn")
            )
            add(f"{table}.{column}" if table and column else None)
        bindings = _json_value(metadata.get("bind_entities")) or []
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict):
                continue
            table = binding.get("mappingTable") or binding.get("mapping_table")
            column = binding.get("mappingColumn") or binding.get("mapping_column")
            add(f"{table}.{column}" if table and column else None)
    return fields


def _known_attribute_field_mappings(knowledge: dict) -> dict[str, set[str]]:
    """Map recalled attribute codes/columns to authorized physical fields."""
    result: dict[str, set[str]] = {}

    def register(attribute: dict) -> None:
        field = _physical_field_from_mapping(attribute.get("field_mapping"))
        if field is None:
            return
        aliases = {
            str(attribute.get("attr_code") or "").strip(),
            field.rsplit(".", 1)[-1],
        }
        for alias in aliases - {""}:
            result.setdefault(alias, set()).add(field)

    for item in knowledge.get("entities", []):
        metadata = getattr(item, "metadata", {}) or {}
        attributes = _json_value(metadata.get("attributes")) or []
        for attribute in attributes if isinstance(attributes, list) else []:
            if isinstance(attribute, dict):
                register(attribute)
    for item in knowledge.get("attributes", []):
        metadata = getattr(item, "metadata", {}) or {}
        register(metadata)

    authorized = _known_physical_fields(knowledge)
    return {
        alias: fields.intersection(authorized)
        for alias, fields in result.items()
        if fields.intersection(authorized)
    }


_CONTACT_REQUEST = re.compile(
    r"联系方式|联系信息|联系电话|联系人|电话|手机|邮箱|电子邮箱|联系地址|"
    r"contact(?:\s*info(?:rmation)?)?|phone|mobile|telephone|e-?mail|address",
    re.IGNORECASE,
)
_CONTACT_ATTRIBUTE = re.compile(
    r"联系|电话|手机|邮箱|电子邮箱|地址|phone|mobile|telephone|tel|"
    r"e-?mail|address|contact",
    re.IGNORECASE,
)


def _normalize_unretrieved_contact_dimensions(
    ast: dict,
    knowledge: dict,
    user_query: str,
) -> None:
    """Replace a hallucinated contact projection with recalled contact fields.

    A generic request such as ``联系方式`` legitimately expands to several
    registered attributes (for example email and address).  The rewrite is
    metadata-driven: no physical field is embedded here, every output field is
    already authorized by the current retrieval scope, and only fields on the
    nearest requested/result entity are retained.  If that bounded choice is
    unavailable, the invalid projection becomes a clarification instead of a
    502 validation failure.
    """
    dimensions = ast.get("dimensions")
    ambiguities = ast.get("ambiguity")
    if (
        not isinstance(dimensions, list)
        or not isinstance(ambiguities, list)
        or not _CONTACT_REQUEST.search(str(user_query or ""))
    ):
        return
    authorized_fields = _known_physical_fields(knowledge)
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    if not authorized_fields or not attributes_by_entity:
        return

    contact_fields: dict[str, str] = {}
    field_owners: dict[str, str] = {}
    for entity_code, attributes in attributes_by_entity.items():
        for field, attribute in attributes.items():
            if field not in authorized_fields or _is_relationship_key_field(
                field, attributes_by_entity
            ):
                continue
            semantic_text = " ".join(str(attribute.get(key) or "") for key in (
                "attr_code", "attr_name", "description", "semantic_role",
            ))
            if _CONTACT_ATTRIBUTE.search(semantic_text):
                contact_fields[field] = semantic_text
                field_owners[field] = entity_code
    if not contact_fields:
        return

    fallback_entity = next(iter(entities), "")
    distances = _result_entity_distances(
        ast, knowledge, attributes_by_entity, fallback_entity
    )
    best_distance = min(
        distances.get(field_owners[field], 10_000) for field in contact_fields
    )
    nearest = sorted(
        field
        for field in contact_fields
        if distances.get(field_owners[field], 10_000) == best_distance
    )
    # More than four equally near contact fields is not a useful deterministic
    # projection; require the caller to specify which contact attributes.
    selected = nearest if 0 < len(nearest) <= 4 else []
    already_selected = {
        str(item.get("name") or "")
        for item in dimensions
        if isinstance(item, dict)
        and str(item.get("name") or "") in authorized_fields
    }
    normalized: list[dict] = []
    replaced = False
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            normalized.append(dimension)
            continue
        old_name = str(dimension.get("name") or "").strip()
        if old_name in authorized_fields or "." not in old_name:
            normalized.append(dimension)
            continue
        old_semantics = old_name.rsplit(".", 1)[-1].replace("_", " ")
        if not _CONTACT_ATTRIBUTE.search(old_semantics):
            normalized.append(dimension)
            continue
        replaced = True
        for field in selected:
            if field in already_selected:
                continue
            normalized.append({
                "name": field,
                "attr": None,
                "level": None,
                "granularity": None,
            })
            already_selected.add(field)
        sort = ast.get("sort")
        if (
            selected
            and isinstance(sort, dict)
            and str(sort.get("field") or "") == old_name
        ):
            sort["field"] = selected[0]
            sort["field_type"] = "field"

    if not replaced:
        return
    ast["dimensions"] = normalized
    if not selected:
        _append_ambiguity(
            ast,
            "dimension",
            "“联系方式”对应多个同等相关的已注册属性，请明确需要的联系字段。",
            nearest,
        )


_NAME_LIKE_DIMENSION = re.compile(
    r"name|title|label|company|profile|dealer|distributor|supplier|vendor|"
    r"名称|姓名|标题|公司|画像|经销商|供应商|医院|商品|产品|科室",
    re.IGNORECASE,
)

_QUALIFIED_NAME_DIMENSION = re.compile(
    r"(?<![a-z0-9])(?:parent|mother|short|abbr(?:eviation)?|legal|registered|"
    r"official|standard|full)(?![a-z0-9])|"
    r"母公司|母集团|上级公司|集团母公司|简称|缩写|法定(?:名称)?|"
    r"法律名称|注册名称|标准名称|全称",
    re.IGNORECASE,
)


def _normalize_unretrieved_dimensions(
    ast: dict,
    knowledge: dict,
    user_query: str,
) -> None:
    """Resolve hallucinated result labels to one recalled main attribute.

    This is deliberately narrower than fuzzy field matching.  A replacement
    must be an already authorized non-key attribute and have deterministic
    identity evidence from the emitted table/column, entity role, or explicit
    metadata wording.  Relation distance from the metric subject/result entity
    breaks ties.  Unresolved dimensions are removed from executable shape and
    surfaced as clarification, preventing a schema-validation 502 without
    silently substituting an unrelated field.
    """
    dimensions = ast.get("dimensions")
    if not isinstance(dimensions, list) or not isinstance(ast.get("ambiguity"), list):
        return
    authorized_fields = _known_physical_fields(knowledge)
    known_dimension_codes = _known_codes(knowledge, "dimensions", "dim_code")
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)

    fallback_entity = next(iter(entities), next(iter(attributes_by_entity), ""))
    distances = _result_entity_distances(
        ast, knowledge, attributes_by_entity, fallback_entity
    )
    already_selected = {
        str(item.get("name") or "")
        for item in dimensions
        if isinstance(item, dict)
        and (
            str(item.get("name") or "") in authorized_fields
            or str(item.get("name") or "") in known_dimension_codes
        )
    }

    def labels(metadata: dict, entity_code: str) -> set[str]:
        values = [entity_code, metadata.get("entity_name")]
        values.extend(PromptBuilder._term_values(metadata.get("entity_alias")))
        result: set[str] = set()
        for value in values:
            normalized = _semantic_label(value)
            if len(normalized) >= 2:
                result.add(normalized)
            for token in re.split(r"[_\W]+", str(value or "")):
                normalized_token = _semantic_label(token)
                if len(normalized_token) >= 3:
                    result.add(normalized_token)
        return result

    normalized_dimensions: list = []
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            normalized_dimensions.append(dimension)
            continue
        old_name = str(dimension.get("name") or "").strip()
        if old_name in authorized_fields or old_name in known_dimension_codes:
            normalized_dimensions.append(dimension)
            continue
        # Bare identifiers are handled by the exact attr_code mapping pass
        # above.  If that pass left one unresolved, its global mapping is
        # ambiguous and an entity-local fuzzy rewrite would be unsafe.
        if "." not in old_name:
            normalized_dimensions.append(dimension)
            continue
        # Registered time-dimension normalization runs later and owns all
        # granularity-bearing projections.
        if dimension.get("granularity"):
            normalized_dimensions.append(dimension)
            continue

        old_table, _, old_column = old_name.partition(".")
        old_text = " ".join((
            old_column.replace("_", " "),
            str(dimension.get("alias") or ""),
        ))
        old_label = _semantic_label(" ".join((old_name, old_text)))
        name_like = bool(_NAME_LIKE_DIMENSION.search(old_text))
        # A qualified name is a distinct business attribute, not a generic
        # display label.  For example ``parent_company_name`` must never be
        # rewritten to a dealer's main ``dealer_name`` merely because both end
        # in ``name``.  Exact recalled attr/column evidence below may still map
        # it; only the generic main-name fallbacks are disabled.
        generic_display_name = name_like and not bool(
            _QUALIFIED_NAME_DIMENSION.search(old_text)
        )
        candidates: list[tuple[tuple[int, int, int, int], str]] = []
        clarification_candidates: list[tuple[int, str]] = []
        for entity_code, attributes in attributes_by_entity.items():
            entity = entities.get(entity_code, {})
            owner_distance = distances.get(entity_code, 10_000)
            entity_labels = labels(entity, entity_code)
            query_has_entity = any(
                label in _semantic_label(user_query) for label in entity_labels
            )
            old_has_entity = any(label in old_label for label in entity_labels)
            for field, attribute in attributes.items():
                if field not in authorized_fields or _is_relationship_key_field(
                    field, attributes_by_entity
                ):
                    continue
                candidate_table, candidate_column = field.split(".", 1)
                attr_code = _semantic_label(attribute.get("attr_code"))
                attr_name = _semantic_label(attribute.get("attr_name"))
                candidate_column_label = _semantic_label(candidate_column)
                is_main = bool(
                    attribute.get("is_main_attribute")
                    or attribute.get("is_primary_name")
                    or attribute.get("is_display_name")
                )
                clarification_candidates.append((owner_distance, field))

                identity_rank: int | None = None
                if _semantic_label(old_column) in {
                    attr_code, candidate_column_label,
                } - {""}:
                    identity_rank = 0
                elif (
                    old_table
                    and old_table == candidate_table
                    and is_main
                    and generic_display_name
                ):
                    identity_rank = 1
                elif generic_display_name and any(
                    label and len(label) >= 3 and label in old_label
                    for label in (attr_code, attr_name, candidate_column_label)
                ):
                    identity_rank = 2
                elif generic_display_name and is_main and old_has_entity:
                    identity_rank = 3
                elif generic_display_name and is_main and query_has_entity:
                    identity_rank = 4
                if identity_rank is None:
                    continue
                candidates.append((
                    (
                        identity_rank,
                        owner_distance,
                        0 if is_main else 1,
                        -_attribute_query_match_strength(attribute, user_query),
                    ),
                    field,
                ))

        best_fields: set[str] = set()
        if candidates:
            best_score = min(score for score, _field in candidates)
            best_fields = {
                field for score, field in candidates if score == best_score
            }
        if len(best_fields) == 1:
            matched_field = next(iter(best_fields))
            if matched_field not in already_selected:
                replacement = dict(dimension)
                replacement.update({
                    "name": matched_field,
                    "attr": None,
                    "level": None,
                    "granularity": None,
                })
                normalized_dimensions.append(replacement)
                already_selected.add(matched_field)
            sort = ast.get("sort")
            if (
                isinstance(sort, dict)
                and sort.get("field_type") in {"dimension", "field"}
                and str(sort.get("field") or "") == old_name
            ):
                sort["field"] = matched_field
                sort["field_type"] = "dimension"
            continue

        nearest_distance = min(
            (distance for distance, _field in clarification_candidates),
            default=10_000,
        )
        choices = sorted({
            field
            for distance, field in clarification_candidates
            if distance == nearest_distance
        })[:12]
        _append_ambiguity(
            ast,
            "dimension",
            (
                f"结果字段 {old_name} 未在当前语义召回中注册，且不能按请求对象"
                "唯一映射到规范属性，请确认需要展示的字段。"
            ),
            sorted(best_fields) or choices,
        )
        sort = ast.get("sort")
        if (
            isinstance(sort, dict)
            and str(sort.get("field") or "") == old_name
        ):
            ast["sort"] = None
    ast["dimensions"] = normalized_dimensions


def _known_dimension_attrs(knowledge: dict) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        code = metadata.get("dim_code")
        if not code:
            continue
        attrs = result.setdefault(str(code), set())
        bindings = _json_value(metadata.get("bind_entities")) or []
        for binding in bindings if isinstance(bindings, list) else []:
            if isinstance(binding, dict) and binding.get("attr"):
                attrs.add(str(binding["attr"]))
    return result


_PHYSICAL_FIELD = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)
_SAFE_HAVING = re.compile(
    r"^(?:COUNT|SUM|AVG|MIN|MAX)\(\s*(?:DISTINCT\s+)?"
    r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\s*\)"
    r"\s*(?:=|!=|>=|<=|>|<)\s*-?(?:\d+(?:\.\d+)?|\.\d+)\s*$",
    re.IGNORECASE,
)
_CODE_LIKE_VALUE = re.compile(
    r"^[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*$"
)
_KEY_TOKEN = re.compile(
    r"(?:^|_)(?:code|id|key|no|number)(?:$|_)",
    re.IGNORECASE,
)


def _semantic_label(value) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _scoped_entity_attributes(
    knowledge: dict,
) -> tuple[dict[str, dict], dict[str, dict[str, dict]]]:
    """Return only entity attributes already exposed by the current scope."""
    entities: dict[str, dict] = {}
    attributes: dict[str, dict[str, dict]] = {}
    for item in knowledge.get("entities", []):
        metadata = getattr(item, "metadata", {}) or {}
        entity_code = str(metadata.get("entity_code") or "").strip()
        if not entity_code:
            continue
        entities[entity_code] = metadata
        for attribute in _json_value(metadata.get("attributes")) or []:
            if not isinstance(attribute, dict):
                continue
            field = _physical_field_from_mapping(attribute.get("field_mapping"))
            if field:
                attributes.setdefault(entity_code, {})[field] = attribute
    for item in knowledge.get("attributes", []):
        metadata = getattr(item, "metadata", {}) or {}
        entity_code = str(metadata.get("parent") or "").strip()
        field = _physical_field_from_mapping(metadata.get("field_mapping"))
        if entity_code and field:
            existing = attributes.setdefault(entity_code, {}).get(field, {})
            attributes[entity_code][field] = {**existing, **metadata}
    return entities, attributes


def _is_relationship_key_field(
    field: str,
    attributes_by_entity: dict[str, dict[str, dict]],
) -> bool:
    column = field.rsplit(".", 1)[-1]
    evidence = [column]
    for entity_attributes in attributes_by_entity.values():
        attribute = entity_attributes.get(field)
        if not attribute:
            continue
        # Uniqueness is a cardinality constraint, not key semantics.  Display
        # names are commonly unique (for example ``department.dept_name``) and
        # must remain valid targets for natural-language filters.  A physical
        # key still has to be declared primary or carry explicit key/code/id
        # metadata below.
        if attribute.get("is_primary_key"):
            return True
        evidence.extend([
            attribute.get("attr_code"),
            attribute.get("attr_name"),
            attribute.get("description"),
        ])
    normalized = " ".join(str(value or "") for value in evidence)
    return bool(
        _KEY_TOKEN.search(column)
        or re.search(r"编码|编号|标识|主键", normalized, re.IGNORECASE)
    )


def _primary_name_candidates(
    entity_codes: set[str],
    entities: dict[str, dict],
    attributes_by_entity: dict[str, dict[str, dict]],
    authorized_fields: set[str],
) -> set[tuple[str, str]]:
    """Find a unique semantic display-name field without guessing an entity."""
    strong: set[tuple[str, str]] = set()
    weak: set[tuple[str, str]] = set()
    for entity_code in entity_codes:
        entity = entities.get(entity_code, {})
        entity_name = _semantic_label(entity.get("entity_name"))
        entity_code_label = _semantic_label(entity_code)
        for field, attribute in attributes_by_entity.get(entity_code, {}).items():
            if field not in authorized_fields:
                continue
            attr_code = str(attribute.get("attr_code") or "").strip()
            attr_name = _semantic_label(attribute.get("attr_name"))
            column = field.rsplit(".", 1)[-1]
            semantic_role = str(
                attribute.get("semantic_role")
                or attribute.get("attribute_role")
                or ""
            ).casefold()
            explicitly_named = any(
                bool(attribute.get(flag))
                for flag in (
                    "is_main_attribute",
                    "is_primary_name",
                    "is_display_name",
                    "is_name",
                )
            ) or semantic_role in {"name", "display_name", "primary_name", "title"}
            label_matches_entity = bool(
                entity_name
                and attr_name in {
                    entity_name + suffix
                    for suffix in ("名称", "姓名", "标题", "name", "title")
                }
            )
            code_matches_entity = bool(
                entity_code_label
                and _semantic_label(attr_code) in {
                    entity_code_label + "name",
                    entity_code_label + "title",
                }
            )
            generic_name = attr_name in {"名称", "姓名", "标题", "name", "title", "label"}
            if explicitly_named or label_matches_entity or code_matches_entity or generic_name:
                strong.add((entity_code, field))
            elif (
                attr_code.casefold().endswith(("_name", "_title"))
                or column.casefold().endswith(("_name", "_title"))
            ):
                weak.add((entity_code, field))
    return strong if strong else weak


def _value_is_obviously_not_code(value, user_query: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip().strip("%")
    if len(text) < 2 or _CODE_LIKE_VALUE.fullmatch(text):
        return False
    if text.casefold() not in str(user_query or "").casefold():
        return False
    return any(ord(char) > 127 or char.isspace() for char in text)


def _natural_name_filter_values(
    operator: str,
    value,
    user_query: str,
) -> list[str]:
    """Return user-mentioned name literals that cannot safely target a key."""
    operator = str(operator or "").upper()
    values = value if operator in {"IN", "NOT IN"} else [value]
    if operator not in {"=", "!=", "LIKE", "IN", "NOT IN"}:
        return []
    if not isinstance(values, list) or not values:
        return []
    result: list[str] = []
    for literal in values:
        if not _value_is_obviously_not_code(literal, user_query):
            return []
        result.append(str(literal).strip().strip("%"))
    return result


def _ambiguity_mentions_filter(
    ambiguity: dict,
    field: str,
    value,
    candidate_fields: set[str] | None = None,
    *,
    require_value: bool = False,
) -> bool:
    if not isinstance(ambiguity, dict) or ambiguity.get("type") != "filter":
        return False
    text = json.dumps(ambiguity, ensure_ascii=False, default=str).casefold()
    fields = {field, field.rsplit(".", 1)[-1], *(candidate_fields or set())}
    field_match = any(
        token and str(token).casefold() in text
        for token in fields
    )
    values = value if isinstance(value, list) else [value]
    value_texts = {
        str(item or "").strip().strip("%").casefold()
        for item in values
        if str(item or "").strip().strip("%")
    }
    value_match = bool(value_texts and any(token in text for token in value_texts))
    return field_match and (not require_value or value_match)


def _related_entity_distances(
    field: str,
    knowledge: dict,
    attributes_by_entity: dict[str, dict[str, dict]],
    relation_endpoints: dict[str, set[str]],
    *,
    max_hops: int = 4,
) -> dict[str, int]:
    """Return bounded recalled-relation distances for one physical key.

    This is used only to propose fields to the source-backed exact-value
    resolver.  It does not authorize a field by graph reachability alone.
    """
    seeds = set(relation_endpoints.get(field, set()))
    seeds.update(
        entity_code
        for entity_code, attributes in attributes_by_entity.items()
        if field in attributes
    )
    if not seeds:
        return {}
    adjacency: dict[str, set[str]] = {}
    for relation in knowledge.get("relations", []):
        metadata = getattr(relation, "metadata", {}) or {}
        source = str(
            metadata.get("parent")
            or metadata.get("source_entity")
            or metadata.get("source_entity_code")
            or ""
        ).strip()
        target = str(
            metadata.get("target_entity")
            or metadata.get("target_entity_code")
            or ""
        ).strip()
        if not source or not target or source == target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    distances = {entity_code: 0 for entity_code in seeds}
    frontier = set(seeds)
    for distance in range(1, max_hops + 1):
        next_frontier = {
            target
            for source in frontier
            for target in adjacency.get(source, set())
            if target not in distances
        }
        if not next_frontier:
            break
        distances.update({target: distance for target in next_frontier})
        frontier = next_frontier
    return distances


def _related_entity_component(
    field: str,
    knowledge: dict,
    attributes_by_entity: dict[str, dict[str, dict]],
    relation_endpoints: dict[str, set[str]],
    *,
    max_hops: int = 4,
) -> set[str]:
    return set(_related_entity_distances(
        field,
        knowledge,
        attributes_by_entity,
        relation_endpoints,
        max_hops=max_hops,
    ))


_DENORMALIZED_ATTRIBUTE = re.compile(
    r"(?:^|[^a-z0-9])(?:list|array|json|summary|profile|snapshot|rollup)"
    r"(?:$|[^a-z0-9])|列表|清单|集合|汇总|画像|快照|合作过",
    re.IGNORECASE,
)
_DISPLAY_LABEL_SUFFIXES = ("名称", "姓名", "标题", "name", "title", "label")


def _attribute_query_match_strength(attribute: dict, user_query: str) -> int:
    """Score explicit attribute wording without inferring from a value alone."""
    query_label = _semantic_label(user_query)
    if not query_label:
        return 0
    terms: set[str] = set()
    for value in (
        attribute.get("attr_name"),
        attribute.get("semantic_role"),
        attribute.get("attribute_role"),
    ):
        label = _semantic_label(value)
        if len(label) < 2:
            continue
        terms.add(label)
        for suffix in _DISPLAY_LABEL_SUFFIXES:
            suffix_label = _semantic_label(suffix)
            if label.endswith(suffix_label) and len(label) > len(suffix_label) + 1:
                terms.add(label[:-len(suffix_label)])
    return max(
        (len(term) for term in terms if term in query_label),
        default=0,
    )


def _prefer_source_verified_related_fields(
    matches: set[str],
    user_query: str,
    attributes_by_entity: dict[str, dict[str, dict]],
    entity_distances: dict[str, int],
) -> set[str]:
    """Keep the uniquely best source-verified field; preserve equal ties.

    Exact source equality is mandatory before this function is called.  The
    ranking then uses explicit attribute wording, prefers normalized scalar
    attributes over denormalized list/profile snapshots, and finally the
    nearest recalled relation.  Equal evidence remains a clarification.
    """
    if len(matches) < 2:
        return matches
    scores: dict[str, tuple[int, int, int]] = {}
    for candidate_field in matches:
        owner_attributes = [
            (entity_code, attributes[candidate_field])
            for entity_code, attributes in attributes_by_entity.items()
            if candidate_field in attributes
        ]
        semantic_strength = max(
            (
                _attribute_query_match_strength(attribute, user_query)
                for _, attribute in owner_attributes
            ),
            default=0,
        )
        metadata_text = " ".join([
            candidate_field,
            *(
                str(value or "")
                for _, attribute in owner_attributes
                for value in (
                    attribute.get("attr_code"),
                    attribute.get("attr_name"),
                    attribute.get("description"),
                    attribute.get("semantic_role"),
                )
            ),
        ])
        denormalized = 1 if _DENORMALIZED_ATTRIBUTE.search(metadata_text) else 0
        relation_distance = min(
            (
                entity_distances.get(entity_code, 10_000)
                for entity_code, _ in owner_attributes
            ),
            default=10_000,
        )
        scores[candidate_field] = (
            -semantic_strength,
            denormalized,
            relation_distance,
        )
    best_score = min(scores.values())
    return {
        candidate_field
        for candidate_field, score in scores.items()
        if score == best_score
    }


def _all_non_key_attribute_candidates(
    entity_codes: set[str],
    entities: dict[str, dict],
    attributes_by_entity: dict[str, dict[str, dict]],
    authorized_fields: set[str],
    excluded_field: str,
) -> list[dict]:
    """Build metadata-scoped fields for exact source-value disambiguation."""
    result: list[dict] = []
    for entity_code in sorted(entity_codes):
        entity = entities.get(entity_code, {})
        domain_id = (
            entity.get("business_domain_id")
            or entity.get("business_domain")
        )
        for candidate_field in sorted(attributes_by_entity.get(entity_code, {})):
            if (
                candidate_field == excluded_field
                or candidate_field not in authorized_fields
                or _is_relationship_key_field(
                    candidate_field, attributes_by_entity
                )
            ):
                continue
            result.append({
                "entity_code": entity_code,
                "field": candidate_field,
                "business_domain_id": domain_id,
            })
    return result


_CATALOG_IDENTITY_ATTRIBUTE = re.compile(
    r"(?<![a-z0-9])(?:name|title|label|brand|manufacturer|maker|category|"
    r"classification|type|kind)(?![a-z0-9])|"
    r"名称|姓名|标题|品牌|厂牌|厂家|生产商|制造商|分类|类别|品类|类型",
    re.IGNORECASE,
)
_CATALOG_NON_IDENTITY_ATTRIBUTE = re.compile(
    r"(?<![a-z0-9])(?:address|addr|street|road|location|longitude|latitude|"
    r"postal|zip|phone|mobile|tel|telephone|email|mail|contact|fax|"
    r"date|time|year|month|day|status|state|enabled|active|flag|"
    r"amount|price|cost|quantity|count|rate|ratio|score|rank|sort|"
    r"id|code|key|list|array|collection)(?![a-z0-9])|"
    r"地址|街道|道路|经纬度|邮编|电话|手机|邮箱|邮件|联系方式|联系人|传真|"
    r"日期|时间|年份|月份|状态|启用|有效|活跃|标志|金额|价格|成本|数量|"
    r"次数|比率|比例|得分|排名|排序|编码|编号|主键|清单|列表|集合|汇总",
    re.IGNORECASE,
)
_CATALOG_NUMERIC_TYPE = re.compile(
    r"(?:^|\W)(?:tinyint|smallint|mediumint|int|integer|bigint|decimal|"
    r"numeric|number|float|double|real|bit|bool|boolean)(?:\W|$)",
    re.IGNORECASE,
)


def _is_catalog_identity_attribute(field: str, attribute: dict) -> bool:
    """Whether a field may participate in fuzzy catalog value migration.

    Catalog lookup accepts substring matches, so it is deliberately narrower
    than exact-value disambiguation.  Only identity-like text attributes are
    eligible; operational facts and free-text contact/location fields must not
    become a product, brand, manufacturer, category, or type filter.
    """
    semantic_text = " ".join(str(value or "") for value in (
        field.rsplit(".", 1)[-1],
        attribute.get("attr_code"),
        attribute.get("attr_name"),
        attribute.get("description"),
        attribute.get("semantic_role"),
        attribute.get("attribute_role"),
    ))
    data_type = str(attribute.get("data_type") or "")
    if _CATALOG_NUMERIC_TYPE.search(data_type):
        return False
    if _CATALOG_NON_IDENTITY_ATTRIBUTE.search(semantic_text):
        return False
    return bool(
        attribute.get("is_main_attribute")
        or attribute.get("is_primary_name")
        or attribute.get("is_display_name")
        or _CATALOG_IDENTITY_ATTRIBUTE.search(semantic_text)
    )


def _catalog_identity_candidates(
    candidates: list[dict],
    attributes_by_entity: dict[str, dict[str, dict]],
) -> list[dict]:
    """Restrict source-catalog candidates to recalled identity attributes."""
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and _is_catalog_identity_attribute(
            str(candidate.get("field") or ""),
            attributes_by_entity.get(
                str(candidate.get("entity_code") or ""), {}
            ).get(str(candidate.get("field") or ""), {}),
        )
    ]


_GROUPING_QUANTIFIER = re.compile(
    r"^(?:每(?:一)?(?:个|家|类|种)?|各(?:个|家|类|种)?|所有|全部|逐(?:个|家|类|种)?)\s*(.+)$"
)
_EXPLICIT_GROUPING_NOUN = re.compile(
    r"(?:每(?:一)?(?:个|家|类|种)?|各(?:个|家|类|种)?|所有|全部|"
    r"逐(?:个|家|类|种)?)(?P<noun>[\u4e00-\u9fffA-Za-z]{2,16}?)"
    r"(?=的?(?:已)?(?:合作|销售|订单|数量|数|金额|总额|销量|覆盖))"
)


def _normalize_explicit_grouping_dimensions(
    ast: dict,
    knowledge: dict,
    user_query: str,
) -> None:
    """Materialize an explicitly quantified result grain from recalled metadata."""
    dimensions = ast.get("dimensions")
    if not isinstance(dimensions, list):
        return
    match = _EXPLICIT_GROUPING_NOUN.search(str(user_query or ""))
    if match is None:
        return
    noun = match.group("noun").strip().casefold()
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized_fields = _known_physical_fields(knowledge)
    exact_entities: list[str] = []
    related_entities: list[str] = []
    for entity_code, metadata in entities.items():
        labels: set[str] = {entity_code.casefold()}
        for key in ("entity_name", "entity_alias"):
            labels.update(
                str(value).strip().casefold()
                for value in _metadata_term_values(metadata.get(key))
                if str(value).strip()
            )
        if noun in labels:
            exact_entities.append(entity_code)
        elif any(noun in label or label in noun for label in labels):
            related_entities.append(entity_code)
    matched_entities = exact_entities or related_entities
    if len(matched_entities) != 1:
        return
    entity_code = matched_entities[0]
    candidates = [
        field
        for field, attribute in attributes_by_entity.get(entity_code, {}).items()
        if field in authorized_fields
        and _is_catalog_identity_attribute(field, attribute)
        and not _is_relationship_key_field(field, attributes_by_entity)
    ]
    preferred = [
        field for field in candidates
        if bool(attributes_by_entity[entity_code][field].get("is_main_attribute"))
        or bool(attributes_by_entity[entity_code][field].get("is_primary_name"))
        or bool(attributes_by_entity[entity_code][field].get("is_display_name"))
    ]
    selected = preferred if preferred else candidates
    if len(selected) != 1:
        return
    field = selected[0]
    if not any(
        isinstance(item, dict) and str(item.get("name") or "") == field
        for item in dimensions
    ):
        dimensions.append({
            "name": field,
            "attr": None,
            "level": None,
            "granularity": None,
        })


def _dedupe_equivalent_dimensions(ast: dict, knowledge: dict) -> None:
    """Keep one semantic dimension when its bound physical field is duplicated.

    A model may emit both ``dealer`` and ``dealer.dealer_name`` for the same
    requested result grain.  The logical dimension is authoritative because
    it carries the registered business label and binding contract; exposing
    the physical field as a second dimension duplicates SELECT/GROUP BY and
    leaks a technical column name into the result.
    """
    dimensions = ast.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) < 2:
        return

    logical_bindings: dict[str, set[str]] = {}
    for result in knowledge.get("dimensions", []):
        metadata = getattr(result, "metadata", {}) or {}
        code = str(metadata.get("dim_code") or "").strip()
        if not code:
            continue
        fields: set[str] = set()
        bindings = _json_value(metadata.get("bind_entities")) or []
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict):
                continue
            table = str(binding.get("mappingTable") or "").strip()
            column = str(binding.get("mappingColumn") or "").strip()
            field = f"{table}.{column}" if table and column else ""
            if _PHYSICAL_FIELD.fullmatch(field):
                fields.add(field)
        if fields:
            logical_bindings[code] = fields

    selected_logical = {
        str(item.get("name") or "").strip()
        for item in dimensions
        if isinstance(item, dict)
        and str(item.get("name") or "").strip() in logical_bindings
    }
    redundant_physical = {
        field
        for code in selected_logical
        for field in logical_bindings[code]
    }
    if not redundant_physical:
        return

    physical_to_logical = {
        field: code
        for code in selected_logical
        for field in logical_bindings[code]
    }
    ast["dimensions"] = [
        item
        for item in dimensions
        if not (
            isinstance(item, dict)
            and str(item.get("name") or "").strip() in redundant_physical
        )
    ]
    sort = ast.get("sort")
    if isinstance(sort, dict):
        replacement = physical_to_logical.get(str(sort.get("field") or "").strip())
        if replacement:
            sort["field"] = replacement
            sort["field_type"] = "dimension"


def _normalize_grouping_quantifier_filters(
    ast: dict,
    knowledge: dict,
    user_query: str,
) -> None:
    """Remove a grouping quantifier copied by the model into a filter value.

    ``每个经销商`` describes result grain when the same semantic entity is
    already a selected dimension. It is not a catalog value. The rewrite is
    allowed only when recalled metadata proves that the quantified noun
    matches that selected grouping dimension.
    """
    filters = ast.get("filters")
    dimensions = ast.get("dimensions")
    ambiguities = ast.get("ambiguity")
    if not isinstance(filters, list) or not isinstance(dimensions, list):
        return

    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    selected_fields = {
        str(item.get("name") or "").strip()
        for item in dimensions
        if isinstance(item, dict) and item.get("name")
    }
    dimension_labels: set[str] = set()
    for entity_code, metadata in entities.items():
        attributes = attributes_by_entity.get(entity_code, {})
        matched_fields = selected_fields.intersection(attributes)
        if not matched_fields and not any(
            field.startswith(f"{entity_code}.") for field in selected_fields
        ):
            continue
        for key in ("entity_code", "entity_name", "entity_alias"):
            dimension_labels.update(_metadata_term_values(metadata.get(key)))
        for field in matched_fields:
            attribute = attributes[field]
            dimension_labels.add(field)
            for key in ("attr_code", "attr_name", "description"):
                dimension_labels.update(_metadata_term_values(attribute.get(key)))

    for result in knowledge.get("dimensions", []):
        metadata = getattr(result, "metadata", {}) or {}
        if str(metadata.get("dim_code") or "") not in selected_fields:
            continue
        for key in ("dim_code", "dim_name", "synonyms", "dim_description"):
            dimension_labels.update(_metadata_term_values(metadata.get(key)))

    normalized_labels = {
        re.sub(r"(?:名称|名字|编码|代码|编号)$", "", str(label).strip()).casefold()
        for label in dimension_labels
        if str(label).strip()
    }
    retained: list = []
    removed: list[tuple[str, object]] = []
    for item in filters:
        if not isinstance(item, dict):
            retained.append(item)
            continue
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        match = (
            _GROUPING_QUANTIFIER.fullmatch(value.strip())
            if isinstance(value, str) and operator in {"=", "EQ", "LIKE"}
            else None
        )
        noun = (
            re.sub(r"(?:名称|名字|编码|代码|编号)$", "", match.group(1).strip()).casefold()
            if match else ""
        )
        dimension_match = bool(
            len(noun) >= 2
            and any(
                noun == label or noun in label or label in noun
                for label in normalized_labels
                if len(label) >= 2
            )
        )
        if match and dimension_match and value in str(user_query or ""):
            removed.append((str(item.get("field") or ""), value))
            continue
        retained.append(item)
    if not removed:
        return
    filters[:] = retained
    if isinstance(ambiguities, list):
        ambiguities[:] = [
            ambiguity
            for ambiguity in ambiguities
            if not any(
                _ambiguity_mentions_filter(
                    ambiguity, field, value, require_value=True
                )
                or (
                    isinstance(ambiguity, dict)
                    and ambiguity.get("type") == "filter"
                    and str(value).casefold() in json.dumps(
                        ambiguity, ensure_ascii=False, default=str
                    ).casefold()
                )
                for field, value in removed
            )
        ]


def _normalize_unretrieved_literal_filters(
    ast: dict,
    knowledge: dict,
    user_query: str,
    *,
    semantic_model_id: int | None = None,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    exact_attribute_value_resolver=None,
) -> None:
    """Repair only source-proven literals placed on an unretrieved field.

    Models occasionally preserve the right user value but invent a plausible
    physical column.  Leaving that field in the AST produces a 502, while
    dropping it would silently broaden the query.  This routine instead checks
    every literal against already recalled, non-key attributes in the
    registered source.  All values of an IN filter must resolve to the same
    uniquely preferred field.  Anything else is removed from execution and
    represented as an explicit clarification.
    """
    filters = ast.get("filters")
    ambiguities = ast.get("ambiguity")
    if (
        not isinstance(filters, list)
        or not isinstance(ambiguities, list)
        or type(semantic_model_id) is not int
    ):
        return
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized_fields = _known_physical_fields(knowledge)
    if not entities or not authorized_fields:
        return
    resolver = (
        exact_attribute_value_resolver
        or resolve_exact_entity_attribute_value_fields
    )

    retained: list[dict] = []
    for item in filters:
        if not isinstance(item, dict):
            retained.append(item)
            continue
        field = str(item.get("field") or "").strip()
        if field in authorized_fields:
            retained.append(item)
            # The model may emit an ambiguity before deterministic semantic
            # normalization runs.  Current recalled metadata is stronger
            # evidence: once this exact physical field is authorized, remove
            # only the ambiguity tied to the same field and literal.
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    item.get("value"),
                    {field},
                    require_value=True,
                )
            ]
            continue
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        natural_values = _natural_name_filter_values(operator, value, user_query)
        candidate_fields: set[str] = set()
        matches_by_value: list[dict[str, str]] = []
        administrative_level = (
            _administrative_query_level(natural_values[0], user_query)
            if natural_values else None
        )
        candidates = _all_non_key_attribute_candidates(
            set(attributes_by_entity),
            entities,
            attributes_by_entity,
            authorized_fields,
            field,
        )
        if administrative_level is not None:
            candidates = [
                candidate
                for candidate in candidates
                if _administrative_attribute_kind(
                    candidate["field"],
                    attributes_by_entity[candidate["entity_code"]][candidate["field"]],
                ) is not None
            ]
        candidate_fields = {candidate["field"] for candidate in candidates}

        for raw_value in natural_values:
            canonical_matches: dict[str, str] = {}
            values_to_try = [raw_value]
            if administrative_level is not None and not raw_value.endswith(
                _ADMINISTRATIVE_SUFFIXES
            ):
                values_to_try.extend(
                    raw_value + suffix for suffix in _ADMINISTRATIVE_SUFFIXES
                )
            for candidate_value in values_to_try:
                resolved_fields: list[str] = []
                try:
                    for offset in range(0, len(candidates), 32):
                        resolved_fields.extend(resolver(
                            semantic_model_id,
                            business_domain_id,
                            candidates[offset:offset + 32],
                            candidate_value,
                        ) or [])
                except Exception as exc:
                    logger.warning(
                        "unretrieved filter resolution unavailable: sm=%s, "
                        "error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    canonical_matches.clear()
                    break
                for resolved_field in resolved_fields:
                    resolved_field = str(resolved_field)
                    if resolved_field in candidate_fields:
                        canonical_matches.setdefault(
                            resolved_field, candidate_value
                        )
                if canonical_matches:
                    break
            matches_by_value.append(canonical_matches)

        common_fields = (
            set.intersection(*(set(matches) for matches in matches_by_value))
            if matches_by_value and all(matches_by_value)
            else set()
        )
        fallback_entity = next(iter(entities), "")
        distances = _result_entity_distances(
            ast, knowledge, attributes_by_entity, fallback_entity
        )
        if administrative_level is not None and common_fields:
            scores = {}
            for candidate_field in common_fields:
                owners = [
                    entity_code
                    for entity_code, attributes in attributes_by_entity.items()
                    if candidate_field in attributes
                ]
                attribute = next(
                    attributes[candidate_field]
                    for attributes in attributes_by_entity.values()
                    if candidate_field in attributes
                )
                kind = _administrative_attribute_kind(candidate_field, attribute)
                if kind is not None:
                    scores[candidate_field] = min(
                        _administrative_field_score(
                            owner, kind, administrative_level, distances
                        )
                        for owner in owners
                    )
            best_score = min(scores.values()) if scores else None
            preferred = {
                candidate_field
                for candidate_field, score in scores.items()
                if score == best_score
            }
        else:
            preferred = _prefer_source_verified_related_fields(
                common_fields,
                user_query,
                attributes_by_entity,
                distances,
            )

        if len(preferred) == 1:
            matched_field = next(iter(preferred))
            canonical_values = [
                matches[matched_field] for matches in matches_by_value
            ]
            item["field"] = matched_field
            if operator in {"IN", "NOT IN"}:
                item["value"] = canonical_values
            else:
                item["value"] = canonical_values[0]
                if operator == "LIKE":
                    item["operator"] = "="
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    candidate_fields,
                    require_value=True,
                )
            ]
            retained.append(item)
            continue

        rendered_values = "、".join(natural_values) or str(value)
        _append_ambiguity(
            ast,
            "filter",
            (
                f"过滤值 {rendered_values} 使用了未注册字段 {field}，"
                "当前语义和源数据不能唯一确定其规范过滤属性，请确认筛选口径。"
            ),
            sorted(common_fields or candidate_fields),
        )
        # Do not retain the unauthorized predicate.  The non-empty ambiguity
        # makes the AST non-executable, so this cannot silently broaden SQL.
    filters[:] = retained


def _normalize_relation_name_filters(
    ast: dict,
    knowledge: dict,
    user_query: str,
    *,
    semantic_model_id: int | None = None,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    exact_value_resolver=None,
    exact_attribute_value_resolver=None,
    catalog_value_resolver=None,
) -> None:
    """Move obvious name values off recalled relationship code keys.

    The rewrite is permitted only for an authorized code/id/join key when an
    exact parameterized lookup resolves every literal to one related endpoint
    display-name field. Multiple candidate endpoint names are safe because the
    source lookup, rather than vector rank, must uniquely choose one. Otherwise
    a clarification is retained or added; no unrelated attribute is exposed.
    """
    filters = ast.get("filters")
    if not isinstance(filters, list):
        return
    ambiguities = ast.get("ambiguity")
    if not isinstance(ambiguities, list):
        return
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized_fields = _known_physical_fields(knowledge)
    relation_endpoints: dict[str, set[str]] = {}
    for item in knowledge.get("relations", []):
        metadata = getattr(item, "metadata", {}) or {}
        source_entity = str(metadata.get("parent") or "").strip()
        target_entity = str(metadata.get("target_entity") or "").strip()
        join_key = _json_value(metadata.get("join_key")) or {}
        if not source_entity or not target_entity or not isinstance(join_key, dict):
            continue
        for side in ("source_field", "target_field"):
            field = _physical_field_from_mapping(join_key.get(side))
            if field:
                relation_endpoints.setdefault(field, set()).update(
                    (source_entity, target_entity)
                )

    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        endpoints = relation_endpoints.get(field, set())
        natural_values = _natural_name_filter_values(operator, value, user_query)
        if (
            field not in authorized_fields
            or not _is_relationship_key_field(field, attributes_by_entity)
            or not natural_values
        ):
            continue

        name_candidates = _primary_name_candidates(
            endpoints,
            entities,
            attributes_by_entity,
            authorized_fields,
        )
        name_candidates = {
            candidate
            for candidate in name_candidates
            if candidate[1] != field
        }
        name_fields = {candidate[1] for candidate in name_candidates}
        matched_by_value: list[set[str]] = []
        if name_candidates and type(semantic_model_id) is int:
            resolver_candidates = []
            for entity_code, name_field in sorted(name_candidates):
                entity_metadata = entities.get(entity_code, {})
                candidate_domain = (
                    entity_metadata.get("business_domain_id")
                    or entity_metadata.get("business_domain")
                )
                resolver_candidates.append({
                    "entity_code": entity_code,
                    "field": name_field,
                    "business_domain_id": candidate_domain,
                })
            resolver = exact_value_resolver or resolve_exact_entity_value_fields
            for raw_value in natural_values:
                try:
                    resolved = resolver(
                        semantic_model_id,
                        business_domain_id,
                        resolver_candidates,
                        raw_value,
                    )
                    matched_by_value.append({
                        str(resolved_field)
                        for resolved_field in (resolved or [])
                        if str(resolved_field) in name_fields
                    })
                except Exception as exc:
                    # A metadata/source outage cannot authorize a semantic
                    # rewrite. Log only its type so credentials never leak.
                    logger.warning(
                        "exact entity-name resolution unavailable: sm=%s, error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    matched_by_value.append(set())

        common_name_fields = (
            set.intersection(*(set(matches) for matches in matched_by_value))
            if matched_by_value and all(matched_by_value)
            else set()
        )
        if (
            len(matched_by_value) == len(natural_values)
            and len(common_name_fields) == 1
        ):
            name_field = next(iter(common_name_fields))
            item["field"] = name_field
            if operator in {"IN", "NOT IN"}:
                item["operator"] = operator
                item["value"] = natural_values
            elif operator == "!=":
                item["operator"] = "!="
                item["value"] = natural_values[0]
            else:
                # The authorized source lookup proved this exact literal.
                # Adding wildcards would silently broaden the accepted binding.
                item["operator"] = "="
                item["value"] = natural_values[0]
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    name_fields,
                )
            ]
            continue

        # A user may name a structured attribute (for example a parent brand
        # or department) while the model places that value on a related code
        # key.  Primary display-name lookup alone cannot repair this case.  A
        # bounded recalled relation component plus an exact source lookup can:
        # every literal must resolve to the same single authorized non-key
        # field, otherwise execution remains blocked by clarification.
        broad_fields: set[str] = set()
        broad_matches_by_value: list[set[str]] = []
        broad_candidates: list[dict] = []
        related_distances: dict[str, int] = {}
        if _administrative_query_level(natural_values[0], user_query) is None:
            related_distances = _related_entity_distances(
                field,
                knowledge,
                attributes_by_entity,
                relation_endpoints,
            )
            broad_candidates = _all_non_key_attribute_candidates(
                set(related_distances),
                entities,
                attributes_by_entity,
                authorized_fields,
                field,
            )
            broad_fields = {item["field"] for item in broad_candidates}
            if broad_candidates and type(semantic_model_id) is int:
                broad_resolver = (
                    exact_attribute_value_resolver
                    or resolve_exact_entity_attribute_value_fields
                )
                for raw_value in natural_values:
                    try:
                        resolved = []
                        # The source resolver intentionally caps one metadata
                        # query at 32 fields.  Chunking preserves that safety
                        # bound without dropping a valid related attribute due
                        # solely to a wider bounded relation path.
                        for offset in range(0, len(broad_candidates), 32):
                            resolved.extend(broad_resolver(
                                semantic_model_id,
                                business_domain_id,
                                broad_candidates[offset:offset + 32],
                                raw_value,
                            ) or [])
                        source_matches = {
                            str(resolved_field)
                            for resolved_field in resolved
                            if str(resolved_field) in broad_fields
                        }
                        broad_matches_by_value.append(source_matches)
                    except Exception as exc:
                        logger.warning(
                            "related attribute-value resolution unavailable: "
                            "sm=%s, error_type=%s",
                            semantic_model_id,
                            type(exc).__name__,
                        )
                        broad_matches_by_value.append(set())
        broad_evidence_fields = (
            set().union(*broad_matches_by_value)
            if broad_matches_by_value else set()
        )
        common_broad_fields = (
            set.intersection(*(set(matches) for matches in broad_matches_by_value))
            if broad_matches_by_value and all(broad_matches_by_value)
            else set()
        )
        preferred_common_fields = _prefer_source_verified_related_fields(
            common_broad_fields,
            user_query,
            attributes_by_entity,
            related_distances,
        )
        if (
            len(broad_matches_by_value) == len(natural_values)
            and len(preferred_common_fields) == 1
        ):
            matched_field = next(iter(preferred_common_fields))
            item["field"] = matched_field
            if operator in {"IN", "NOT IN"}:
                item["operator"] = operator
                item["value"] = natural_values
            elif operator == "!=":
                item["operator"] = "!="
                item["value"] = natural_values[0]
            else:
                item["operator"] = "="
                item["value"] = natural_values[0]
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    name_fields.union(broad_evidence_fields),
                )
            ]
            continue

        # Exact equality cannot resolve a contextual noun that the model kept
        # inside a key value (for example ``费森尤斯产品`` while the registered
        # parent-brand value is ``费森尤斯``).  A bounded catalog lookup may
        # canonicalize it, but only when every literal has one unique best hit
        # and all hits target the same recalled non-key attribute.
        catalog_choices: list[dict] = []
        catalog_evidence_fields: set[str] = set()
        catalog_ranked_choices: set[str] = set()
        catalog_candidates = _catalog_identity_candidates(
            broad_candidates, attributes_by_entity
        )
        catalog_fields = {
            str(candidate["field"]) for candidate in catalog_candidates
        }
        if (
            catalog_candidates
            and type(semantic_model_id) is int
            and not broad_evidence_fields
        ):
            catalog_resolver = (
                catalog_value_resolver
                or resolve_entity_attribute_catalog_matches
            )
            for raw_value in natural_values:
                try:
                    catalog_matches: list[dict] = []
                    for offset in range(0, len(catalog_candidates), 32):
                        catalog_matches.extend(catalog_resolver(
                            semantic_model_id,
                            business_domain_id,
                            catalog_candidates[offset:offset + 32],
                            raw_value,
                        ) or [])
                except Exception as exc:
                    logger.warning(
                        "related attribute catalog resolution unavailable: "
                        "sm=%s, error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    catalog_choices = []
                    break
                catalog_evidence_fields.update(
                    str(match.get("field") or "")
                    for match in catalog_matches
                    if isinstance(match, dict)
                    and str(match.get("field") or "") in catalog_fields
                )
                catalog_ranked_choices.update(
                    f"{match.get('field')}={match.get('canonical_value')}"
                    for match in catalog_matches
                    if isinstance(match, dict)
                    and str(match.get("field") or "") in catalog_fields
                    and str(match.get("canonical_value") or "").strip()
                )
                choice = _unique_catalog_match(catalog_matches, catalog_fields)
                if choice is None:
                    catalog_choices = []
                    break
                catalog_choices.append(choice)

        if (
            len(catalog_choices) == len(natural_values)
            and len({str(choice["field"]) for choice in catalog_choices}) == 1
        ):
            matched_field = str(catalog_choices[0]["field"])
            canonical_values = [
                str(choice["canonical_value"]) for choice in catalog_choices
            ]
            item["field"] = matched_field
            if operator in {"IN", "NOT IN"}:
                item["operator"] = operator
                item["value"] = canonical_values
            elif operator == "!=":
                item["operator"] = "!="
                item["value"] = canonical_values[0]
            else:
                item["operator"] = "="
                item["value"] = canonical_values[0]
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    name_fields.union(catalog_evidence_fields),
                    require_value=True,
                )
            ]
            continue

        if not any(
            _ambiguity_mentions_filter(
                ambiguity,
                field,
                value,
                name_fields.union(broad_evidence_fields),
                require_value=True,
            )
            for ambiguity in ambiguities
        ):
            ambiguities.append({
                "type": "filter",
                "question": (
                    f"过滤值 {'、'.join(natural_values)} 看起来不像关系键 {field} 的编码，"
                    "且当前语义作用域"
                    "无法唯一确定对应的名称属性。请确认使用编码还是名称过滤。"
                ),
                "candidates": sorted(
                    catalog_ranked_choices
                    or name_fields
                    .union(broad_evidence_fields)
                    .union(catalog_evidence_fields)
                ),
            })


def _literal_is_explicitly_mentioned(value: str, user_query: str) -> bool:
    """Require an exact user mention before source-backed filter correction."""
    literal = value.casefold()
    query = str(user_query or "").casefold()
    start = query.find(literal)
    while start >= 0:
        before = query[start - 1] if start else ""
        end = start + len(literal)
        after = query[end] if end < len(query) else ""
        if not (
            literal[0].isascii()
            and literal[0].isalnum()
            and before.isascii()
            and before.isalnum()
        ) and not (
            literal[-1].isascii()
            and literal[-1].isalnum()
            and after.isascii()
            and after.isalnum()
        ):
            return True
        start = query.find(literal, start + 1)
    return False


def _split_composite_brand_filter(
    ast: dict,
    knowledge: dict,
    user_query: str,
    *,
    semantic_model_id: int | None = None,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    catalog_value_resolver=None,
) -> None:
    """Split a source-verified brand modifier from another entity value.

    Some model outputs concatenate adjacent natural-language modifiers into a
    single name filter (``<brand>品牌<product>``).  Brand fields are identified
    only from retrieved attribute metadata.  Both resulting literals must be
    proven as exact values in their registered physical fields before the AST
    is changed, so no brand vocabulary or ownership mapping lives in code.
    """
    filters = ast.get("filters")
    ambiguities = ast.get("ambiguity")
    if (
        not isinstance(filters, list)
        or not isinstance(ambiguities, list)
        or type(semantic_model_id) is not int
    ):
        return
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized_fields = _known_physical_fields(knowledge)
    resolver = catalog_value_resolver or resolve_entity_attribute_catalog_matches
    brand_candidates: list[dict] = []
    brand_fields: set[str] = set()
    catalog_candidates: list[dict] = []
    catalog_fields: set[str] = set()
    for entity_code, attributes in attributes_by_entity.items():
        entity_metadata = entities.get(entity_code, {})
        candidate_domain = (
            entity_metadata.get("business_domain_id")
            or entity_metadata.get("business_domain")
        )
        entity_semantics = " ".join(str(entity_metadata.get(key) or "") for key in (
            "entity_code", "entity_name", "entity_alias", "description",
        )).casefold()
        is_brand_entity = any(
            token in entity_semantics for token in ("品牌", "厂牌", "brand")
        )
        for field, attribute in attributes.items():
            semantic_text = " ".join(str(attribute.get(key) or "") for key in (
                "attr_code", "attr_name", "description",
            )).casefold()
            if field not in authorized_fields or _is_relationship_key_field(
                field, attributes_by_entity
            ):
                continue
            if not _is_catalog_identity_attribute(field, attribute):
                continue
            candidate = {
                "entity_code": entity_code,
                "field": field,
                "business_domain_id": candidate_domain,
                "catalog_priority": (
                    0 if any(token in semantic_text for token in (
                        "type", "类型", "分类", "category", "品类",
                    )) else
                    1 if bool(attribute.get("is_main_attribute")) else
                    2
                ),
            }
            is_brand_attribute = any(token in semantic_text for token in (
                "brand", "品牌", "厂牌", "manufacturer", "maker",
                "厂家", "生产商", "制造商",
            ))
            if is_brand_entity or is_brand_attribute:
                brand_fields.add(field)
                brand_candidates.append(candidate)
            catalog_fields.add(field)
            catalog_candidates.append(candidate)
    if not brand_candidates:
        return

    # Canonicalize an already separated brand/manufacturer literal as well.
    # Composite splitting below cannot help when the model correctly produced
    # two filters but chose the wrong brand attribute.  Catalog evidence is
    # authoritative: a unique best hit supplies both the physical field and
    # the canonical source value; equal-ranked hits remain a clarification.
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        if (
            field not in brand_fields
            or operator not in {"=", "LIKE"}
            or not isinstance(value, str)
            or not re.search(r"品牌|厂牌|brand", user_query, re.IGNORECASE)
        ):
            continue
        raw_value = value.strip().strip("%")
        if (
            len(raw_value) < 2
            or "%" in raw_value
            or "_" in raw_value
            or re.search(r"品牌|厂牌|brand", raw_value, re.IGNORECASE)
            or not _literal_is_explicitly_mentioned(raw_value, user_query)
        ):
            continue
        try:
            catalog_matches = resolver(
                semantic_model_id,
                business_domain_id,
                brand_candidates,
                raw_value,
            ) or []
        except Exception as exc:
            logger.warning(
                "brand attribute resolution unavailable: sm=%s, error_type=%s",
                semantic_model_id,
                type(exc).__name__,
            )
            continue
        match = _unique_catalog_match(
            catalog_matches,
            brand_fields,
            prefer_unique_main=True,
        )
        if match is not None:
            item["field"] = match["field"]
            item["operator"] = "="
            item["value"] = match["canonical_value"]
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    brand_fields,
                    require_value=True,
                )
            ]
            continue
        ranked_choices = sorted({
            f"{candidate.get('field')}={candidate.get('canonical_value')}"
            for candidate in catalog_matches
            if isinstance(candidate, dict)
            and candidate.get("field") in brand_fields
            and str(candidate.get("canonical_value") or "").strip()
        })
        if ranked_choices and not any(
            isinstance(ambiguity, dict)
            and ambiguity.get("type") == "filter"
            and ambiguity.get("candidates") == ranked_choices
            for ambiguity in ambiguities
        ):
            ambiguities.append({
                "type": "filter",
                "question": (
                    f"品牌值 {raw_value} 对应多个同优先级目录字段或规范值，"
                    "请确认具体品牌口径。"
                ),
                "candidates": ranked_choices,
            })

    for item in list(filters):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        if (
            field not in authorized_fields
            or field in brand_fields
            or operator not in {"=", "LIKE"}
            or not isinstance(value, str)
        ):
            continue
        raw_value = value.strip().strip("%")
        marker = re.search(r"品牌|厂牌|brand", raw_value, re.IGNORECASE)
        if marker is None:
            continue
        brand_value = raw_value[:marker.start()].strip(" 的-_/，,：:")
        entity_value = raw_value[marker.end():].strip(" 的-_/，,：:")
        if (
            len(brand_value) < 2
            or len(entity_value) < 2
            or not _literal_is_explicitly_mentioned(raw_value, user_query)
        ):
            continue

        owner_codes = {
            entity_code
            for entity_code, attributes in attributes_by_entity.items()
            if field in attributes
        }
        if len(owner_codes) != 1:
            continue
        owner_code = next(iter(owner_codes))
        tail_candidates = [
            candidate for candidate in catalog_candidates
            if candidate["field"] not in brand_fields
        ]
        if field not in {candidate["field"] for candidate in tail_candidates}:
            owner_domain = (
                entities.get(owner_code, {}).get("business_domain_id")
                or entities.get(owner_code, {}).get("business_domain")
            )
            tail_candidates.append({
                "entity_code": owner_code,
                "field": field,
                "business_domain_id": owner_domain,
                "catalog_priority": -1,
            })
        tail_candidates.sort(key=lambda candidate: (
            0 if candidate["field"] == field else 1,
            int(candidate.get("catalog_priority", 2)),
            candidate["field"],
        ))
        tail_candidates = tail_candidates[:24]
        try:
            brand_catalog_matches = resolver(
                semantic_model_id,
                business_domain_id,
                brand_candidates,
                brand_value,
            ) or []
            brand_match = _unique_catalog_match(
                brand_catalog_matches,
                brand_fields,
                prefer_unique_main=True,
            )
            entity_catalog_matches = resolver(
                semantic_model_id,
                business_domain_id,
                tail_candidates,
                entity_value,
            ) or []
            entity_match = _unique_catalog_match(
                entity_catalog_matches,
                {candidate["field"] for candidate in tail_candidates},
            )
        except Exception as exc:
            logger.warning(
                "composite entity-filter resolution unavailable: sm=%s, error_type=%s",
                semantic_model_id,
                type(exc).__name__,
            )
            continue
        if brand_match is None or entity_match is None:
            ranked_choices = sorted({
                f"{match.get('field')}={match.get('canonical_value')}"
                for match in [
                    *brand_catalog_matches,
                    *entity_catalog_matches,
                ]
                if isinstance(match, dict)
                and str(match.get("field") or "") in (
                    brand_fields
                    | {candidate["field"] for candidate in tail_candidates}
                )
                and str(match.get("canonical_value") or "").strip()
            })
            if ranked_choices:
                _append_ambiguity(
                    ast,
                    "filter",
                    (
                        f"复合目录值 {raw_value} 存在同级字段或规范值候选，"
                        "请明确品牌/厂家以及商品/分类口径。"
                    ),
                    ranked_choices,
                )
            continue

        brand_field = brand_match["field"]
        item["field"] = entity_match["field"]
        item["operator"] = "="
        item["value"] = entity_match["canonical_value"]
        if not any(
            isinstance(existing, dict)
            and str(existing.get("field") or "") == brand_field
            and str(existing.get("value") or "").strip("%")
            == brand_match["canonical_value"]
            for existing in filters
        ):
            filters.append({
                "field": brand_field,
                "operator": "=",
                "value": brand_match["canonical_value"],
            })
        ambiguities[:] = [
            ambiguity
            for ambiguity in ambiguities
            if not _ambiguity_mentions_filter(
                ambiguity,
                field,
                value,
                {brand_field},
                require_value=True,
            )
        ]


def _unique_catalog_match(
    matches: list[dict],
    allowed_fields: set[str],
    *,
    prefer_unique_main: bool = False,
) -> dict | None:
    """Select only a unique best catalog hit; exact and main-name hits win."""
    rank = {
        "EXACT": 0,
        "CANONICAL_CONTAINS_MENTION": 1,
        "MENTION_CONTAINS_CANONICAL": 2,
    }
    candidates: dict[tuple[str, str], tuple[tuple[int, int], dict]] = {}
    for match in matches:
        if not isinstance(match, dict):
            continue
        field = str(match.get("field") or "")
        canonical_value = str(match.get("canonical_value") or "").strip()
        match_type = str(match.get("match_type") or "")
        if field not in allowed_fields or not canonical_value or match_type not in rank:
            continue
        score = (rank[match_type], 0 if match.get("is_main_attribute") else 1)
        key = (field, canonical_value.casefold())
        existing = candidates.get(key)
        if existing is None or score < existing[0]:
            candidates[key] = (score, match)
    if not candidates:
        return None
    if prefer_unique_main:
        best_match_rank = min(item[0][0] for item in candidates.values())
        main_matches = [
            item[1]
            for item in candidates.values()
            if item[0][0] == best_match_rank
            and item[1].get("is_main_attribute")
        ]
        if len(main_matches) == 1:
            return main_matches[0]
    best_score = min(item[0] for item in candidates.values())
    best = [item[1] for item in candidates.values() if item[0] == best_score]
    return best[0] if len(best) == 1 else None


_ADMINISTRATIVE_SUFFIXES = (
    "特别行政区", "自治区", "自治州", "地区", "城市", "省", "市", "区", "县",
)
_ADMINISTRATIVE_FREE_TEXT = re.compile(
    r"address|addr|street|road|location|longitude|latitude|postal|zip|"
    r"地址|详细地址|街道|道路|路号|经纬度|邮编",
    re.IGNORECASE,
)


def _administrative_attribute_kind(
    field: str,
    attribute: dict,
) -> str | None:
    """Classify an attribute only when its metadata denotes an admin level.

    Source values alone are deliberately insufficient: a city name commonly
    appears in a company name or a free-text address.  Attribute/column codes
    are considered before translated labels, and address-like metadata is an
    explicit exclusion even when it also contains words such as ``region``.
    """
    column = field.rsplit(".", 1)[-1]
    code_text = " ".join((
        column,
        str(attribute.get("attr_code") or ""),
    )).casefold()
    semantic_text = " ".join((
        code_text,
        str(attribute.get("attr_name") or ""),
        str(attribute.get("description") or ""),
        str(attribute.get("semantic_role") or ""),
    )).casefold()
    if _ADMINISTRATIVE_FREE_TEXT.search(semantic_text):
        return None

    def english_token(text: str, token: str) -> bool:
        return bool(re.search(rf"(?:^|[^a-z0-9]){token}(?:$|[^a-z0-9])", text))

    # Stable codes are more precise than a broad translated label such as
    # "所在省市".  This makes ``attr_code=province`` unambiguously provincial.
    for token, kind in (
        ("province", "province"),
        ("city", "city"),
        ("district", "district"),
        ("county", "district"),
        ("region", "region"),
        ("administrative_area", "region"),
    ):
        if english_token(code_text.replace("_", " "), token.replace("_", " ")):
            return kind

    label = _semantic_label(semantic_text)
    if any(token in label for token in ("省份", "所在省", "行政省")):
        return "province"
    if any(token in label for token in ("城市", "所在市", "行政市")):
        return "city"
    if any(token in label for token in ("区县", "县区", "行政区", "所在区", "所在县")):
        return "district"
    if any(token in label for token in ("地区", "区域", "行政区域")):
        return "region"
    return None


def _administrative_query_level(raw_value: str, user_query: str) -> str | None:
    """Return the admin level explicitly implied around a literal mention."""
    query = str(user_query or "")
    mentions = [raw_value]
    for suffix in _ADMINISTRATIVE_SUFFIXES:
        if raw_value.endswith(suffix) and len(raw_value) > len(suffix) + 1:
            mentions.append(raw_value[:-len(suffix)])
            break

    suffix_levels = (
        ("特别行政区", "province"),
        ("自治区", "province"),
        ("省", "province"),
        ("城市", "city"),
        ("市", "city"),
        ("区县", "district"),
        ("县", "district"),
        ("区", "district"),
        ("自治州", "region"),
        ("地区", "region"),
        ("区域", "region"),
    )
    for mention in dict.fromkeys(mentions):
        escaped = re.escape(mention)
        for suffix, level in suffix_levels:
            if re.search(rf"{escaped}\s*{re.escape(suffix)}", query):
                return level
        if re.search(
            rf"(?:位于|地处|所在(?:地区|区域)?|限定(?:地区|区域)?|在)\s*{escaped}",
            query,
        ):
            return "region"
        if re.search(
            rf"(?:推荐|筛选|查找|查询|提供|找出).{{0,12}}{escaped}"
            rf"(?:地区|区域)?\s*(?:合适|当地)",
            query,
        ):
            return "region"

    # A canonical value emitted by the model may include its suffix while the
    # user wrote the short form (for example 上海地区 -> 上海市).  Accept that
    # only when the short form itself occurs in a geographical construction.
    if _literal_is_explicitly_mentioned(raw_value, query):
        for suffix, level in suffix_levels:
            if raw_value.endswith(suffix):
                return level
    return None


def _administrative_value_candidates(
    raw_value: str,
    attribute: dict,
) -> list[str]:
    """Return standard administrative suffix variants permitted by metadata."""
    if _administrative_attribute_kind("unknown.value", attribute) is None:
        return []
    if raw_value.endswith(_ADMINISTRATIVE_SUFFIXES):
        return []
    return [raw_value + suffix for suffix in _ADMINISTRATIVE_SUFFIXES]


def _administrative_field_score(
    entity_code: str,
    field_kind: str,
    query_level: str,
    entity_distances: dict[str, int],
) -> tuple[int, int]:
    level_order = {
        "province": ("province", "region", "city", "district"),
        "city": ("city", "region", "province", "district"),
        "district": ("district", "city", "region", "province"),
        # An unqualified 地区/区域 means the broadest useful administrative
        # field when no explicit region field exists.
        "region": ("region", "province", "city", "district"),
    }[query_level]
    return entity_distances.get(entity_code, 10_000), level_order.index(field_kind)


def _result_entity_distances(
    ast: dict,
    knowledge: dict,
    attributes_by_entity: dict[str, dict[str, dict]],
    fallback_entity: str,
) -> dict[str, int]:
    """Measure undirected recalled-relation distance from requested entities."""
    seeds: set[str] = set()
    subject = ast.get("subject")
    subject_entity = (
        str(subject["entity"])
        if isinstance(subject, dict) and subject.get("entity")
        else ""
    )
    for dimension in ast.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        name = str(dimension.get("name") or "")
        for entity_code, attributes in attributes_by_entity.items():
            if name in attributes:
                seeds.add(entity_code)
    # A projected/grouped entity is the result object.  Geographic filters
    # such as “上海市经销商” must therefore prefer that entity's administrative
    # attribute over an equally distant fact/subject field.  Fall back to the
    # subject only when no concrete result dimension was requested.
    if not seeds and subject_entity:
        seeds.add(subject_entity)
    seeds.intersection_update(attributes_by_entity)
    if not seeds:
        seeds.add(fallback_entity)

    adjacency: dict[str, set[str]] = {
        entity_code: set() for entity_code in attributes_by_entity
    }
    for relation in knowledge.get("relations", []):
        metadata = getattr(relation, "metadata", {}) or {}
        source = str(
            metadata.get("parent")
            or metadata.get("source_entity")
            or metadata.get("source_entity_code")
            or ""
        )
        target = str(
            metadata.get("target_entity")
            or metadata.get("target_entity_code")
            or ""
        )
        if source in adjacency and target in adjacency:
            adjacency[source].add(target)
            adjacency[target].add(source)

    distances = {entity_code: 0 for entity_code in seeds}
    frontier = list(seeds)
    while frontier:
        source = frontier.pop(0)
        for target in adjacency.get(source, ()):
            if target in distances:
                continue
            distances[target] = distances[source] + 1
            frontier.append(target)
    return distances


def _normalize_entity_attribute_filters(
    ast: dict,
    knowledge: dict,
    user_query: str,
    *,
    semantic_model_id: int | None = None,
    business_domain_id: int | list[int] | tuple[int, ...] | None = None,
    exact_attribute_value_resolver=None,
    catalog_value_resolver=None,
) -> None:
    """Move a literal to its uniquely matching semantic attribute.

    The model can correctly recall an entity but place a structured value on
    its display-name field (for example a parent-brand value on manufacturer
    name).  This correction never uses a vocabulary embedded in agent code.
    It considers only attributes already authorized by retrieval and verifies
    the literal in the registered physical source.  Ordinary business values
    stay on the same entity.  Explicit geographical slots may traverse the
    recalled relation graph, but only to administrative fields and only through
    a deterministic distance/level ranking; equal-ranked matches still clarify.
    """
    filters = ast.get("filters")
    ambiguities = ast.get("ambiguity")
    if (
        not isinstance(filters, list)
        or not isinstance(ambiguities, list)
        or type(semantic_model_id) is not int
    ):
        return
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized_fields = _known_physical_fields(knowledge)
    resolver = (
        exact_attribute_value_resolver
        or resolve_exact_entity_attribute_value_fields
    )

    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").upper()
        value = item.get("value")
        if (
            field not in authorized_fields
            or operator not in {"=", "LIKE"}
            or not isinstance(value, str)
        ):
            continue
        raw_value = value.strip().strip("%")
        if len(raw_value) < 2 or "%" in raw_value or "_" in raw_value:
            continue
        administrative_level = _administrative_query_level(raw_value, user_query)
        if (
            not _literal_is_explicitly_mentioned(raw_value, user_query)
            and administrative_level is None
        ):
            continue

        owner_codes = {
            entity_code
            for entity_code, attributes in attributes_by_entity.items()
            if field in attributes
        }
        if len(owner_codes) != 1:
            continue
        entity_code = next(iter(owner_codes))
        entity_metadata = entities.get(entity_code, {})
        candidate_domain = (
            entity_metadata.get("business_domain_id")
            or entity_metadata.get("business_domain")
        )
        candidate_fields = [
            candidate_field
            for candidate_field in attributes_by_entity.get(entity_code, {})
            if candidate_field in authorized_fields
            and not _is_relationship_key_field(
                candidate_field, attributes_by_entity
            )
        ]
        if field not in candidate_fields:
            continue
        candidates = [
            {
                "entity_code": entity_code,
                "field": candidate_field,
                "business_domain_id": candidate_domain,
            }
            for candidate_field in candidate_fields
        ]

        # A geographical value must be resolved against administrative
        # attributes across the recalled relation graph, not against arbitrary
        # text attributes on whichever entity the model happened to choose.
        # This is intentionally before the generic same-entity correction.
        if administrative_level is not None:
            administrative_fields: dict[str, tuple[str, str, int | None]] = {}
            for candidate_entity, attributes in attributes_by_entity.items():
                metadata = entities.get(candidate_entity, {})
                domain = (
                    metadata.get("business_domain_id")
                    or metadata.get("business_domain")
                )
                for candidate_field, attribute in attributes.items():
                    if (
                        candidate_field not in authorized_fields
                        or _is_relationship_key_field(
                            candidate_field, attributes_by_entity
                        )
                    ):
                        continue
                    kind = _administrative_attribute_kind(
                        candidate_field, attribute
                    )
                    if kind is not None:
                        administrative_fields[candidate_field] = (
                            candidate_entity, kind, domain
                        )

            if administrative_fields:
                administrative_candidates = [
                    {
                        "entity_code": candidate_entity,
                        "field": candidate_field,
                        "business_domain_id": domain,
                    }
                    for candidate_field, (
                        candidate_entity, _kind, domain
                    ) in administrative_fields.items()
                ]

                def resolve_admin_value(candidate_value: str) -> set[str]:
                    return {
                        str(matched)
                        for matched in (resolver(
                            semantic_model_id,
                            business_domain_id,
                            administrative_candidates,
                            candidate_value,
                        ) or [])
                        if str(matched) in administrative_fields
                    }

                administrative_matches: set[tuple[str, str]] = set()
                try:
                    administrative_matches.update(
                        (matched_field, raw_value)
                        for matched_field in resolve_admin_value(raw_value)
                    )
                    if not administrative_matches and not raw_value.endswith(
                        _ADMINISTRATIVE_SUFFIXES
                    ):
                        for suffix in _ADMINISTRATIVE_SUFFIXES:
                            candidate_value = raw_value + suffix
                            administrative_matches.update(
                                (matched_field, candidate_value)
                                for matched_field in resolve_admin_value(
                                    candidate_value
                                )
                            )
                except Exception as exc:
                    logger.warning(
                        "administrative field resolution unavailable: sm=%s, error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    administrative_matches.clear()

                relevant_fields = set(candidate_fields).union(
                    administrative_fields
                )

                def attribute_choice_ambiguity(ambiguity: dict) -> bool:
                    if not _ambiguity_mentions_filter(
                        ambiguity,
                        field,
                        value,
                        relevant_fields,
                        require_value=True,
                    ):
                        return False
                    ambiguity_candidates = ambiguity.get("candidates")
                    if not isinstance(ambiguity_candidates, list) or not ambiguity_candidates:
                        return False
                    allowed = relevant_fields.union(
                        candidate.rsplit(".", 1)[-1]
                        for candidate in relevant_fields
                    )
                    return all(str(candidate) in allowed for candidate in ambiguity_candidates)

                if administrative_matches:
                    distances = _result_entity_distances(
                        ast,
                        knowledge,
                        attributes_by_entity,
                        entity_code,
                    )
                    scored = [
                        (
                            _administrative_field_score(
                                administrative_fields[matched_field][0],
                                administrative_fields[matched_field][1],
                                administrative_level,
                                distances,
                            ),
                            matched_field,
                            canonical_value,
                        )
                        for matched_field, canonical_value
                        in administrative_matches
                    ]
                    best_score = min(match[0] for match in scored)
                    best = [match for match in scored if match[0] == best_score]
                    if len(best) == 1:
                        _, matched_field, canonical_value = best[0]
                        item["field"] = matched_field
                        item["operator"] = "="
                        item["value"] = canonical_value
                        ambiguities[:] = [
                            ambiguity for ambiguity in ambiguities
                            if not attribute_choice_ambiguity(ambiguity)
                        ]
                        continue

                    ambiguities[:] = [
                        ambiguity for ambiguity in ambiguities
                        if not attribute_choice_ambiguity(ambiguity)
                    ]
                    choices = sorted(
                        f"{matched_field}={canonical_value}"
                        for _, matched_field, canonical_value in best
                    )
                    if not any(
                        isinstance(ambiguity, dict)
                        and ambiguity.get("type") == "filter"
                        and ambiguity.get("candidates") == choices
                        for ambiguity in ambiguities
                    ):
                        ambiguities.append({
                            "type": "filter",
                            "question": (
                                f"地区值 {raw_value} 在与结果对象等距的多个行政区字段中"
                                "精确存在，请确认行政层级。"
                            ),
                            "candidates": choices,
                        })
                    continue

                current_attribute = attributes_by_entity[entity_code][field]
                if _administrative_attribute_kind(
                    field, current_attribute
                ) is None:
                    # Never execute a geographic slot against a name/address
                    # field merely because that free text happens to contain
                    # the same place name.
                    choices = sorted(administrative_fields)
                    if not any(
                        isinstance(ambiguity, dict)
                        and ambiguity.get("type") == "filter"
                        and ambiguity.get("candidates") == choices
                        for ambiguity in ambiguities
                    ):
                        ambiguities.append({
                            "type": "filter",
                            "question": (
                                f"地区值 {raw_value} 未能在召回的行政区字段中唯一匹配，"
                                "请确认地区名称或行政层级。"
                            ),
                            "candidates": choices,
                        })
                    continue

        try:
            matched_fields = {
                str(matched)
                for matched in (resolver(
                    semantic_model_id,
                    business_domain_id,
                    candidates,
                    raw_value,
                ) or [])
                if str(matched) in candidate_fields
            }
        except Exception as exc:
            logger.warning(
                "exact entity-attribute resolution unavailable: sm=%s, error_type=%s",
                semantic_model_id,
                type(exc).__name__,
            )
            continue
        if field in matched_fields:
            # Source-backed exact existence is authoritative.  Leaving the
            # model's LIKE operator intact here broadened an exact catalog name
            # to sibling products (and even inverted terms such as 可吸收/不可吸收).
            # Preserve fuzzy matching only when exact source resolution fails.
            item["operator"] = "="
            item["value"] = raw_value
            continue
        if not matched_fields:
            attribute = attributes_by_entity[entity_code][field]
            canonical_values: list[str] = []
            for candidate_value in _administrative_value_candidates(
                raw_value,
                attribute,
            ):
                try:
                    resolved = resolver(
                        semantic_model_id,
                        business_domain_id,
                        [{
                            "entity_code": entity_code,
                            "field": field,
                            "business_domain_id": candidate_domain,
                        }],
                        candidate_value,
                    ) or []
                except Exception as exc:
                    logger.warning(
                        "administrative value resolution unavailable: sm=%s, error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    canonical_values = []
                    break
                if {str(matched) for matched in resolved} == {field}:
                    canonical_values.append(candidate_value)
            if len(canonical_values) == 1:
                item["operator"] = "="
                item["value"] = canonical_values[0]
                continue
            if len(canonical_values) > 1:
                ambiguities.append({
                    "type": "filter",
                    "question": (
                        f"地区简称 {raw_value} 对应多个目录规范值，请确认具体地区。"
                    ),
                    "candidates": canonical_values,
                })
                continue
        if len(matched_fields) == 1:
            matched_field = next(iter(matched_fields))
            item["field"] = matched_field
            item["operator"] = "="
            item["value"] = raw_value
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    matched_fields,
                    require_value=True,
                )
            ]
            continue
        if not matched_fields and administrative_level is None:
            relation_distances = _related_entity_distances(
                field,
                knowledge,
                attributes_by_entity,
                {},
            )
            related_candidates = _all_non_key_attribute_candidates(
                set(relation_distances),
                entities,
                attributes_by_entity,
                authorized_fields,
                "",
            )
            related_candidates = _catalog_identity_candidates(
                related_candidates, attributes_by_entity
            )
            related_fields = {
                candidate["field"] for candidate in related_candidates
            }
            catalog_matches: list[dict] = []
            if related_candidates:
                catalog_resolver = (
                    catalog_value_resolver
                    or resolve_entity_attribute_catalog_matches
                )
                try:
                    for offset in range(0, len(related_candidates), 32):
                        catalog_matches.extend(catalog_resolver(
                            semantic_model_id,
                            business_domain_id,
                            related_candidates[offset:offset + 32],
                            raw_value,
                        ) or [])
                except Exception as exc:
                    logger.warning(
                        "related filter catalog resolution unavailable: "
                        "sm=%s, error_type=%s",
                        semantic_model_id,
                        type(exc).__name__,
                    )
                    catalog_matches = []
            choice = _unique_catalog_match(catalog_matches, related_fields)
            if choice is not None:
                matched_field = str(choice["field"])
                canonical_value = str(choice["canonical_value"])
                item["field"] = matched_field
                item["operator"] = "="
                item["value"] = canonical_value
                evidence_fields = {
                    str(match.get("field") or "")
                    for match in catalog_matches
                    if isinstance(match, dict)
                    and str(match.get("field") or "") in related_fields
                }
                ambiguities[:] = [
                    ambiguity
                    for ambiguity in ambiguities
                    if not _ambiguity_mentions_filter(
                        ambiguity,
                        field,
                        value,
                        evidence_fields,
                        require_value=True,
                    )
                ]
                continue
            ranked_choices = sorted({
                f"{match.get('field')}={match.get('canonical_value')}"
                for match in catalog_matches
                if isinstance(match, dict)
                and str(match.get("field") or "") in related_fields
                and str(match.get("canonical_value") or "").strip()
            })
            if ranked_choices:
                _append_ambiguity(
                    ast,
                    "filter",
                    (
                        f"过滤值 {raw_value} 在关联实体目录中存在多个同等候选，"
                        "请确认具体商品、品牌或厂家口径。"
                    ),
                    ranked_choices,
                )
                continue
        if len(matched_fields) > 1:
            # Exact source existence on several governed attributes is the
            # strongest available ambiguity evidence.  Replace any earlier
            # fuzzy/catalog ambiguity for the same literal so users choose an
            # attribute, not an incidental source value discovered earlier in
            # normalization.
            ambiguities[:] = [
                ambiguity
                for ambiguity in ambiguities
                if not _ambiguity_mentions_filter(
                    ambiguity,
                    field,
                    value,
                    require_value=True,
                )
            ]
            ambiguities.append({
                "type": "filter",
                "question": (
                    f"过滤值 {raw_value} 在实体 {entity_code} 的多个属性中精确存在，"
                    "无法安全确定业务含义，请确认要筛选的属性。"
                ),
                "candidates": sorted(matched_fields),
            })


_DATE_TOKEN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*(?:年|[-/.])\s*"
    r"(0?[1-9]|1[0-2])\s*(?:月|[-/.])\s*"
    r"(0?[1-9]|[12]\d|3[01])\s*日?(?!\d)"
)
_YEAR_MONTH_TOKEN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*年\s*(0?[1-9]|1[0-2])\s*月(?!\s*\d)",
)
_YEAR_TOKEN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*年(?!\s*(?:\d|[一二三四五六七八九十]+)\s*月)",
)
_BOUNDED_RELATIVE_YEAR = re.compile(
    r"(?:近|最近|过去)\s*(?:1|一)\s*年|近\s*12\s*个?月",
    re.IGNORECASE,
)
_ONGOING_TIME_WORDING = re.compile(
    r"正在(?:销售|成交|合作)|当前(?:在售|销售中)|目前(?:在售|销售中)",
    re.IGNORECASE,
)
_EXPLICIT_TIME_WORDING = re.compile(
    r"今天|昨日|昨天|本周|上周|本月|上月|本季度|上季度|今年|去年|"
    r"近\s*\d+\s*(?:天|周|个?月|年)|最近|过去|截至|期间|日期|时间范围|"
    r"按(?:日|天|周|月|季度|年)|逐(?:日|周|月|年)",
    re.IGNORECASE,
)
_GENERIC_SALES_MEASURE = re.compile(
    r"销售(?:情况|趋势|表现|数据|分析)",
    re.IGNORECASE,
)
_EXPLICIT_SALES_MEASURE = re.compile(
    r"含税|不含税|销售(?:总)?额|销售金额|销量|销售(?:总)?数量|"
    r"订单(?:数|笔数)|销售成本|毛利|利润",
    re.IGNORECASE,
)


def _append_ambiguity(
    ast: dict,
    ambiguity_type: str,
    question: str,
    candidates: list[str],
) -> None:
    ambiguities = ast.get("ambiguity")
    if not isinstance(ambiguities, list):
        return
    normalized_candidates = list(dict.fromkeys(map(str, candidates)))
    if any(
        isinstance(item, dict)
        and str(item.get("type") or "") == ambiguity_type
        and list(map(str, item.get("candidates") or [])) == normalized_candidates
        for item in ambiguities
    ):
        return
    ambiguities.append({
        "type": ambiguity_type,
        "question": question,
        "candidates": normalized_candidates,
    })


def _query_date_bounds(user_query: str) -> tuple[str, str, str] | None:
    """Return only a date range that can be derived from the user's words."""
    parsed: list[date] = []
    for match in _DATE_TOKEN.finditer(str(user_query or "")):
        try:
            value = date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            continue
        if value not in parsed:
            parsed.append(value)
    if len(parsed) >= 2:
        start, end = parsed[0], parsed[1]
        if start <= end:
            return start.isoformat(), end.isoformat(), "custom"
        return None
    month_match = _YEAR_MONTH_TOKEN.search(str(user_query or ""))
    if month_match is not None:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return start.isoformat(), end.isoformat(), "custom"
    year_match = _YEAR_TOKEN.search(str(user_query or ""))
    if year_match is not None:
        year = int(year_match.group(1))
        return f"{year:04d}-01-01", f"{year + 1:04d}-01-01", "custom"
    if _BOUNDED_RELATIVE_YEAR.search(str(user_query or "")):
        end = date.today()
        try:
            start = end.replace(year=end.year - 1)
        except ValueError:
            start = end.replace(year=end.year - 1, day=28)
        return start.isoformat(), end.isoformat(), "range"
    return None


def _metric_time_anchor(metadata: dict) -> str | None:
    time_caliber = _json_value(metadata.get("time_caliber")) or {}
    anchor = (
        time_caliber.get("time_anchor")
        if isinstance(time_caliber, dict)
        else None
    )
    return (
        str(anchor)
        if isinstance(anchor, str) and _PHYSICAL_FIELD.fullmatch(anchor)
        else None
    )


def _is_precomputed_snapshot_metric(metadata: dict) -> bool:
    """Recognize a scalar snapshot metric from its registered definition."""
    if _metric_time_anchor(metadata) is not None:
        return False
    calculation = _json_value(metadata.get("calculation_rule")) or {}
    formula = (
        calculation.get("calc_formula")
        if isinstance(calculation, dict)
        else calculation
    )
    if not isinstance(formula, str):
        return False
    return bool(re.fullmatch(
        r"\s*(?:[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*)?"
        r"(?:MAX|MIN)\(\s*[A-Za-z_][A-Za-z0-9_]*\."
        r"[A-Za-z_][A-Za-z0-9_]*\s*\)\s*",
        formula,
        re.IGNORECASE,
    ))


def _normalize_all_time_snapshot_scope(
    content: str,
    knowledge: dict,
    *,
    all_time_scope: bool,
) -> str:
    """Remove model-invented transaction time from an all-time snapshot query."""
    if not all_time_scope:
        return content
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(ast, dict):
        return content
    selected_codes = {
        str(item.get("name"))
        for item in ast.get("metrics") or []
        if isinstance(item, dict) and item.get("name")
    }
    selected_metadata = [
        metadata
        for item in knowledge.get("metrics", [])
        for metadata in [getattr(item, "metadata", {}) or {}]
        if str(metadata.get("metric_code") or "") in selected_codes
    ]
    if (
        not selected_codes
        or len(selected_metadata) != len(selected_codes)
        or not all(_is_precomputed_snapshot_metric(item) for item in selected_metadata)
    ):
        return content
    ast["time_context"] = None
    dimensions = ast.get("dimensions")
    if isinstance(dimensions, list):
        ast["dimensions"] = [
            item for item in dimensions
            if not isinstance(item, dict) or not item.get("granularity")
        ]
    ambiguities = ast.get("ambiguity")
    if isinstance(ambiguities, list):
        ast["ambiguity"] = [
            item for item in ambiguities
            if not (
                isinstance(item, dict)
                and (
                    str(item.get("type") or "") in {"time", "time_anchor"}
                    or re.search(
                        r"时间|日期|time|date",
                        str(item.get("question") or ""),
                        re.IGNORECASE,
                    )
                )
            )
        ]
    return json.dumps(ast, ensure_ascii=False)


def _selected_metric_metadata(ast: dict, knowledge: dict) -> list[dict]:
    codes = {
        str(item.get("name"))
        for item in ast.get("metrics") or []
        if isinstance(item, dict) and item.get("name")
    }
    return [
        metadata
        for item in knowledge.get("metrics", [])
        for metadata in [getattr(item, "metadata", {}) or {}]
        if str(metadata.get("metric_code") or "") in codes
    ]


def _external_time_query(ast: dict, knowledge: dict, user_query: str) -> str:
    """Mask rolling-window words that belong to a precomputed metric name."""
    query = str(user_query or "")
    for metadata in _selected_metric_metadata(ast, knowledge):
        if not _is_precomputed_snapshot_metric(metadata):
            continue
        for term in sorted(_metric_terms(metadata), key=len, reverse=True):
            if not re.search(r"(?:最近|近|过去)\s*(?:\d+|[一二两三四五六七八九十]+)", term):
                continue
            query = re.sub(re.escape(term), " " * len(term), query, flags=re.IGNORECASE)
    return query


_TRANSACTION_ACTIVITY_WORDING = re.compile(
    r"TRANSACTION_TIME_SCOPE=SALES_RECORD|"
    r"正在销售|销售过|曾经销售|(?:有|存在)(?:过)?销售记录|"
    r"(?:期间|范围内).{0,12}(?:存在|有|发生).{0,12}销售|"
    r"发生过销售|"
    r"活跃(?:经销商|供应商|客户|门店).{0,20}(?:在)?销售"
)


def _uses_transaction_activity_definition(user_query: str) -> bool:
    """Whether partner eligibility is defined by dated transaction facts."""
    return bool(_TRANSACTION_ACTIVITY_WORDING.search(str(user_query or "")))


def _time_dimension_candidates(knowledge: dict) -> list[tuple[str, dict, dict]]:
    """Return only registered physical fields from recalled time dimensions."""
    candidates: list[tuple[str, dict, dict]] = []
    authorized = _known_physical_fields(knowledge)
    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        semantic_text = " ".join(str(metadata.get(key) or "") for key in (
            "dim_code", "dim_name", "dim_type", "synonyms", "business_definition",
        ))
        granularities = _json_value(metadata.get("granularity_support")) or []
        if not (
            granularities
            or re.search(r"时间|日期|time|date", semantic_text, re.IGNORECASE)
        ):
            continue
        for binding in _json_value(metadata.get("bind_entities")) or []:
            if not isinstance(binding, dict):
                continue
            table = binding.get("mappingTable")
            column = binding.get("mappingColumn")
            field = f"{table}.{column}" if table and column else ""
            if field in authorized and _PHYSICAL_FIELD.fullmatch(field):
                candidates.append((field, metadata, binding))
    return candidates


def _recalled_relation_table_graph(knowledge: dict) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for item in knowledge.get("relations", []):
        metadata = getattr(item, "metadata", {}) or {}
        join_key = _json_value(metadata.get("join_key")) or {}
        if not isinstance(join_key, dict):
            continue
        source = _physical_field_from_mapping(join_key.get("source_field"))
        target = _physical_field_from_mapping(join_key.get("target_field"))
        if not source or not target:
            continue
        source_table, target_table = source.split(".", 1)[0], target.split(".", 1)[0]
        graph.setdefault(source_table, set()).add(target_table)
        graph.setdefault(target_table, set()).add(source_table)
    return graph


def _ast_referenced_tables(ast: dict, knowledge: dict) -> set[str]:
    tables = {
        value.split(".", 1)[0]
        for key in ("dimensions", "filters")
        for item in ast.get(key) or []
        if isinstance(item, dict)
        for value in [str(item.get("name") or item.get("field") or "")]
        if _PHYSICAL_FIELD.fullmatch(value)
    }
    for metadata in _selected_metric_metadata(ast, knowledge):
        calculation = _json_value(metadata.get("calculation_rule")) or {}
        formula = calculation.get("calc_formula") if isinstance(calculation, dict) else ""
        tables.update(
            match.split(".", 1)[0]
            for match in re.findall(
                r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
                str(formula or ""),
            )
        )
    return tables


def _table_is_connected(
    table: str,
    referenced_tables: set[str],
    graph: dict[str, set[str]],
    *,
    max_hops: int = 4,
) -> bool:
    if not referenced_tables:
        return False
    if table in referenced_tables:
        return True
    visited = {table}
    frontier = {table}
    for _ in range(max_hops):
        frontier = {
            neighbor
            for current in frontier
            for neighbor in graph.get(current, set())
            if neighbor not in visited
        }
        if frontier.intersection(referenced_tables):
            return True
        if not frontier:
            return False
        visited.update(frontier)
    return False


def _transaction_time_anchor(
    ast: dict, knowledge: dict, user_query: str
) -> str | None:
    """Resolve a fact time field by registered semantics and recalled lineage.

    This path is used only when the user explicitly defines eligibility through
    sales/transaction records.  A physical-looking model value is never trusted:
    every candidate must be a recalled time-dimension binding, connected to the
    current query through recalled relations, and have a unique semantic score.
    """
    if not _uses_transaction_activity_definition(user_query):
        return None
    referenced_tables = _ast_referenced_tables(ast, knowledge)
    graph = _recalled_relation_table_graph(knowledge)
    query = _semantic_label(user_query)
    event_groups = (
        ("销售", "订单", "交易", "成交"),
        ("退款", "退货", "售后"),
        ("付款", "支付", "收款"),
        ("发货", "配送", "物流"),
        ("库存", "入库", "出库"),
    )
    scored: list[tuple[int, str]] = []
    for field, metadata, binding in _time_dimension_candidates(knowledge):
        if not _table_is_connected(
            field.split(".", 1)[0], referenced_tables, graph
        ):
            continue
        raw_terms: list[str] = []
        for key in ("dim_code", "dim_name", "synonyms", "business_definition"):
            raw_terms.extend(PromptBuilder._term_values(metadata.get(key)))
        for key in ("attrName", "entityName"):
            raw_terms.extend(PromptBuilder._term_values(binding.get(key)))
        raw_terms.extend((field, field.replace("_", " ")))
        labels = {_semantic_label(term) for term in raw_terms if _semantic_label(term)}
        score = sum(
            100 + len(label)
            for label in labels
            if len(label) >= 2 and label in query
        )
        candidate_text = "".join(labels)
        for group in event_groups:
            if any(token in query for token in group) and any(
                token in candidate_text for token in group
            ):
                score += 20
        if score > 0:
            scored.append((score, field))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    winners = {field for score, field in scored if score == best}
    return next(iter(winners)) if len(winners) == 1 else None


def _unique_query_time_anchor(
    ast: dict, knowledge: dict, user_query: str = ""
) -> str | None:
    selected_codes = {
        str(item.get("name"))
        for item in ast.get("metrics") or []
        if isinstance(item, dict) and item.get("name")
    }
    selected_anchors = {
        anchor
        for item in knowledge.get("metrics", [])
        if str((getattr(item, "metadata", {}) or {}).get("metric_code"))
        in selected_codes
        for anchor in [_metric_time_anchor(getattr(item, "metadata", {}) or {})]
        if anchor
    }
    if len(selected_anchors) == 1:
        return next(iter(selected_anchors))
    if len(selected_anchors) > 1:
        return None

    transaction_anchor = _transaction_time_anchor(ast, knowledge, user_query)
    if transaction_anchor is not None:
        return transaction_anchor

    # A snapshot/profile metric may be sorted together with a transaction-time
    # predicate. In that case only a unique recalled time dimension or recalled
    # transaction metric may authorize the anchor; no table/column is guessed.
    candidates: set[str] = set()
    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        for binding in _json_value(metadata.get("bind_entities")) or []:
            if not isinstance(binding, dict):
                continue
            table = binding.get("mappingTable")
            column = binding.get("mappingColumn")
            field = f"{table}.{column}" if table and column else ""
            if _PHYSICAL_FIELD.fullmatch(field):
                candidates.add(field)
    if not candidates:
        candidates = {
            anchor
            for item in knowledge.get("metrics", [])
            for anchor in [_metric_time_anchor(getattr(item, "metadata", {}) or {})]
            if anchor
        }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _registered_subject_time_anchor(
    ast: dict,
    knowledge: dict,
    semantic_model_id: int | None,
    table_field_loader=None,
    *,
    domain_scope=None,
) -> str | None:
    """Resolve one event-time field from the subject's registered source table.

    Entity attributes are the public projection/filter contract, while the
    physical field registry can additionally contain the business timestamp
    needed to apply a user-supplied range.  Use that registry only when the ASL
    subject resolves to exactly one physical table and one field has a unique,
    business-time-preferred score. Audit/synchronization timestamps are never
    eligible fallbacks.
    """
    if type(semantic_model_id) is not int:
        return None
    subject = ast.get("subject")
    entity_code = (
        str(subject.get("entity") or "").strip()
        if isinstance(subject, dict)
        else ""
    )
    if not entity_code:
        return None
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    if entity_code not in entities:
        return None
    tables = {
        field.split(".", 1)[0]
        for field in attributes_by_entity.get(entity_code, {})
        if _PHYSICAL_FIELD.fullmatch(field)
    }
    if len(tables) != 1:
        return None
    table_name = next(iter(tables))
    loader = table_field_loader or get_table_field_by_scope
    try:
        catalog = (loader(semantic_model_id, None, business_domain_id=domain_scope)
                   if domain_scope is not None else loader(semantic_model_id, None))
    except (
        ConnectionError,
        TimeoutError,
        OSError,
        RuntimeError,
        MySQLInterfaceError,
        MySQLOperationalError,
    ) as exc:
        logger.warning(
            "registered time-anchor lookup unavailable: sm=%s entity=%s error_type=%s",
            semantic_model_id,
            entity_code,
            type(exc).__name__,
        )
        return None

    eligible_types = {"DATE", "DATETIME", "TIMESTAMP"}
    audit_tokens = (
        "sync", "create", "created", "update", "updated", "modify",
        "modified", "import", "ingest", "load", "etl", "publish",
    )
    preferred_tokens = (
        "collection", "collect", "sample", "measure", "detect",
        "observe", "event", "transaction", "occur", "recorded",
    )
    scored: list[tuple[int, str, dict]] = []
    for table in catalog.get("tables", []) if isinstance(catalog, dict) else []:
        if (
            not isinstance(table, dict)
            or str(table.get("table_name") or "") != table_name
            or table.get("semantic_model_id") != semantic_model_id
        ):
            continue
        for field in table.get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("field_name") or "").strip()
            field_type = str(field.get("data_type") or "").upper()
            lowered = name.casefold()
            if (
                not name
                or field_type not in eligible_types
                or any(token in lowered for token in audit_tokens)
            ):
                continue
            score = 100
            if any(token in lowered for token in preferred_tokens):
                score += 300
            elif lowered in {"business_date", "data_date", "stat_date"}:
                score += 200
            scored.append((score, f"{table_name}.{name}", field))
    if not scored:
        return None
    best_score = max(item[0] for item in scored)
    winners = [item for item in scored if item[0] == best_score]
    if len(winners) != 1:
        return None
    _, resolved, field_metadata = winners[0]
    # Make the SQL-registered anchor visible to the ordinary ASL validator.
    knowledge.setdefault("attributes", []).append(SimpleNamespace(
        id=f"registered-time-anchor:{semantic_model_id}:{resolved}",
        score=1.0,
        text=resolved,
        metadata={
            "type": "attribute",
            "parent": entity_code,
            "attr_code": str(field_metadata.get("field_name") or ""),
            "attr_name": str(
                field_metadata.get("comment")
                or field_metadata.get("field_name")
                or ""
            ),
            "field_mapping": resolved,
            "semantic_role": "time_anchor",
            "business_domain_id": entities[entity_code].get("business_domain_id")
            or entities[entity_code].get("business_domain"),
            "semantic_model_id": semantic_model_id,
        },
    ))
    return resolved


def _normalize_time_context(ast: dict, knowledge: dict, user_query: str) -> None:
    """Build/repair time_context only from query-verifiable dates and metadata."""
    time_context = ast.get("time_context")
    temporal_query = _external_time_query(ast, knowledge, user_query)
    bounds = _query_date_bounds(temporal_query)
    explicit_ongoing = bool(_ONGOING_TIME_WORDING.search(temporal_query))
    anchor = _unique_query_time_anchor(ast, knowledge, temporal_query)
    registered_time_fields = {
        field for field, _, _ in _time_dimension_candidates(knowledge)
    }
    incoming_anchor = (
        str(time_context.get("anchor") or "")
        if isinstance(time_context, dict)
        else ""
    )
    # An incoming anchor is acceptable only when it is an exact field published
    # by the recalled semantic model.  This preserves an already resolved
    # caller contract without allowing the model to invent a physical column.
    if anchor is None and incoming_anchor in registered_time_fields:
        anchor = incoming_anchor

    # A metric-level anchor is an optional override, never an LLM-owned field.
    # Remove it when the user did not ask for a time scope; otherwise replace it
    # only with the anchor registered for that exact metric.  The query-level
    # time_context remains the authoritative execution scope.
    metadata_by_code = {
        str((getattr(item, "metadata", {}) or {}).get("metric_code")): (
            getattr(item, "metadata", {}) or {}
        )
        for item in knowledge.get("metrics", [])
        if (getattr(item, "metadata", {}) or {}).get("metric_code")
    }
    for metric in ast.get("metrics") or []:
        if not isinstance(metric, dict) or "time_anchor" not in metric:
            continue
        registered_anchor = _metric_time_anchor(
            metadata_by_code.get(str(metric.get("name") or ""), {})
        )
        if bounds is not None and registered_anchor is not None:
            metric["time_anchor"] = registered_anchor
        else:
            metric.pop("time_anchor", None)

    # A syntactically valid time_context is still untrusted when no temporal
    # constraint can be derived from the caller's question.  Discard model-
    # invented ranges for ordinary grouped, cumulative and distinct-count
    # queries instead of failing later on a fabricated anchor.
    if bounds is None and not explicit_ongoing:
        ast["time_context"] = None
        if not _EXPLICIT_TIME_WORDING.search(str(user_query or "")):
            ambiguities = ast.get("ambiguity")
            if isinstance(ambiguities, list):
                ast["ambiguity"] = [
                    item for item in ambiguities
                    if not (
                        isinstance(item, dict)
                        and (
                            str(item.get("type") or "") in {"time", "time_anchor"}
                            or re.search(
                                r"时间|日期|time|date",
                                str(item.get("question") or ""),
                                re.IGNORECASE,
                            )
                        )
                    )
                ]
        return

    if bounds is not None:
        if anchor is None:
            logger.info(
                "ASL time-anchor unresolved: transaction_scope=%s, "
                "registered_candidates=%s, referenced_tables=%s",
                _uses_transaction_activity_definition(temporal_query),
                sorted({field for field, _, _ in _time_dimension_candidates(knowledge)}),
                sorted(_ast_referenced_tables(ast, knowledge)),
            )
            ast["time_context"] = None
            _append_ambiguity(
                ast,
                "time_anchor",
                "已识别时间范围，但当前召回元数据不能唯一确定交易时间字段，请确认时间口径。",
                [],
            )
            return
        ambiguities = ast.get("ambiguity")
        if isinstance(ambiguities, list):
            ast["ambiguity"] = [
                item for item in ambiguities
                if not (
                    isinstance(item, dict)
                    and (
                        str(item.get("type") or "") in {"time", "time_anchor"}
                        or re.search(
                            r"时间字段|时间口径|交易时间|time.?anchor",
                            str(item.get("question") or ""),
                            re.IGNORECASE,
                        )
                    )
                )
            ]
        start, end, time_type = bounds
        requested_granularities = {
            str(item.get("granularity") or "")
            for item in ast.get("dimensions") or []
            if isinstance(item, dict)
        }
        unit = (
            "month"
            if "month" in requested_granularities
            or re.search(r"按月|逐月|月度", user_query)
            else "day"
        )
        ast["time_context"] = {
            "type": time_type,
            "start": start,
            "end": end,
            "value": None,
            "unit": unit,
            "anchor": anchor,
        }
        return

    if explicit_ongoing:
        ast["time_context"] = None
        _append_ambiguity(
            ast,
            "time_anchor",
            "“正在销售”缺少可执行的时间范围，请提供开始日期和结束日期。",
            ["明确日期范围"],
        )
        return

    if time_context is None:
        return
    if not isinstance(time_context, dict):
        return

    allowed_types = {
        "this_month", "last_month", "this_year", "year", "custom",
        "range", "today", "yesterday",
    }
    if str(time_context.get("type") or "") not in allowed_types:
        # An unknown relative label is never silently reinterpreted. Without a
        # user-verifiable range it is a hallucinated constraint, so discard it.
        if ast.get("version") == "2.0" and ast.get("intent") == "query":
            ast["time_context"] = None
        return
    if anchor is not None:
        time_context["anchor"] = anchor


def _is_last_activity_date_attribute(attribute: dict) -> bool:
    semantics = " ".join(str(attribute.get(key) or "") for key in (
        "attr_code", "attr_name", "description",
    )).casefold()
    activity = "活跃" in semantics or "activity" in semantics
    last_order_time = any(token in semantics for token in (
        "最近一次订单时间", "最近订单时间", "最后订单时间",
        "last order time", "last_order_time", "last order date",
    ))
    return activity and last_order_time


def _activity_date_fields(knowledge: dict) -> set[str]:
    _, attributes_by_entity = _scoped_entity_attributes(knowledge)
    authorized = _known_physical_fields(knowledge)
    return {
        field
        for attributes in attributes_by_entity.values()
        for field, attribute in attributes.items()
        if field in authorized and _is_last_activity_date_attribute(attribute)
    }


def _normalize_activity_semantics(ast: dict, knowledge: dict, user_query: str) -> None:
    """Represent “active” only by a retrieved last-order date attribute."""
    transaction_activity = _uses_transaction_activity_definition(user_query)
    if "活跃" not in str(user_query or "") and not transaction_activity:
        return
    filters = ast.get("filters")
    ambiguities = ast.get("ambiguity")
    time_context = ast.get("time_context")
    if not isinstance(filters, list) or not isinstance(ambiguities, list):
        return
    bounds = (
        [time_context.get("start"), time_context.get("end")]
        if isinstance(time_context, dict)
        and time_context.get("start")
        and time_context.get("end")
        else None
    )
    fields = _activity_date_fields(knowledge)
    active_filters = [
        item
        for item in filters
        if isinstance(item, dict)
        and (
            "activ" in str(item.get("field") or "").casefold()
            or "活跃" in str(item.get("value") or "")
            or (
                bool(re.search(
                    r"(?:^|[._])(?:business_)?status$",
                    str(item.get("field") or ""),
                    re.IGNORECASE,
                ))
                and isinstance(item.get("value"), str)
                and not _literal_is_explicitly_mentioned(
                    str(item["value"]).strip().strip("%"),
                    user_query,
                )
            )
        )
    ]
    if transaction_activity:
        # In wording such as “partners that sold this product during the last
        # year”, active is an existence condition on the dated transaction fact,
        # not a separate profile label or last-activity threshold.  The bounded
        # time_context supplies that fact window; invented status/date filters
        # would incorrectly narrow the eligible rows a second time.
        filters[:] = [item for item in filters if item not in active_filters]
        ast["ambiguity"] = [
            item for item in ambiguities
            if not (
                isinstance(item, dict)
                and str(item.get("type") or "") == "filter"
                and re.search(
                    r"活跃|activity|activity_level|最近订单|活跃阈值",
                    str(item.get("question") or ""),
                    re.IGNORECASE,
                )
            )
        ]
        return
    if bounds is not None and len(fields) == 1:
        field = next(iter(fields))
        replacement = {"field": field, "operator": "BETWEEN", "value": bounds}
        if active_filters:
            first = filters.index(active_filters[0])
            filters[:] = [item for item in filters if item not in active_filters]
            filters.insert(first, replacement)
        elif replacement not in filters:
            filters.append(replacement)
        return

    # Invalid/invented active-status filters must not make it past the validator.
    # Remove only those filters, then produce a non-executable clarification AST.
    filters[:] = [item for item in filters if item not in active_filters]
    candidates = sorted(fields)
    _append_ambiguity(
        ast,
        "filter",
        (
            "当前元数据无法用唯一的最近订单日期字段证明“活跃”，请确认活跃口径和时间范围。"
            if bounds is not None
            else "“活跃”缺少明确时间范围，请确认活跃口径和开始、结束日期。"
        ),
        candidates,
    )


def _metadata_term_values(value) -> list[str]:
    """Read code/name/synonym terms without depending on PromptBuilder state."""
    parsed = _json_value(value)
    if isinstance(parsed, (list, tuple, set)):
        result: list[str] = []
        for item in parsed:
            result.extend(_metadata_term_values(item))
        return result
    if parsed is None:
        return []
    text = str(parsed).strip()
    if not text:
        return []
    return [
        part.strip()
        for part in re.split(r"[,，;；|\n]+", text)
        if part.strip()
    ]


def _sales_metric_candidates(knowledge: dict) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for item in knowledge.get("metrics", []):
        metadata = getattr(item, "metadata", {}) or {}
        code = str(metadata.get("metric_code") or "").strip()
        name = str(metadata.get("metric_name") or "").strip()
        terms = [name, *_metadata_term_values(metadata.get("synonyms"))]
        semantic_text = " ".join(terms)
        if code and re.search(
            r"销售(?:总)?额|销售金额|销量|销售(?:总)?数量",
            semantic_text,
            re.IGNORECASE,
        ):
            candidates.append((code, name or code))
    return list(dict.fromkeys(candidates))


def _normalize_exploration_metrics(
    content: str,
    knowledge: dict,
    requirements: dict,
) -> str:
    """Keep an open exploration inside its actually recalled metric scope.

    Open reports intentionally leave metric choice to Oagnet, but vector order
    is not semantic evidence.  A model-written metric is retained only when its
    code is recalled or its emitted code/alias exactly and uniquely matches a
    recalled metric code, name, or synonym.  Anything else becomes an explicit
    non-executable ambiguity; unrelated recalled metrics are never used merely
    to satisfy the minimum count.
    """
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(ast, dict) or not isinstance(ast.get("metrics"), list):
        return content

    try:
        minimum = int(requirements["minimum_numeric_metrics"])
        maximum = int(requirements["maximum_numeric_metrics"])
    except (KeyError, TypeError, ValueError):
        return content

    recalled: list[tuple[str, dict]] = []
    seen_recalled: set[str] = set()
    for item in knowledge.get("metrics", []):
        metadata = getattr(item, "metadata", {}) or {}
        code = str(metadata.get("metric_code") or "").strip()
        if code and code not in seen_recalled:
            recalled.append((code, metadata))
            seen_recalled.add(code)
    metadata_by_code = dict(recalled)

    term_index: dict[str, set[str]] = {}
    for code, metadata in recalled:
        for term in _metric_terms(metadata):
            folded = str(term).strip().casefold()
            if folded:
                term_index.setdefault(folded, set()).add(code)

    selected: list[dict] = []
    selected_codes: set[str] = set()
    removed_codes: set[str] = set()
    unresolved_labels: list[str] = []
    unresolved_candidates: set[str] = set()
    rewritten_terms: dict[str, str] = {}
    for item in ast["metrics"]:
        if not isinstance(item, dict):
            continue
        emitted_code = str(item.get("name") or "").strip()
        emitted_terms = {
            str(term).strip()
            for key in ("name", "alias")
            for term in _metadata_term_values(item.get(key))
            if str(term).strip()
        }
        matched_codes: set[str] = set()
        if emitted_code in metadata_by_code:
            matched_codes.add(emitted_code)
        else:
            for term in emitted_terms:
                matched_codes.update(term_index.get(term.casefold(), set()))
        if len(matched_codes) != 1:
            if emitted_code:
                removed_codes.add(emitted_code)
            emitted_alias = str(item.get("alias") or "").strip()
            label = (
                f"{emitted_code}（{emitted_alias}）"
                if emitted_code and emitted_alias and emitted_code != emitted_alias
                else emitted_alias or emitted_code or "未命名指标"
            )
            unresolved_labels.append(label)
            for code in matched_codes:
                metadata = metadata_by_code[code]
                unresolved_candidates.add(
                    f"{code}（{metadata.get('metric_name') or code}）"
                )
            continue
        code = next(iter(matched_codes))
        for term in emitted_terms:
            rewritten_terms[term.casefold()] = code
        if code in selected_codes or len(selected) >= maximum:
            continue
        metadata = metadata_by_code[code]
        replacement = dict(item)
        replacement["name"] = code
        replacement["alias"] = str(metadata.get("metric_name") or code)
        selected.append(replacement)
        selected_codes.add(code)

    ast["metrics"] = selected[:maximum]
    sort = ast.get("sort")
    if isinstance(sort, dict) and str(sort.get("field_type") or "") in {
        "metric", "field", "",
    }:
        current = str(sort.get("field") or "").strip()
        target = (
            current
            if current in selected_codes
            else rewritten_terms.get(current.casefold())
        )
        if target in selected_codes:
            sort["field"] = target
            sort["field_type"] = "metric"
        elif current in removed_codes or current not in selected_codes:
            ast["sort"] = None

    needs_clarification = bool(unresolved_labels) or len(ast["metrics"]) < minimum
    ambiguities = ast.get("ambiguity")
    if needs_clarification and isinstance(ambiguities, list):
        if not unresolved_candidates:
            unresolved_candidates.update(
                f"{code}（{metadata.get('metric_name') or code}）"
                for code, metadata in recalled[:12]
            )
        labels = "、".join(dict.fromkeys(unresolved_labels)) or "当前报告指标"
        existing = next((
            item for item in ambiguities
            if isinstance(item, dict)
            and item.get("reason_code") == "UNRESOLVED_EXPLORATION_METRIC"
        ), None)
        if existing is not None and not unresolved_labels:
            return json.dumps(ast, ensure_ascii=False)
        payload = {
            "type": "metric",
            "reason_code": "UNRESOLVED_EXPLORATION_METRIC",
            "question": (
                f"指标 {labels} 无法通过当前召回指标的 code/name/synonym "
                "精确唯一映射，请明确报告所需的规范指标口径。"
            ),
            "candidates": sorted(unresolved_candidates),
        }
        if existing is None:
            ambiguities.append(payload)
        else:
            existing.update(payload)
    elif len(ast["metrics"]) >= minimum and isinstance(ambiguities, list):
        # Metric selection is part of the caller-authorized exploration shape;
        # remove model-created uncertainty only after every emitted metric has
        # deterministic recalled identity evidence.  A previous deterministic
        # ambiguity survives the function's second normalization pass.
        ast["ambiguity"] = [
            item for item in ambiguities
            if not isinstance(item, dict)
            or item.get("type") != "metric"
            or item.get("reason_code") == "UNRESOLVED_EXPLORATION_METRIC"
        ]
    return json.dumps(ast, ensure_ascii=False)


def _normalize_generic_sales_metric(ast: dict, knowledge: dict, user_query: str) -> None:
    """Do not silently equate generic “sales” with amount, quantity, or cost."""
    query = str(user_query or "")
    if (
        not _GENERIC_SALES_MEASURE.search(query)
        or _EXPLICIT_SALES_MEASURE.search(query)
        or re.search(r"(?:正在|在|有)销售", query)
    ):
        return
    candidates = _sales_metric_candidates(knowledge)
    if len(candidates) <= 1:
        return
    _append_ambiguity(
        ast,
        "metric",
        "“销售”可能指金额或数量，请确认本次趋势/分析采用的规范指标。",
        [f"{code}（{name}）" for code, name in candidates],
    )


def _metric_exact_codes(knowledge: dict, user_query: str) -> set[str]:
    """Find retrieved metric codes explicitly named by name/synonym in the query."""
    if not user_query:
        return set()
    query = user_query.casefold()
    mentions: list[tuple[str, int, int]] = []
    for item in knowledge.get("metrics", []):
        metadata = getattr(item, "metadata", {}) or {}
        code = metadata.get("metric_code")
        if not code:
            continue
        values = []
        for key in ("metric_name", "synonyms", "metric_code"):
            values.extend(_metadata_term_values(metadata.get(key)))
        for term in set(values):
            folded = term.casefold()
            if len(folded) < 2:
                continue
            # Bare action/relationship verbs are not exact metric requests.
            # For example, “销售过某产品的经销商名单” asks for rows, not sales
            # amount or quantity. Explicit measures such as 销售额/销售量 remain.
            if folded in {
                "销售", "合作", "购买", "采购", "交易", "下单", "退款",
                "sales", "sale",
            }:
                continue
            start = 0
            while True:
                index = query.find(folded, start)
                if index < 0:
                    break
                mentions.append((str(code), index, index + len(folded)))
                start = index + 1

    # A short synonym embedded entirely inside a longer exact metric name does
    # not represent a second requested metric ("销售额" inside "商品销售额").
    # If the short term appears separately elsewhere, that distinct span is kept.
    result: set[str] = set()
    for code, start, end in mentions:
        shadowed = any(
            other_code != code
            and other_start <= start
            and other_end >= end
            and (other_end - other_start) > (end - start)
            for other_code, other_start, other_end in mentions
        )
        if not shadowed:
            result.add(code)
    return result


def _metric_terms(metadata: dict) -> set[str]:
    values: list[str] = []
    for key in ("metric_code", "metric_name", "synonyms"):
        values.extend(_metadata_term_values(metadata.get(key)))
    return {str(value).strip() for value in values if str(value).strip()}


def _ranking_metric_code(
    metric_metadata: dict[str, dict],
    user_query: str,
) -> str | None:
    """Resolve the metric immediately governing an explicit rank/sort phrase."""
    query = str(user_query or "").casefold()
    markers = list(re.finditer(r"排名|排行|排序|top\s*\d*|bottom\s*\d*", query, re.IGNORECASE))
    if not markers:
        return None
    candidates: list[tuple[int, int, str]] = []
    for code, metadata in metric_metadata.items():
        for term in _metric_terms(metadata):
            folded = term.casefold()
            if len(folded) < 2:
                continue
            start = 0
            while True:
                index = query.find(folded, start)
                if index < 0:
                    break
                end = index + len(folded)
                following = [marker.start() for marker in markers if marker.start() >= end]
                if following:
                    candidates.append((min(following) - end, -len(folded), code))
                start = index + 1
    if not candidates:
        return None
    best_score = min((distance, negative_length) for distance, negative_length, _ in candidates)
    best_codes = {
        code
        for distance, negative_length, code in candidates
        if (distance, negative_length) == best_score
    }
    return next(iter(best_codes)) if len(best_codes) == 1 else None


def _normalize_caller_bound_metrics(
    content: str,
    knowledge: dict,
    preferred_metric_codes: list[str] | tuple[str, ...],
    user_query: str,
) -> str:
    """Apply caller-resolved metrics as an authoritative execution contract.

    The caller's metric IDs have already passed semantic resolution.  The LLM
    chooses dimensions and filters, but it must not replace those metric IDs.
    Rebuilding the small metric list from retrieved metadata is deterministic
    and keeps the existing fail-closed check for missing metadata.
    """
    codes = list(dict.fromkeys(map(str, preferred_metric_codes or [])))
    if not codes:
        return content
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(ast, dict):
        return content
    metadata_by_code = {
        str(metadata.get("metric_code")): metadata
        for item in knowledge.get("metrics", [])
        for metadata in [getattr(item, "metadata", {}) or {}]
        if metadata.get("metric_code")
        and str(metadata.get("metric_code")) in codes
    }
    missing = [code for code in codes if code not in metadata_by_code]
    if missing:
        raise ValueError(
            "caller-bound metric metadata is unavailable: " + ",".join(missing)
        )
    ast["metrics"] = [
        {
            "name": code,
            "alias": str(metadata_by_code[code].get("metric_name") or code),
        }
        for code in codes
    ]
    ambiguities = ast.get("ambiguity")
    if isinstance(ambiguities, list):
        ast["ambiguity"] = [
            item for item in ambiguities
            if not isinstance(item, dict) or item.get("type") != "metric"
        ]

    sort = ast.get("sort")
    if isinstance(sort, dict) and str(sort.get("field_type") or "") in {
        "metric", "field",
    }:
        current = str(sort.get("field") or "").strip()
        target = current if current in metadata_by_code else None
        if target is None:
            exact_term_matches = {
                code
                for code, metadata in metadata_by_code.items()
                if current.casefold() in {
                    term.casefold() for term in _metric_terms(metadata)
                }
            }
            if len(exact_term_matches) == 1:
                target = next(iter(exact_term_matches))
        if target is None and sort.get("field_type") == "field":
            formula_matches = {
                code
                for code, metadata in metadata_by_code.items()
                for formula in [_json_value(metadata.get("calculation_rule")) or {}]
                if isinstance(formula, dict)
                and current in re.findall(
                    r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b",
                    str(formula.get("calc_formula") or ""),
                )
            }
            if len(formula_matches) == 1:
                target = next(iter(formula_matches))
        if target is None:
            target = (
                codes[0]
                if len(codes) == 1
                else _ranking_metric_code(metadata_by_code, user_query)
            )
        if target is not None:
            sort["field_type"] = "metric"
            sort["field"] = target
    return json.dumps(ast, ensure_ascii=False)


def _validate_asl_output(
    content: str,
    knowledge: dict,
    user_query: str = "",
    required_metric_codes: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Reject malformed or invented semantic references before SQL translation."""
    try:
        ast = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("model output is not valid ASL JSON") from exc
    if not isinstance(ast, dict):
        raise ValueError("ASL root must be an object")
    if ast.get("version") != "2.0" or ast.get("intent") != "query":
        raise ValueError("ASL version/intent is invalid")
    for name in ("metrics", "dimensions", "filters", "having", "ambiguity"):
        if not isinstance(ast.get(name), list):
            raise ValueError(f"ASL {name} must be a list")

    known_metrics = _known_codes(knowledge, "metrics", "metric_code")
    selected_metrics: set[str] = set()
    for item in ast["metrics"]:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("ASL metric name is required")
        if str(item["name"]) not in known_metrics:
            raise ValueError(f"ASL metric was not retrieved from semantic scope: {item['name']}")
        if str(item["name"]) in selected_metrics:
            raise ValueError(f"ASL metric is duplicated: {item['name']}")
        selected_metrics.add(str(item["name"]))

    # Metrics resolved by the caller's semantic service are authoritative.
    # Do not re-expand short/common synonyms from the enriched retrieval text
    # (for example “合作” matching both 合作医院数 and 合作经销商数), which can
    # manufacture false extra requirements after the caller already selected
    # one exact metric ID.
    exact_metrics = (
        set(map(str, required_metric_codes))
        if required_metric_codes is not None
        else _metric_exact_codes(knowledge, user_query)
    )
    missing_exact = exact_metrics - selected_metrics
    if missing_exact:
        raise ValueError(
            "ASL omitted metrics explicitly named in the question: "
            + ", ".join(sorted(missing_exact))
        )

    subject = ast.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("ASL subject must be an object")
    subject_entity = subject.get("entity")
    if selected_metrics and not subject_entity:
        raise ValueError("ASL subject entity is required when metrics are selected")
    if subject_entity:
        known_subjects = _known_subject_codes(knowledge, selected_metrics)
        if str(subject_entity) not in known_subjects:
            raise ValueError(
                f"ASL subject was not retrieved or resolved from semantic scope: {subject_entity}"
            )

    known_dimensions = _known_codes(knowledge, "dimensions", "dim_code")
    known_fields = _known_physical_fields(knowledge)
    known_dimension_attrs = _known_dimension_attrs(knowledge)
    for metric in ast["metrics"]:
        override_anchor = metric.get("time_anchor")
        if override_anchor is not None and (
            not _PHYSICAL_FIELD.fullmatch(str(override_anchor))
            or str(override_anchor) not in known_fields
        ):
            raise ValueError("ASL metric time_anchor was not retrieved from semantic scope")
    selected_dimensions: set[str] = set()
    for item in ast["dimensions"]:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("ASL dimension name is required")
        name = str(item["name"])
        if name in selected_dimensions:
            raise ValueError(f"ASL dimension is duplicated: {name}")
        selected_dimensions.add(name)
        if "." in name and not _PHYSICAL_FIELD.fullmatch(name):
            raise ValueError("ASL physical dimension must use safe table.column")
        if "." in name and name not in known_fields:
            raise ValueError("ASL physical dimension was not retrieved from semantic scope")
        if "." not in name and name not in known_dimensions:
            raise ValueError(f"ASL dimension was not retrieved from semantic scope: {name}")
        if "." in name and item.get("attr") is not None:
            raise ValueError("ASL physical dimension attr must be null")
        if "." not in name and item.get("attr") is not None:
            if str(item["attr"]) not in known_dimension_attrs.get(name, set()):
                raise ValueError("ASL dimension attr was not retrieved from semantic scope")
        granularity = item.get("granularity")
        if granularity is not None and str(granularity) not in {
            "hour", "day", "week", "month", "quarter", "year",
        }:
            raise ValueError("ASL dimension granularity is invalid")

    allowed_operators = {
        "=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE", "BETWEEN"
    }
    _, attributes_by_entity = _scoped_entity_attributes(knowledge)
    for item in ast["filters"]:
        if not isinstance(item, dict):
            raise ValueError("ASL filter must be an object")
        if not _PHYSICAL_FIELD.fullmatch(str(item.get("field") or "")):
            raise ValueError("ASL filter field must use table.column")
        if str(item["field"]) not in known_fields:
            raise ValueError(
                "ASL filter field was not retrieved from semantic scope: "
                f"{item['field']}"
            )
        operator = str(item.get("operator") or "").upper()
        if operator not in allowed_operators:
            raise ValueError("ASL filter operator is not allowed")
        value = item.get("value")
        if operator == "BETWEEN" and (not isinstance(value, list) or len(value) != 2):
            raise ValueError("ASL BETWEEN filter value must contain exactly two items")
        if operator in {"IN", "NOT IN"} and not isinstance(value, list):
            raise ValueError("ASL IN filter value must be a list")
        field_attribute = next((
            attributes[str(item["field"])]
            for attributes in attributes_by_entity.values()
            if str(item["field"]) in attributes
        ), None)
        if field_attribute and _is_last_activity_date_attribute(field_attribute):
            date_values = value if isinstance(value, list) else [value]
            if operator not in {"=", ">", ">=", "<", "<=", "BETWEEN"}:
                raise ValueError("ASL last-activity filter must use a date comparison")
            try:
                parsed_dates = [date.fromisoformat(str(item)) for item in date_values]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "ASL last-activity filter values must be ISO dates"
                ) from exc
            if operator == "BETWEEN" and parsed_dates[0] > parsed_dates[1]:
                raise ValueError("ASL last-activity date range is reversed")
        natural_key_values = _natural_name_filter_values(
            operator,
            value,
            user_query,
        )
        if (
            natural_key_values
            and _is_relationship_key_field(str(item["field"]), attributes_by_entity)
            and not any(
                _ambiguity_mentions_filter(
                    ambiguity,
                    str(item["field"]),
                    value,
                    require_value=True,
                )
                for ambiguity in ast["ambiguity"]
            )
        ):
            raise ValueError(
                "ASL natural-language entity name cannot be used as a code/id/join key"
            )

    time_context = ast.get("time_context")
    if time_context is not None:
        if not isinstance(time_context, dict):
            raise ValueError("ASL time_context must be an object or null")
        if set(time_context) - {"type", "start", "end", "value", "unit", "anchor"}:
            raise ValueError("ASL time_context contains unsupported fields")
        time_type = str(time_context.get("type") or "")
        if time_type not in {
            "this_month", "last_month", "this_year", "year", "custom",
            "range", "today", "yesterday",
        }:
            raise ValueError("ASL time_context type is invalid")
        if str(time_context.get("unit") or "") not in {
            "hour", "day", "week", "month", "quarter", "year",
        }:
            raise ValueError("ASL time_context unit is invalid")
        if not _PHYSICAL_FIELD.fullmatch(str(time_context.get("anchor") or "")):
            raise ValueError("ASL time_context anchor must use table.column")
        if str(time_context["anchor"]) not in known_fields:
            raise ValueError("ASL time_context anchor was not retrieved from semantic scope")
        if time_type in {"custom", "range"} and (
            not time_context.get("start") or not time_context.get("end")
        ):
            raise ValueError("ASL custom/range time_context requires start and end")
        if time_type in {"custom", "range"}:
            try:
                start_date = date.fromisoformat(str(time_context["start"]))
                end_date = date.fromisoformat(str(time_context["end"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("ASL time_context dates must use YYYY-MM-DD") from exc
            if start_date > end_date:
                raise ValueError("ASL time_context date range is reversed")
        if time_type == "year" and time_context.get("value") is None:
            raise ValueError("ASL year time_context requires value")
        if time_type == "year":
            try:
                year_value = int(time_context["value"])
            except (TypeError, ValueError) as exc:
                raise ValueError("ASL year time_context value must be a year") from exc
            if not 1900 <= year_value <= 2100:
                raise ValueError("ASL year time_context value is outside supported range")

    sort = ast.get("sort")
    if sort is not None:
        if not isinstance(sort, dict):
            raise ValueError("ASL sort must be an object or null")
        direction = str(sort.get("direction") or "").upper()
        field_type = str(sort.get("field_type") or "")
        field = str(sort.get("field") or "")
        if direction not in {"ASC", "DESC"}:
            raise ValueError("ASL sort direction is invalid")
        if field_type not in {"metric", "dimension", "field"}:
            raise ValueError("ASL sort field_type is invalid")
        if field_type == "metric" and field not in selected_metrics:
            raise ValueError("ASL metric sort field must be a selected metric")
        if field_type == "dimension" and (
            field not in {str(item["name"]) for item in ast["dimensions"]}
        ):
            raise ValueError("ASL dimension sort field must be a selected dimension")
        if field_type == "field" and not _PHYSICAL_FIELD.fullmatch(field):
            raise ValueError("ASL field sort must use table.column")
        if field_type == "field" and field not in known_fields:
            raise ValueError("ASL field sort was not retrieved from semantic scope")

    for expression in ast["having"]:
        if not isinstance(expression, str) or not _SAFE_HAVING.fullmatch(expression.strip()):
            raise ValueError("ASL having expression is outside the safe aggregate subset")

    limit = ast.get("limit")
    if limit is not None and (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or limit > 10000
    ):
        raise ValueError("ASL limit must be an integer between 1 and 10000")
    if not all(isinstance(item, dict) for item in ast["ambiguity"]):
        raise ValueError("ASL ambiguity items must be objects")
    if not ast["metrics"] and not ast["dimensions"] and not ast["ambiguity"]:
        raise ValueError(
            "ASL must contain a metric, a detail projection, or a clarification ambiguity"
        )
    return json.dumps(ast, ensure_ascii=False)


def _contract_label(value: object) -> str:
    normalized = re.sub(
        r"(?:名称|名字|清单|名单)$", "",
        re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).casefold(),
    )
    # Natural-language contracts commonly use “业务城市/业务省份”, while the
    # governed attribute catalog uses the equivalent concise labels “业务市/业务省”.
    # Canonicalize only qualified suffixes so the standalone generic labels
    # “城市/省份” remain available for normal ambiguity handling.
    normalized = re.sub(r"(?<=.)城市$", "市", normalized)
    normalized = re.sub(r"(?<=.)省份$", "省", normalized)
    return normalized


def _contract_projection_candidates(
    label: str,
    query_object: str | None,
    knowledge: dict,
    preferred_fields: set[str] | None = None,
    *,
    prefer_physical: bool = False,
) -> list[str]:
    """Resolve a requested display label only from recalled semantic metadata."""

    target = _contract_label(label)
    object_label = _contract_label(query_object)
    authorized = _known_physical_fields(knowledge)
    entities, attributes_by_entity = _scoped_entity_attributes(knowledge)

    exact_object_entities: set[str] = set()
    related_object_entities: set[str] = set()
    if object_label:
        for entity_code, metadata in entities.items():
            labels = {_contract_label(entity_code)}
            for key in ("entity_name", "entity_alias"):
                labels.update(_contract_label(value) for value in _metadata_term_values(metadata.get(key)))
            if object_label in labels:
                exact_object_entities.add(entity_code)
            elif any(
                object_label and (object_label in value or value in object_label)
                for value in labels if value
            ):
                related_object_entities.add(entity_code)

    scored: list[tuple[int, str]] = []
    entity_scope = (
        exact_object_entities or related_object_entities or set(attributes_by_entity)
    )
    for entity_code in entity_scope:
        entity_labels = {_contract_label(entity_code)}
        metadata = entities.get(entity_code, {})
        for key in ("entity_name", "entity_alias"):
            entity_labels.update(_contract_label(value) for value in _metadata_term_values(metadata.get(key)))
        for field, attribute in attributes_by_entity.get(entity_code, {}).items():
            if field not in authorized or _is_relationship_key_field(field, attributes_by_entity):
                continue
            terms = {_contract_label(field.rsplit(".", 1)[-1])}
            for key in ("attr_code", "attr_name", "description", "synonyms"):
                terms.update(_contract_label(value) for value in _metadata_term_values(attribute.get(key)))
            is_display = any(bool(attribute.get(flag)) for flag in (
                "is_main_attribute", "is_primary_name", "is_display_name", "is_name",
            )) or str(attribute.get("semantic_role") or "").casefold() in {
                "name", "display_name", "primary_name", "title",
            }
            exact = target in terms
            entity_display = is_display and target in entity_labels
            partial = any(
                len(target) >= 2 and len(term) >= 2 and (target in term or term in target)
                for term in terms
            )
            if exact or entity_display:
                scored.append((0, field))
            elif partial:
                scored.append((1, field))

    if prefer_physical and scored:
        best_physical_score = min(score for score, _field in scored)
        best_physical = sorted({
            field for score, field in scored
            if score == best_physical_score
        })
        if len(best_physical) == 1:
            return best_physical

    # If the ASL model already selected a currently authorized attribute owned
    # by the exact query object, preserve that metadata-grounded choice.  This
    # matters when a global logical dimension has an overlapping synonym but a
    # different grain (for example a product's department-combination
    # attribute versus the standard-department bridge).  Arbitrary model fields
    # cannot pass: the candidate must occur in the recalled entity metadata and
    # in the caller-requested object scope.
    if preferred_fields and exact_object_entities and scored:
        preferred_scored = [
            (score, field) for score, field in scored
            if field in preferred_fields
        ]
        if preferred_scored:
            best_preferred_score = min(score for score, _field in preferred_scored)
            best_preferred = sorted({
                field for score, field in preferred_scored
                if score == best_preferred_score
            })
            if len(best_preferred) == 1:
                return best_preferred

    dimension_scored: list[tuple[int, str]] = []
    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        code = str(metadata.get("dim_code") or "").strip()
        if not code:
            continue
        terms: set[str] = set()
        for key in ("dim_code", "dim_name", "synonyms", "business_definition", "dim_description"):
            terms.update(_contract_label(value) for value in _metadata_term_values(metadata.get(key)))
        if target in terms:
            dimension_scored.append((0, code))
        elif any(
            len(target) >= 2 and len(term) >= 2 and (target in term or term in target)
            for term in terms
        ):
            dimension_scored.append((1, code))

    # A registered logical dimension is the canonical projection contract. It
    # may be backed by several physical/profile fields, so prefer one uniquely
    # matched logical code before comparing those implementation bindings.
    if dimension_scored:
        best_dimension_score = min(score for score, _candidate in dimension_scored)
        best_dimensions = sorted({
            candidate for score, candidate in dimension_scored
            if score == best_dimension_score
        })
        if len(best_dimensions) == 1:
            return best_dimensions

    if not scored:
        return []
    best = min(score for score, _candidate in scored)
    return sorted({candidate for score, candidate in scored if score == best})


def _contract_entity_candidates(label: str, knowledge: dict) -> list[str]:
    """Resolve a semantic entity label from the current recalled registry."""

    target = _contract_label(label)
    entities, _attributes = _scoped_entity_attributes(knowledge)
    exact: set[str] = set()
    related: set[str] = set()
    for entity_code, metadata in entities.items():
        labels = {_contract_label(entity_code)}
        for key in ("entity_name", "entity_alias"):
            labels.update(
                _contract_label(value)
                for value in _metadata_term_values(metadata.get(key))
            )
        if target in labels:
            exact.add(entity_code)
        elif any(
            target and value and (target in value or value in target)
            for value in labels
        ):
            related.add(entity_code)
    return sorted(exact or related)


def _contract_filter_present(
    ast: dict,
    expected: dict,
    *,
    negative: bool,
    allowed_fields: set[str] | None = None,
) -> bool:
    expected_value = expected.get("value")
    expected_operator = str(expected.get("operator") or "").upper()
    negative_ops = {"NE", "!=", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE"}
    for item in ast.get("filters") or []:
        if not isinstance(item, dict) or item.get("value") != expected_value:
            continue
        if allowed_fields is not None and str(item.get("field") or "") not in allowed_fields:
            continue
        actual_operator = str(item.get("operator") or "").upper()
        if negative:
            if actual_operator in negative_ops:
                return True
        elif actual_operator not in negative_ops and (
            actual_operator == expected_operator
            or {actual_operator, expected_operator}.issubset({"=", "EQ", "EQUAL", "EQUALS"})
        ):
            return True
    return False


def _contract_filter_candidates(
    label: str,
    knowledge: dict,
    *,
    semantic_model_id: int | None = None,
    domain_scope: int | list[int] | tuple[int, ...] | None = None,
    query_object: str | None = None,
) -> list[str]:
    """Resolve a semantic filter label to recalled physical fields only.

    Filters deliberately differ from projections: ASL requires a physical
    ``table.column`` in a predicate, while a projection may use a registered
    logical dimension. Both entity attributes and logical-dimension bindings
    are semantic evidence; no field-name-only fallback is permitted.
    """

    target = _contract_label(label)
    request_bindings = knowledge.get("_contract_filter_bindings", {})
    if isinstance(request_bindings, dict):
        bound = request_bindings.get(target)
        if isinstance(bound, list):
            verified = sorted({
                str(value) for value in bound
                if _PHYSICAL_FIELD.fullmatch(str(value))
            })
            if verified:
                return verified
    authorized = _known_physical_fields(knowledge)
    _entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    scored: list[tuple[int, str]] = []

    def administrative_role(value: str) -> str | None:
        compact = value.removeprefix(_contract_label("业务"))
        if compact in {
            _contract_label("城市"), _contract_label("市"),
        }:
            return "city"
        if compact in {_contract_label("省份"), _contract_label("省")}:
            return "province"
        return None

    def match_score(
        identity_terms: set[str], descriptive_terms: set[str]
    ) -> int | None:
        """Prefer governed names/codes over synonyms and fuzzy containment."""
        if target in identity_terms:
            return 0
        if target in descriptive_terms:
            return 1
        target_role = administrative_role(target)
        if target_role and any(
            administrative_role(term) == target_role
            for term in identity_terms | descriptive_terms
        ):
            return 1
        if any(
            len(target) >= 2
            and len(term) >= 2
            and (target in term or term in target)
            for term in identity_terms
        ):
            return 2
        if any(
            len(target) >= 2
            and len(term) >= 2
            and (target in term or term in target)
            for term in descriptive_terms
        ):
            return 3
        return None

    for entity_attributes in attributes_by_entity.values():
        for field, attribute in entity_attributes.items():
            if field not in authorized or _is_relationship_key_field(
                field, attributes_by_entity
            ):
                continue
            identity_terms = {_contract_label(field.rsplit(".", 1)[-1])}
            for key in ("attr_code", "attr_name"):
                identity_terms.update(
                    _contract_label(value)
                    for value in _metadata_term_values(attribute.get(key))
                )
            descriptive_terms: set[str] = set()
            for key in ("description", "synonyms", "business_definition"):
                descriptive_terms.update(
                    _contract_label(value)
                    for value in _metadata_term_values(attribute.get(key))
                )
            score = match_score(identity_terms, descriptive_terms)
            if score is not None:
                scored.append((score, field))

    for item in knowledge.get("dimensions", []):
        metadata = getattr(item, "metadata", {}) or {}
        identity_terms: set[str] = set()
        for key in ("dim_code", "dim_name"):
            identity_terms.update(
                _contract_label(value)
                for value in _metadata_term_values(metadata.get(key))
            )
        descriptive_terms: set[str] = set()
        for key in ("synonyms", "business_definition", "dim_description"):
            descriptive_terms.update(
                _contract_label(value)
                for value in _metadata_term_values(metadata.get(key))
            )
        score = match_score(identity_terms, descriptive_terms)
        if score is None:
            continue
        bindings = _json_value(metadata.get("bind_entities")) or []
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict):
                continue
            table = str(binding.get("mappingTable") or "").strip()
            column = str(binding.get("mappingColumn") or "").strip()
            field = f"{table}.{column}" if table and column else ""
            if field in authorized:
                scored.append((score, field))

    if not scored and semantic_model_id is not None:
        # Bounded vector recall can omit the very name attribute required by a
        # caller-owned filter.  Fall back to the current published entity
        # registry, then independently prove that the physical field is still
        # active in this semantic-model scope.  This is metadata evidence, not
        # a field-name guess, and it automatically follows republished models.
        registered = get_registered_entity_attributes(
            semantic_model_id, domain_scope
        )
        physical = (get_table_field_by_scope(semantic_model_id=semantic_model_id,
                                            business_domain_id=domain_scope)
                    if domain_scope is not None
                    else get_table_field_by_scope(semantic_model_id=semantic_model_id))
        active_fields = {
            f"{str(table.get('table_name') or '')}.{str(field.get('field_name') or '')}"
            for table in physical.get("tables", [])
            if isinstance(table, dict)
            for field in table.get("fields", [])
            if isinstance(field, dict)
            and table.get("table_name")
            and field.get("field_name")
        }
        for attribute in registered:
            field = str(attribute.get("field_mapping") or "").strip()
            if (
                field not in active_fields
                or attribute.get("is_primary_key")
                or bool(re.search(r"(?:^|[._])(?:id|code)$", field, re.I))
            ):
                continue
            identity_terms = {_contract_label(field.rsplit(".", 1)[-1])}
            for key in ("attr_code", "attr_name"):
                identity_terms.update(
                    _contract_label(value)
                    for value in _metadata_term_values(attribute.get(key))
                )
            descriptive_terms = {
                _contract_label(value)
                for value in _metadata_term_values(attribute.get("description"))
            }
            attribute_score = match_score(identity_terms, descriptive_terms)
            entity_terms: set[str] = set()
            for key in ("entity_code", "entity_name", "entity_alias"):
                entity_terms.update(
                    _contract_label(value)
                    for value in _metadata_term_values(attribute.get(key))
                )
            entity_score = match_score(entity_terms, set())
            if attribute_score is not None:
                # Prefer the exact entity role when two active entities expose
                # the same display label (for example hospital and an
                # all-hospital snapshot).  Attribute identity still dominates.
                scored.append((attribute_score * 10 + (entity_score or 0), field))
            elif attribute.get("is_main_attribute") and entity_score is not None:
                scored.append((40 + entity_score, field))
    if not scored:
        return []

    # Generic administrative roles describe the location of the caller-owned
    # result object. Several active entities can expose a legitimate city or
    # province field, so lexical similarity alone is insufficient. Resolve the
    # query object from current semantic metadata and prefer the candidate
    # reached by the shortest published relation path.
    if target in {
        _contract_label(value)
        for value in (
            "城市", "业务城市",
            "省份", "业务省份", "省", "市",
        )
    } and query_object:
        object_entities = _contract_entity_candidates(query_object, knowledge)
        if len(object_entities) == 1:
            object_entity = object_entities[0]
            object_tables = {
                field.split(".", 1)[0]
                for field in attributes_by_entity.get(object_entity, {})
                if _PHYSICAL_FIELD.fullmatch(field)
            }
            graph = _recalled_relation_table_graph(knowledge)

            def distance_to_object(field: str) -> int | None:
                target_table = field.split(".", 1)[0]
                if target_table in object_tables:
                    return 0
                visited = set(object_tables)
                frontier = set(object_tables)
                for distance in range(1, 6):
                    frontier = {
                        neighbor
                        for table in frontier
                        for neighbor in graph.get(table, set())
                        if neighbor not in visited
                    }
                    if target_table in frontier:
                        return distance
                    if not frontier:
                        return None
                    visited.update(frontier)
                return None

            distances = {
                field: distance
                for _score, field in scored
                if (distance := distance_to_object(field)) is not None
            }
            if distances:
                nearest = min(distances.values())
                scored = [
                    (score, field) for score, field in scored
                    if distances.get(field) == nearest
                ]
    best = min(score for score, _field in scored)
    return sorted({field for score, field in scored if score == best})


def _contract_operator(operator: object, *, negative: bool) -> str:
    normalized = str(operator or "").upper().replace("_", " ")
    if negative:
        return "NOT IN" if normalized in {"NOT IN", "EXCLUDE"} else "!="
    aliases = {"EQ": "=", "EQUAL": "=", "EQUALS": "=", "NE": "!="}
    return aliases.get(normalized, normalized or "=")


def _repair_contract_filter(
    ast: dict,
    expected: dict,
    knowledge: dict,
    *,
    negative: bool,
    semantic_model_id: int | None = None,
    domain_scope: int | list[int] | tuple[int, ...] | None = None,
    query_object: str | None = None,
) -> dict | None:
    """Apply one metadata-backed filter requirement or fail precisely."""

    candidates = _contract_filter_candidates(
        str(expected.get("field") or ""),
        knowledge,
        semantic_model_id=semantic_model_id,
        domain_scope=domain_scope,
        query_object=query_object,
    )
    expected_value = expected.get("value")
    expected_operator = _contract_operator(expected.get("operator"), negative=negative)
    negative_ops = {"NE", "!=", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE"}
    filters = ast.setdefault("filters", [])
    if not isinstance(filters, list):
        ast["filters"] = filters = []

    if len(candidates) != 1 and type(semantic_model_id) is int:
        # Semantic labels such as “经销商名称” may match more than one active
        # entity exposing a generic name attribute.  The caller also owns the
        # literal, so use the current published source values to prove its one
        # executable field.  This follows republished models automatically and
        # never resolves a tie by table naming or vector rank.
        published = load_published_entity_attribute_candidates(
            semantic_model_id, domain_scope
        )
        if candidates:
            candidate_set = set(candidates)
            published = [
                item for item in published
                if str(item.get("field") or "") in candidate_set
            ]
        natural_values = (
            list(expected_value)
            if isinstance(expected_value, list)
            else [expected_value]
        )
        resolved_by_value: list[set[str]] = []
        for raw_value in natural_values:
            if not isinstance(raw_value, (str, int, float)):
                resolved_by_value = []
                break
            resolved_fields: list[str] = []
            for offset in range(0, len(published), 32):
                resolved_fields.extend(
                    resolve_exact_entity_attribute_value_fields(
                        semantic_model_id,
                        domain_scope,
                        published[offset : offset + 32],
                        str(raw_value),
                    ) or []
                )
            resolved_by_value.append(set(map(str, resolved_fields)))
        common = (
            set.intersection(*resolved_by_value)
            if resolved_by_value and all(resolved_by_value)
            else set()
        )
        if len(common) == 1:
            candidates = [next(iter(common))]
        elif len(common) > 1:
            # A geographic or organizational literal can legitimately occur
            # in both the governed conformed dimension and a denormalized
            # descriptive attribute (for example 四川省 in province and
            # all_hospital).  Prefer a unique published main attribute: this
            # is semantic-model governance evidence and automatically follows
            # republishing, rather than a table-name or question-specific
            # heuristic.  If governance still leaves a tie, fail closed.
            published_by_field = {
                str(item.get("field") or ""): item
                for item in published
                if str(item.get("field") or "") in common
            }
            main_fields = {
                field for field, item in published_by_field.items()
                if bool(item.get("is_main_attribute"))
            }
            if len(main_fields) == 1:
                candidates = [next(iter(main_fields))]

    if len(candidates) > 1:
        # A governed role may intentionally be exposed on more than one active
        # entity (for example ``业务城市`` on both the transaction fact and a
        # hospital profile).  Source-value lookup cannot break that tie when
        # the same literal legitimately occurs in both columns.  In that case
        # the already validated ASL subject is the semantic query anchor: bind
        # the filter to its unique published field instead of rejecting a
        # perfectly executable cross-entity query.  If the subject does not
        # prove exactly one candidate, keep failing closed below.
        subject = ast.get("subject")
        subject_entity = (
            str(subject.get("entity") or "").strip()
            if isinstance(subject, dict)
            else ""
        )
        subject_candidates = [
            field for field in candidates
            if subject_entity and field.partition(".")[0] == subject_entity
        ]
        if len(subject_candidates) == 1:
            candidates = subject_candidates

    matching: list[dict] = []
    for item in filters:
        if not isinstance(item, dict) or item.get("value") != expected_value:
            continue
        actual_negative = str(item.get("operator") or "").upper() in negative_ops
        if actual_negative == negative and str(item.get("field") or "") in candidates:
            matching.append(item)
    if len(matching) == 1:
        matching[0]["operator"] = expected_operator
        return None

    if len(candidates) != 1:
        raise ASLValidationError(
            "ASL_FILTER_INVALID",
            "required filter field cannot be uniquely resolved from recalled metadata",
            field="filters",
            details={
                "semantic_field": expected.get("field"),
                "negative": negative,
                "candidates": candidates,
            },
        )

    resolved = candidates[0]
    published_authorized = knowledge.setdefault(
        "_published_authorized_fields", []
    )
    if resolved not in published_authorized:
        published_authorized.append(resolved)
    contract_bindings = knowledge.setdefault("_contract_filter_bindings", {})
    binding_key = _contract_label(str(expected.get("field") or ""))
    bound_fields = contract_bindings.setdefault(binding_key, [])
    if resolved not in bound_fields:
        bound_fields.append(resolved)
    filters[:] = [
        item for item in filters
        if not (
            isinstance(item, dict)
            and item.get("value") == expected_value
            and (
                str(item.get("operator") or "").upper() in negative_ops
            ) == negative
        )
    ]
    filters.append({
        "field": resolved,
        "operator": expected_operator,
        "value": expected_value,
    })
    ambiguities = ast.get("ambiguity")
    if isinstance(ambiguities, list):
        expected_values = (
            list(expected_value)
            if isinstance(expected_value, list)
            else [expected_value]
        )
        value_tokens = {
            str(value or "").strip().strip("%").casefold()
            for value in expected_values
            if str(value or "").strip().strip("%")
        }
        ambiguities[:] = [
            item for item in ambiguities
            if not (
                isinstance(item, dict)
                and item.get("type") == "filter"
                and any(
                    token in json.dumps(
                        item, ensure_ascii=False, default=str
                    ).casefold()
                    for token in value_tokens
                )
            )
        ]
    return {
        "type": "ADD_REQUIRED_NEGATIVE_FILTER" if negative else "ADD_REQUIRED_FILTER",
        "semantic_label": expected.get("field"),
        "resolved_field": resolved,
        "operator": expected_operator,
        "source": "RECALLED_SEMANTIC_METADATA",
    }


def _apply_intent_asl_contract(
    content: str,
    knowledge: dict,
    contract: dict | None,
    semantic_model_id: int | None = None,
    domain_scope: int | list[int] | None = None,
) -> tuple[str, list[dict]]:
    """Perform at most one deterministic metadata-backed contract repair."""

    if not contract:
        return content, []
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content, []
    if not isinstance(ast, dict):
        return content, []

    repairs: list[dict] = []
    intent = str(contract.get("intent") or "")
    relationship_anchor = str(contract.get("relationship_anchor") or "").strip()
    if relationship_anchor:
        anchor_candidates = _contract_entity_candidates(
            relationship_anchor, knowledge,
        )
        if len(anchor_candidates) != 1:
            raise ASLValidationError(
                "ASL_RELATIONSHIP_ANCHOR_INVALID",
                "relationship anchor cannot be uniquely resolved from recalled metadata",
                field="subject",
                details={
                    "relationship_anchor": relationship_anchor,
                    "candidates": anchor_candidates,
                },
            )
        anchor_entity = anchor_candidates[0]
        subject = ast.get("subject")
        current_subject = (
            str(subject.get("entity") or "")
            if isinstance(subject, dict)
            else ""
        )
        if current_subject != anchor_entity:
            ast["subject"] = {"entity": anchor_entity}
            repairs.append({
                "type": "BIND_RELATIONSHIP_ANCHOR",
                "semantic_label": relationship_anchor,
                "resolved_entity": anchor_entity,
                "source": "INTENT_CONTRACT",
            })
    projections = list(dict.fromkeys(map(str, contract.get("required_projections") or [])))
    groupings = list(dict.fromkeys(map(str, contract.get("required_groupings") or [])))
    dimensions = ast.get("dimensions")
    if not isinstance(dimensions, list):
        dimensions = []
        ast["dimensions"] = dimensions

    if intent == "DETAIL_QUERY" and ast.get("metrics"):
        ast["metrics"] = []
        repairs.append({"type": "REMOVE_UNREQUESTED_METRICS", "source": "INTENT_CONTRACT"})

    selected_names = {
        str(item.get("name") or "") for item in dimensions if isinstance(item, dict)
    }
    required_dimension_names: set[str] = set()
    for projection in projections:
        candidates = _contract_projection_candidates(
            projection,
            contract.get("query_object"),
            knowledge,
            preferred_fields=selected_names,
        )
        if len(candidates) != 1:
            raise ASLValidationError(
                "ASL_DETAIL_PROJECTION_MISSING",
                "required detail projection cannot be uniquely resolved from recalled metadata",
                field="dimensions",
                details={
                    "query_object": contract.get("query_object"),
                    "projection": projection,
                    "candidates": candidates,
                },
            )
        selected = candidates[0]
        required_dimension_names.add(selected)
        if selected not in selected_names:
            dimensions.append({
                "name": selected,
                "attr": None,
                "level": None,
                "granularity": None,
            })
            selected_names.add(selected)
            repairs.append({
                "type": "ADD_REQUIRED_PROJECTION",
                "semantic_label": projection,
                "resolved_field": selected,
                "source": "RECALLED_SEMANTIC_METADATA",
            })

    if intent == "DETAIL_QUERY" and required_dimension_names:
        removed = [
            str(item.get("name") or "")
            for item in dimensions
            if isinstance(item, dict)
            and str(item.get("name") or "") not in required_dimension_names
        ]
        ast["dimensions"] = dimensions = [
            item for item in dimensions
            if isinstance(item, dict)
            and str(item.get("name") or "") in required_dimension_names
        ]
        if removed:
            repairs.append({
                "type": "REMOVE_UNREQUESTED_PROJECTIONS",
                "fields": removed,
                "source": "INTENT_CONTRACT",
            })
        # A caller-owned DETAIL contract is explicitly metricless. Once every
        # required projection has been resolved from published attributes, an
        # LLM warning about an empty metric catalog is no longer a material
        # ambiguity and must not block SQL translation.
        ambiguities = ast.get("ambiguity")
        if isinstance(ambiguities, list):
            retained = [
                item for item in ambiguities
                if not (
                    isinstance(item, dict)
                    and str(item.get("type") or "") == "metric"
                )
            ]
            if len(retained) != len(ambiguities):
                ast["ambiguity"] = retained
                repairs.append({
                    "type": "REMOVE_IRRELEVANT_METRIC_AMBIGUITY",
                    "source": "INTENT_CONTRACT",
                })

    if intent != "DETAIL_QUERY":
        required_grouping_names: set[str] = set()
        for grouping in groupings:
            candidates = _contract_projection_candidates(
                grouping,
                contract.get("query_object"),
                knowledge,
                preferred_fields=selected_names,
                prefer_physical=bool(relationship_anchor),
            )
            if len(candidates) != 1:
                raise ASLValidationError(
                    "ASL_GROUPING_DIMENSION_MISSING",
                    "required grouping cannot be uniquely resolved from recalled metadata",
                    field="dimensions",
                    details={
                        "query_object": contract.get("query_object"),
                        "grouping": grouping,
                        "candidates": candidates,
                    },
                )
            selected = candidates[0]
            required_grouping_names.add(selected)
            if selected not in selected_names:
                dimensions.append({
                    "name": selected,
                    "attr": None,
                    "level": None,
                    "granularity": None,
                })
                selected_names.add(selected)
                repairs.append({
                    "type": "ADD_REQUIRED_GROUPING",
                    "semantic_label": grouping,
                    "resolved_field": selected,
                    "source": "RECALLED_SEMANTIC_METADATA",
                })
        removed_groupings = [
            str(item.get("name") or "")
            for item in dimensions
            if isinstance(item, dict)
            and not item.get("granularity")
            and str(item.get("name") or "") not in required_grouping_names
        ]
        if removed_groupings:
            ast["dimensions"] = dimensions = [
                item for item in dimensions
                if isinstance(item, dict)
                and (
                    item.get("granularity")
                    or str(item.get("name") or "") in required_grouping_names
                )
            ]
            repairs.append({
                "type": "REMOVE_UNCONTRACTED_GROUPINGS",
                "fields": removed_groupings,
                "source": "INTENT_CONTRACT",
            })

    for expected in contract.get("filters") or []:
        repair = _repair_contract_filter(
            ast, expected, knowledge, negative=False,
            semantic_model_id=semantic_model_id,
            domain_scope=domain_scope,
            query_object=contract.get("query_object"),
        )
        if repair is not None:
            repairs.append(repair)
    for expected in contract.get("negative_filters") or []:
        repair = _repair_contract_filter(
            ast, expected, knowledge, negative=True,
            semantic_model_id=semantic_model_id,
            domain_scope=domain_scope,
            query_object=contract.get("query_object"),
        )
        if repair is not None:
            repairs.append(repair)

    # The caller-owned contract is also an upper bound for identity filters.
    # A generative ASL draft can occasionally attach an unrelated company,
    # hospital or product name that merely appeared in recalled examples.  A
    # required-filter check only proves that the requested predicate survived;
    # it does not detect this result-emptying extra predicate.  Remove only
    # uncontracted *main identity* filters here. Numeric/status predicates from
    # governed metric definitions remain untouched, and untyped caller mentions
    # are re-added below only after current-source value resolution.
    typed_filter_values: set[str] = set()
    for expected in [
        *(contract.get("filters") or []),
        *(contract.get("negative_filters") or []),
    ]:
        raw_value = expected.get("value") if isinstance(expected, dict) else None
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        typed_filter_values.update(
            re.sub(r"\s+", "", str(value or "")).casefold()
            for value in values
            if isinstance(value, str) and value.strip()
        )
    typed_filter_values.update(
        re.sub(r"\s+", "", str(value or "")).casefold()
        for value in contract.get("semantic_entity_mentions") or []
        if isinstance(value, str) and value.strip()
    )
    _entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
    # Every governed identity-like text attribute is caller-owned, not only an
    # entity's primary display name.  Limiting this upper bound to
    # ``is_main_attribute`` allowed stale brand/category/manufacturer filters
    # from a recalled draft to survive when the caller replaced the commercial
    # subject with an exact product.  Operational, numeric and status fields
    # remain excluded by ``_is_catalog_identity_attribute`` and can still be
    # introduced by governed metric/business-rule definitions.
    main_identity_fields = {
        str(field)
        for entity_attributes in attributes_by_entity.values()
        for field, attribute in entity_attributes.items()
        if _is_catalog_identity_attribute(str(field), attribute)
    }
    if type(semantic_model_id) is int:
        main_identity_fields.update(
            str(item.get("field") or "")
            for item in load_published_entity_attribute_candidates(
                semantic_model_id, domain_scope,
            )
            if _is_catalog_identity_attribute(
                str(item.get("field") or ""), item
            )
        )
    removed_identity_filters: list[dict] = []
    retained_filters: list[dict] = []
    for item in ast.get("filters") or []:
        if not isinstance(item, dict):
            retained_filters.append(item)
            continue
        value = item.get("value")
        value_key = (
            re.sub(r"\s+", "", value).casefold()
            if isinstance(value, str)
            else ""
        )
        if (
            str(item.get("field") or "") in main_identity_fields
            and value_key
            and value_key not in typed_filter_values
        ):
            removed_identity_filters.append(item)
            continue
        retained_filters.append(item)
    if removed_identity_filters:
        ast["filters"] = retained_filters
        repairs.append({
            "type": "REMOVE_UNCONTRACTED_IDENTITY_FILTERS",
            "fields": sorted({
                str(item.get("field") or "")
                for item in removed_identity_filters
            }),
            "source": "INTENT_CONTRACT",
        })

    # Unlabelled noun phrases remain caller-owned literals, but their semantic
    # role is resolved only here against current registered attributes and the
    # source-value catalog. This covers questions such as “人工心肺系统的销售量”
    # without hard-coding the phrase as 商品名称, brand or category.
    if semantic_model_id is not None:
        entities, attributes_by_entity = _scoped_entity_attributes(knowledge)
        authorized_fields = _known_physical_fields(knowledge)
        candidates = _catalog_identity_candidates(
            _all_non_key_attribute_candidates(
                set(entities), entities, attributes_by_entity,
                authorized_fields, "",
            ),
            attributes_by_entity,
        )
        published_candidates: list[dict] | None = None
        for mention in contract.get("semantic_entity_mentions") or []:
            literal = str(mention).strip()
            if not literal:
                continue
            literal_key = re.sub(r"\s+", "", literal).casefold()
            if any(
                isinstance(item, dict)
                and re.sub(
                    r"\s+", "", str(item.get("value") or "").strip()
                ).casefold() == literal_key
                for item in ast.get("filters") or []
            ):
                continue
            def catalog_resolution(
                candidate_fields: list[dict],
            ) -> tuple[list[str], list[dict[str, Any]]]:
                # The catalog APIs intentionally cap a single request at 32
                # candidate fields. Resolve every bounded chunk and merge.
                exact: list[str] = []
                matches: list[dict[str, Any]] = []
                for offset in range(0, len(candidate_fields), 32):
                    chunk = candidate_fields[offset : offset + 32]
                    exact.extend(resolve_exact_entity_attribute_value_fields(
                        semantic_model_id, domain_scope, chunk, literal,
                    ))
                exact = list(dict.fromkeys(exact))
                for offset in range(0, len(candidate_fields), 32):
                    chunk = candidate_fields[offset : offset + 32]
                    matches.extend(resolve_entity_attribute_catalog_matches(
                        semantic_model_id, domain_scope, chunk, literal,
                    ))
                deduped_matches: dict[tuple[str, str, str], dict[str, Any]] = {}
                for item in matches:
                    key = (
                        str(item.get("field") or ""),
                        str(item.get("canonical_value") or ""),
                        str(item.get("match_type") or ""),
                    )
                    deduped_matches.setdefault(key, item)
                return exact, list(deduped_matches.values())

            def selected_resolution(
                exact_fields: list[str],
                catalog_items: list[dict[str, Any]],
            ) -> tuple[str, str] | None:
                if len(exact_fields) == 1:
                    canonical_exact = {
                        str(item.get("canonical_value") or "")
                        for item in catalog_items
                        if str(item.get("field") or "") == exact_fields[0]
                        and item.get("match_type") == "EXACT"
                        and str(item.get("canonical_value") or "")
                    }
                    if len(canonical_exact) == 1:
                        return exact_fields[0], next(iter(canonical_exact))
                    # Exact source lookup has already authorized and proven the
                    # field. This compatibility fallback applies only when an
                    # older catalog provider cannot return its canonical text.
                    return exact_fields[0], literal
                exact_catalog = [
                    item for item in catalog_items
                    if item.get("match_type") == "EXACT"
                ]
                unique_exact = {
                    (
                        str(item.get("field") or ""),
                        str(item.get("canonical_value") or ""),
                    )
                    for item in exact_catalog
                }
                if len(unique_exact) == 1:
                    return next(iter(unique_exact))
                fuzzy_matches = [
                    item for item in catalog_items
                    if item.get("match_type") == "ORDERED_SUBSEQUENCE"
                    and SequenceMatcher(
                        None,
                        str(mention).casefold(),
                        str(item.get("canonical_value") or "").casefold(),
                    ).ratio() >= 0.86
                ]
                fuzzy_matches.sort(
                    key=lambda item: len(str(item.get("canonical_value") or ""))
                )
                if not fuzzy_matches or (
                    len(fuzzy_matches) > 1
                    and len(str(fuzzy_matches[0].get("canonical_value") or ""))
                    == len(str(fuzzy_matches[1].get("canonical_value") or ""))
                ):
                    return None
                return (
                    str(fuzzy_matches[0].get("field") or ""),
                    str(fuzzy_matches[0].get("canonical_value") or ""),
                )

            matched_fields, catalog_matches = catalog_resolution(candidates)
            resolved = selected_resolution(matched_fields, catalog_matches)
            if resolved is None:
                # Top-k semantic recall is a relevance optimization, not an
                # authority boundary.  Retry against every currently published
                # attribute in the same model/domain before declaring a real
                # entity ambiguity or absence.  This makes newly published
                # names usable immediately without hard-coded dictionaries.
                if published_candidates is None:
                    published_candidates = (
                        load_published_entity_attribute_candidates(
                            semantic_model_id, domain_scope
                        )
                    )
                known = {
                    (str(item.get("entity_code") or ""), str(item.get("field") or ""))
                    for item in candidates
                }
                expanded = [
                    *candidates,
                    *(
                        item for item in published_candidates
                        if (
                            str(item.get("entity_code") or ""),
                            str(item.get("field") or ""),
                        ) not in known
                    ),
                ]
                if len(expanded) > len(candidates):
                    matched_fields, catalog_matches = catalog_resolution(expanded)
                    resolved = selected_resolution(matched_fields, catalog_matches)

            if resolved is None:
                raise ASLValidationError(
                    "ASL_ENTITY_MENTION_UNRESOLVED",
                    "semantic entity mention cannot be uniquely resolved "
                    "from the current published source catalog",
                    field="filters",
                    details={
                        "mention": literal,
                        "candidate_field_count": len(published_candidates or candidates),
                    },
                )
            field, canonical_value = resolved
            if not field or not canonical_value:
                raise ASLValidationError(
                    "ASL_ENTITY_MENTION_UNRESOLVED",
                    "semantic entity mention resolved to an incomplete source value",
                    field="filters",
                    details={"mention": literal},
                )
            published_authorized = knowledge.setdefault(
                "_published_authorized_fields", []
            )
            if field not in published_authorized:
                published_authorized.append(field)
            if any(
                isinstance(item, dict)
                and str(item.get("field") or "") == field
                and str(item.get("value") or "") == canonical_value
                for item in ast.get("filters") or []
            ):
                continue
            ast.setdefault("filters", []).append({
                "field": field,
                "operator": "=",
                "value": canonical_value,
            })
            ambiguities = ast.get("ambiguity")
            if isinstance(ambiguities, list):
                literal_folded = literal.casefold()
                field_prefix = f"{field}=".casefold()

                def resolved_mention_ambiguity(item: object) -> bool:
                    if not isinstance(item, dict):
                        return False
                    if str(item.get("type") or "") not in {
                        "filter", "entity_value", "entity_role", "filter_slot",
                    }:
                        return False
                    phrase_and_question = " ".join(
                        str(item.get(key) or "")
                        for key in ("phrase", "question", "message")
                    ).casefold()
                    raw_candidates = item.get("candidates")
                    candidate_values = (
                        [str(value).casefold() for value in raw_candidates]
                        if isinstance(raw_candidates, list)
                        else []
                    )
                    return (
                        literal_folded in phrase_and_question
                        or any(value.startswith(field_prefix) for value in candidate_values)
                    )

                ambiguities[:] = [
                    item for item in ambiguities
                    if not resolved_mention_ambiguity(item)
                ]
            repairs.append({
                "type": "ADD_SOURCE_RESOLVED_ENTITY_FILTER",
                "mention": literal,
                "canonical_value": canonical_value,
                "resolved_field": field,
                "source": "REGISTERED_SOURCE_VALUE",
            })

    time_policy = str(contract.get("time_policy") or "OPTIONAL").upper()
    if time_policy == "REQUIRED":
        canonical_range = contract.get("canonical_time_range") or {}
        time_context = ast.get("time_context")
        anchor = (
            str(time_context.get("anchor"))
            if isinstance(time_context, dict) and time_context.get("anchor")
            else _unique_query_time_anchor(ast, knowledge)
        )
        anchor_source = "RECALLED_SEMANTIC_METADATA"
        if not anchor:
            anchor = _registered_subject_time_anchor(
                ast, knowledge, semantic_model_id, domain_scope=domain_scope
            )
            if anchor:
                anchor_source = "MYSQL_SEMANTIC_FIELD_REGISTRY"
        if anchor and canonical_range.get("start") and canonical_range.get("end"):
            ast["time_context"] = {
                "type": "custom",
                "start": str(canonical_range["start"]),
                "end": str(canonical_range["end"]),
                "value": None,
                "unit": (
                    str(time_context.get("unit") or "day")
                    if isinstance(time_context, dict) else "day"
                ),
                "anchor": anchor,
            }
            if (
                intent == "DETAIL_QUERY"
                and not contract.get("metric_required")
                and str(contract.get("projection_mode") or "ROWS").upper()
                != "DISTINCT"
            ):
                # A bounded row-detail query may expose its row timestamp for
                # deterministic chronology.  A DISTINCT entity list instead
                # uses time only as an eligibility predicate: projecting the
                # timestamp changes the result grain from one entity to one
                # entity/date and manufactures duplicates.
                projected = {
                    str(item.get("name") or "")
                    for item in ast.get("dimensions") or []
                    if isinstance(item, dict)
                }
                if anchor not in projected:
                    _, scoped_attributes = _scoped_entity_attributes(knowledge)
                    anchor_metadata = next(
                        (
                            attributes.get(anchor)
                            for attributes in scoped_attributes.values()
                            if anchor in attributes
                        ),
                        {},
                    ) or {}
                    ast.setdefault("dimensions", []).append({
                        "name": anchor,
                        "alias": str(
                            anchor_metadata.get("attr_name")
                            or anchor.rsplit(".", 1)[-1]
                        ),
                        "attr": None,
                        "level": None,
                        "granularity": None,
                    })
                    repairs.append({
                        "type": "ADD_TIME_CONTEXT_PROJECTION",
                        "resolved_field": anchor,
                        "source": anchor_source,
                    })
                if ast.get("sort") is None:
                    ast["sort"] = {
                        "field": anchor,
                        "field_type": "field",
                        "direction": "ASC",
                    }
                    repairs.append({
                        "type": "ADD_TIME_CONTEXT_SORT",
                        "resolved_field": anchor,
                        "source": anchor_source,
                    })
            ambiguities = ast.get("ambiguity")
            if isinstance(ambiguities, list):
                ast["ambiguity"] = [
                    item for item in ambiguities
                    if not (
                        isinstance(item, dict)
                        and str(item.get("type") or "") in {"time", "time_anchor"}
                    )
                ]
            repairs.append({
                "type": "BIND_CANONICAL_TIME_RANGE",
                "resolved_anchor": anchor,
                "source": anchor_source,
            })
    if time_policy == "FORBIDDEN":
        had_time_context = ast.get("time_context") is not None
        ast["time_context"] = None
        ambiguities = ast.get("ambiguity")
        removed_time_ambiguity = False
        if isinstance(ambiguities, list):
            retained = [
                item for item in ambiguities
                if not (
                    isinstance(item, dict)
                    and str(item.get("type") or "") in {"time", "time_anchor"}
                )
            ]
            removed_time_ambiguity = len(retained) != len(ambiguities)
            ast["ambiguity"] = retained
        if had_time_context or removed_time_ambiguity:
            repairs.append({
                "type": "REMOVE_DEFAULT_TIME_SCOPE",
                "source": "INTENT_CONTRACT",
            })

    sorting = contract.get("sorting")
    if isinstance(sorting, dict) and sorting.get("required") is True:
        sort = ast.get("sort")
        direction = str(sorting.get("direction") or "DESC").upper()
        if not isinstance(sort, dict):
            metric_codes = list(contract.get("required_metric_codes") or [])
            if len(metric_codes) == 1:
                ast["sort"] = {
                    "field": metric_codes[0], "field_type": "metric", "direction": direction,
                }
                repairs.append({"type": "ADD_REQUIRED_SORT", "source": "INTENT_CONTRACT"})
        if sorting.get("limit") is not None and ast.get("limit") != sorting.get("limit"):
            ast["limit"] = sorting["limit"]
            repairs.append({"type": "APPLY_REQUIRED_LIMIT", "source": "INTENT_CONTRACT"})
    return json.dumps(ast, ensure_ascii=False), repairs


def _validate_intent_asl_contract(
    content: str,
    contract: dict | None,
    knowledge: dict | None = None,
) -> None:
    if not contract:
        return
    ast = json.loads(content)
    intent = str(contract.get("intent") or "")
    metrics = [item for item in ast.get("metrics") or [] if isinstance(item, dict)]
    dimensions = [item for item in ast.get("dimensions") or [] if isinstance(item, dict)]
    if contract.get("metric_required") and not metrics:
        raise ASLValidationError(
            "ASL_METRIC_SELECTION_INVALID",
            "intent contract requires at least one governed metric",
            field="metrics",
        )
    if intent == "DETAIL_QUERY" and not dimensions:
        raise ASLValidationError(
            "ASL_DETAIL_PROJECTION_MISSING",
            "detail intent contract requires at least one registered projection",
            field="dimensions",
            details={"query_object": contract.get("query_object")},
        )
    relationship_anchor = str(contract.get("relationship_anchor") or "").strip()
    if relationship_anchor and knowledge is not None:
        candidates = _contract_entity_candidates(relationship_anchor, knowledge)
        subject = ast.get("subject")
        selected = (
            str(subject.get("entity") or "")
            if isinstance(subject, dict)
            else ""
        )
        if len(candidates) != 1 or selected != candidates[0]:
            raise ASLValidationError(
                "ASL_RELATIONSHIP_ANCHOR_INVALID",
                "ASL did not preserve the governed relationship anchor",
                field="subject",
                details={
                    "relationship_anchor": relationship_anchor,
                    "candidates": candidates,
                    "selected": selected,
                },
            )
    if knowledge is not None:
        selected_dimension_names = {
            str(item.get("name") or "") for item in dimensions
        }
        for projection in contract.get("required_projections") or []:
            candidates = _contract_projection_candidates(
                str(projection),
                contract.get("query_object"),
                knowledge,
                preferred_fields=selected_dimension_names,
            )
            if len(candidates) != 1 or candidates[0] not in selected_dimension_names:
                raise ASLValidationError(
                    "ASL_DETAIL_PROJECTION_MISSING",
                    "ASL omitted a metadata-resolved required detail projection",
                    field="dimensions",
                    details={
                        "projection": projection,
                        "candidates": candidates,
                        "selected_dimensions": sorted(selected_dimension_names),
                    },
                )
        selected_dimension_names = {
            str(item.get("name") or "") for item in dimensions
        }
        for grouping in contract.get("required_groupings") or []:
            candidates = _contract_projection_candidates(
                str(grouping),
                contract.get("query_object"),
                knowledge,
                preferred_fields=selected_dimension_names,
            )
            if len(candidates) != 1 or candidates[0] not in selected_dimension_names:
                raise ASLValidationError(
                    "ASL_GROUPING_DIMENSION_MISSING",
                    "ASL omitted a metadata-resolved required grouping",
                    field="dimensions",
                    details={
                        "grouping": grouping,
                        "candidates": candidates,
                        "selected_dimensions": sorted(selected_dimension_names),
                    },
                )
    for expected in contract.get("filters") or []:
        candidates = _contract_filter_candidates(
            str(expected.get("field") or ""), knowledge or {},
            query_object=contract.get("query_object"),
        )
        if (
            knowledge is not None and not candidates
        ) or not _contract_filter_present(
            ast,
            expected,
            negative=False,
            allowed_fields=set(candidates) if knowledge is not None else None,
        ):
            raise ASLValidationError(
                "ASL_FILTER_INVALID", "ASL omitted a required positive filter",
                field="filters", details={"expected_filter": expected},
            )
    for expected in contract.get("negative_filters") or []:
        candidates = _contract_filter_candidates(
            str(expected.get("field") or ""), knowledge or {},
            query_object=contract.get("query_object"),
        )
        if (
            knowledge is not None and not candidates
        ) or not _contract_filter_present(
            ast,
            expected,
            negative=True,
            allowed_fields=set(candidates) if knowledge is not None else None,
        ):
            raise ASLValidationError(
                "ASL_FILTER_INVALID", "ASL omitted a required negative filter",
                field="filters", details={"expected_filter": expected},
            )
    for forbidden in contract.get("forbidden_filters") or []:
        candidates = _contract_filter_candidates(
            str(forbidden.get("field") or ""), knowledge or {},
            query_object=contract.get("query_object"),
        )
        raw_value = forbidden.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        forbidden_values = {
            str(value).strip() for value in values if value not in (None, "")
        }
        present = any(
            isinstance(item, dict)
            and bool({
                str(value).strip()
                for value in (
                    item.get("value")
                    if isinstance(item.get("value"), list)
                    else [item.get("value")]
                )
                if value not in (None, "")
            } & forbidden_values)
            and (
                knowledge is None
                or not candidates
                or str(item.get("field") or "") in set(candidates)
            )
            for item in ast.get("filters") or []
        )
        if present:
            raise ASLValidationError(
                "ASL_FORBIDDEN_FILTER_PRESENT",
                "ASL retained a filter explicitly replaced by the current turn",
                field="filters",
                details={"forbidden_filter": forbidden},
            )
    sorting = contract.get("sorting")
    if isinstance(sorting, dict) and sorting.get("required") is True:
        sort = ast.get("sort")
        if not isinstance(sort, dict) or str(sort.get("direction") or "").upper() != str(
            sorting.get("direction") or ""
        ).upper():
            raise ASLValidationError(
                "ASL_SORT_INVALID", "ranking intent contract requires canonical sorting",
                field="sort",
            )
    time_policy = str(contract.get("time_policy") or "OPTIONAL").upper()
    if time_policy == "REQUIRED" and (
        not isinstance(ast.get("time_context"), dict)
        or not ast["time_context"].get("anchor")
    ):
        raise ASLValidationError(
            "ASL_TIME_ANCHOR_MISSING",
            "time-bound intent contract requires a registered time anchor",
            field="time_context",
        )
    if contract.get("time_dimension_required"):
        if not any(item.get("granularity") for item in dimensions):
            raise ASLValidationError(
                "ASL_TIME_DIMENSION_MISSING",
                "trend intent contract requires a time-grain projection",
                field="time_context",
            )
    if time_policy == "FORBIDDEN" and (
        ast.get("time_context") is not None
        or any(
            isinstance(item, dict) and item.get("granularity")
            for item in dimensions
        )
    ):
        raise ASLValidationError(
            "ASL_TIME_SCOPE_INVALID",
            "detail intent contract forbids an implicit default time scope",
            field="time_context",
        )


@lru_cache(maxsize=1)
def _get_chat_model():
    return init_chat_model(
        model=LLM_MODEL,
        model_provider="openai",
        api_key=require_runtime_secret(API_KEY, "OAGNET_API_KEY/DASHSCOPE_API_KEY"),
        base_url=BASE_URL,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
        temperature=0,
        extra_body={"enable_thinking": False},
    )


def _normalize_dynamic_subject(
    content: str,
    semantic_model_id: int,
    business_domain_id: int | list[int] | tuple[int, ...] | None,
    knowledge: dict | None = None,
    resolver=None,
) -> str:
    """Resolve the primary subject from selected metric metadata or time anchor."""
    try:
        ast = json.loads(content)
    except (TypeError, ValueError):
        return content
    resolve = resolver or resolve_entity_code_by_table
    table_candidates: list[str] = []

    def remember_subject(entity_code: str) -> None:
        if not isinstance(knowledge, dict):
            return
        values = knowledge.setdefault("_resolved_subjects", [])
        if entity_code not in values:
            values.append(entity_code)

    selected_metrics = [
        str(item.get("name"))
        for item in ast.get("metrics", [])
        if isinstance(item, dict) and item.get("name")
    ]
    for metric_name in selected_metrics[:1]:
        for result in (knowledge or {}).get("metrics", []):
            metadata = getattr(result, "metadata", {}) or {}
            if str(metadata.get("metric_code")) != metric_name:
                continue
            calculation = _json_value(metadata.get("calculation_rule")) or {}
            global_filters = (
                _json_value(calculation.get("global_filters"))
                if isinstance(calculation, dict)
                else []
            ) or []
            population_tables: set[str] = set()
            for item in global_filters if isinstance(global_filters, list) else []:
                if not isinstance(item, dict):
                    continue
                condition = str(
                    item.get("filterCondition")
                    or item.get("filter_condition")
                    or item.get("condition")
                    or ""
                )
                population_tables.update(re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\b",
                    condition,
                ))
            if len(population_tables) == 1:
                table_name = next(iter(population_tables))
                try:
                    population_entity = resolve(
                        semantic_model_id, business_domain_id, table_name
                    )
                except Exception as exc:
                    logger.warning(
                        "metric population entity resolution failed for table=%s: %s",
                        table_name,
                        exc,
                    )
                    population_entity = None
                if population_entity:
                    subject = ast.setdefault("subject", {})
                    if isinstance(subject, dict):
                        subject["entity"] = population_entity
                        remember_subject(population_entity)
                    return json.dumps(ast, ensure_ascii=False)
            dependency = _json_value(metadata.get("source_dependency")) or {}
            bound_entities = (
                _json_value(dependency.get("bind_entity"))
                if isinstance(dependency, dict)
                else []
            ) or []
            if bound_entities:
                subject = ast.setdefault("subject", {})
                if isinstance(subject, dict):
                    entity_code = str(bound_entities[0])
                    subject["entity"] = entity_code
                    remember_subject(entity_code)
                return json.dumps(ast, ensure_ascii=False)
            formula = (
                calculation.get("calc_formula")
                if isinstance(calculation, dict)
                else None
            )
            if formula:
                table_candidates.extend(re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\b",
                    str(formula),
                ))
            break

    time_context = ast.get("time_context") or {}
    anchor = time_context.get("anchor") if isinstance(time_context, dict) else None
    if isinstance(anchor, str) and "." in anchor:
        table_candidates.append(anchor.split(".", 1)[0])

    for table_name in dict.fromkeys(table_candidates):
        try:
            entity_code = resolve(semantic_model_id, business_domain_id, table_name)
        except Exception as exc:
            logger.warning("dynamic entity resolution failed for table=%s: %s", table_name, exc)
            continue
        if entity_code:
            subject = ast.setdefault("subject", {})
            if isinstance(subject, dict):
                subject["entity"] = entity_code
                remember_subject(entity_code)
            break
    return json.dumps(ast, ensure_ascii=False)

# Runtime objects are read-only on import. Index rebuilds must be explicit API calls.
from embedding import embed_query
from vector_store import create_vector_store
from logger import logger
store = create_vector_store()


def _normalize_business_domain_scope(
    business_domain_id: int | None,
    business_domain_ids: list[int] | tuple[int, ...] | None,
) -> list[int]:
    """An empty set is MODEL_WIDE; explicit multi-domain fails closed."""
    return normalize_domains(business_domain_id, business_domain_ids)


def _vector_semantic_ambiguities(
    knowledge: dict,
    user_query: str,
    *,
    preferred_metric_codes: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """Return blocking choices when one explicit phrase has multiple vector hits.

    Multiple independent concepts in one question are valid (for example two
    explicitly named metrics).  Ambiguity exists only when the *same literal
    phrase* maps to more than one canonical record of the same semantic type.
    The caller-bound metric contract remains authoritative and is never reopened.
    """

    query = str(user_query or "")
    pools = knowledge.get("_ambiguity_candidates")
    if not isinstance(pools, dict):
        pools = {
            key: knowledge.get(key, [])
            for key in (
                "metrics", "entities", "dimensions", "attributes",
                "relations", "entity_attribute_values",
            )
        }
    bound_metrics = set(map(str, preferred_metric_codes or []))
    specifications = {
        "metric": {
            "pool": "metric", "ambiguity_type": "metric", "slot": "metrics",
            "code": "metric_code", "name": "metric_name",
            "terms": ("metric_name", "synonyms", "metric_code"),
            "title": "指标",
        },
        "entity": {
            "pool": "entity", "ambiguity_type": "subject", "slot": "subject",
            "code": "entity_code", "name": "entity_name",
            "terms": ("entity_name", "entity_alias", "entity_code"),
            "title": "查询对象",
        },
        "dimension": {
            "pool": "dimension", "ambiguity_type": "dimension", "slot": "dimensions",
            "code": "dim_code", "name": "dim_name",
            "terms": ("dim_name", "synonyms", "dim_code"),
            "title": "维度",
        },
        "attribute": {
            "pool": "attribute", "ambiguity_type": "filter_slot", "slot": "fields",
            "code": "attr_code", "name": "attr_name",
            "terms": ("attr_name", "attr_code"),
            "title": "属性字段",
        },
        "relation": {
            "pool": "relation", "ambiguity_type": "schema_relation", "slot": "relation",
            "code": "relation_code", "name": "relation_name",
            "terms": ("relation_name", "relation_semantic", "relation_code"),
            "title": "实体关系",
        },
        "entity_attribute_value": {
            "pool": "entity_attribute_value", "ambiguity_type": "entity_value", "slot": "filters",
            "code": "attr_code", "name": "attr_value",
            "terms": ("attr_value",),
            "title": "实体值",
        },
    }

    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for semantic_type, spec in specifications.items():
        if semantic_type == "metric" and bound_metrics:
            continue
        records = pools.get(spec["pool"], [])
        if not isinstance(records, list):
            continue
        for record in records:
            metadata = getattr(record, "metadata", {}) or {}
            if str(metadata.get("type") or "") != semantic_type:
                continue
            canonical_code = str(metadata.get(spec["code"]) or "").strip()
            canonical_name = str(metadata.get(spec["name"]) or "").strip()
            if not canonical_code or not canonical_name:
                continue
            terms: list[str] = []
            for key in spec["terms"]:
                terms.extend(_metadata_term_values(metadata.get(key)))
            if semantic_type == "attribute":
                parent_name = str(metadata.get("parent_name") or "").strip()
                if parent_name:
                    terms.extend(
                        parent_name + term
                        for term in list(terms)
                        if term and not term.startswith(parent_name)
                    )
            for phrase in dict.fromkeys(term.strip() for term in terms if len(term.strip()) >= 2):
                if phrase.casefold() not in query.casefold():
                    continue
                key = (semantic_type, phrase.casefold())
                bucket = grouped.setdefault(key, {"phrase": phrase, "records": {}})
                identity = canonical_code
                details = bucket["records"]
                current = details.get(identity)
                score = float(getattr(record, "score", 0.0) or 0.0)
                if current is None or score > current["score"]:
                    label = f"{canonical_name}（{canonical_code}）"
                    if semantic_type == "attribute":
                        parent = str(metadata.get("parent_name") or "").strip()
                        if parent:
                            label = f"{parent} / {label}"
                    details[identity] = {
                        "label": label,
                        "semantic_type": semantic_type,
                        "canonical_code": canonical_code,
                        "canonical_name": canonical_name,
                        "record_id": str(getattr(record, "id", "")),
                        "score": score,
                        "business_domain_id": metadata.get("business_domain_id"),
                    }

    # A longer uniquely-grounded phrase defeats ambiguity from its nested alias.
    # Example: “经销商名称” must not be blocked merely because “名称” is shared.
    unique_phrases = {
        (semantic_type, str(bucket["phrase"]).casefold())
        for (semantic_type, _), bucket in grouped.items()
        if len(bucket["records"]) == 1
    }
    result: list[dict] = []
    for (semantic_type, folded_phrase), bucket in grouped.items():
        records = list(bucket["records"].values())
        if len(records) < 2:
            continue
        # A clarification reply carries the selected canonical label/code while
        # the rewritten pending question still contains the old shared alias.
        # Treat that explicit canonical identifier as resolution evidence so the
        # same ambiguity cannot loop forever on the next turn.
        explicitly_selected = [
            item for item in records
            if any(
                term
                and term.casefold() != folded_phrase
                and term.casefold() in query.casefold()
                for term in (
                    str(item.get("canonical_code") or "").strip(),
                    str(item.get("canonical_name") or "").strip(),
                )
            )
        ]
        if explicitly_selected:
            continue
        if any(
            other_type == semantic_type
            and folded_phrase != other_phrase
            and folded_phrase in other_phrase
            for other_type, other_phrase in unique_phrases
        ):
            continue
        records.sort(key=lambda item: (-item["score"], item["label"]))
        spec = specifications[semantic_type]
        candidates = [item["label"] for item in records[:10]]
        digest = hashlib.sha256(
            f"{semantic_type}:{folded_phrase}:{'|'.join(candidates)}".encode("utf-8")
        ).hexdigest()[:20]
        result.append({
            "ambiguity_id": f"vector-semantic-{digest}",
            "type": spec["ambiguity_type"],
            "phrase": bucket["phrase"],
            "affected_slots": [spec["slot"]],
            "question": (
                f"“{bucket['phrase']}”命中多个{spec['title']}，"
                "请选择本次要使用的规范项："
            ),
            "candidates": candidates,
            "candidate_details": records[:10],
            "material_impact": f"不同选择会改变本次查询使用的{spec['title']}和生成的 SQL",
            "blocking": True,
        })
    return result[:5]


def _ambiguity_asl(ambiguities: list[dict]) -> str:
    return json.dumps({
        "version": "2.0",
        "intent": "query",
        "subject": {},
        "metrics": [],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": None,
        "limit": None,
        "having": [],
        "ambiguity": ambiguities,
    }, ensure_ascii=False)

def main(
    query: str,
    retrieval_query: str | None = None,
    store=None,
    semantic_model_id: int = None,
    business_domain_id: int = None,
    business_domain_ids: list[int] | tuple[int, ...] | None = None,
    preferred_metric_codes: list[str] | tuple[str, ...] | None = None,
    metric_selection_authoritative: bool = False,
    intent_asl_contract: dict | None = None,
    analysis_operator: str | None = None,
    result_contract: dict | None = None,
    exploration_requirements: dict | None = None,
    include_evidence: bool = False,
):
    """自然语言 → DSL

    Args:
        query: 用户自然语言查询
        store: 向量存储实例，为 None 时使用模块级默认 store
        semantic_model_id: 语义建模 ID（必填，限定检索作用域）
        business_domain_id: 兼容旧版的单业务域 ID（可选）
        business_domain_ids: 当前仅支持一个不同显式域；空列表表示 MODEL_WIDE
    """
    if type(semantic_model_id) is not int or semantic_model_id <= 0:
        raise ValueError("semantic_model_id must be a positive integer")
    domain_ids = _normalize_business_domain_scope(
        business_domain_id,
        business_domain_ids,
    )
    metric_codes = list(dict.fromkeys(preferred_metric_codes or []))
    if any(not isinstance(code, str) or not code.strip() for code in metric_codes):
        raise ValueError("preferred_metric_codes must contain non-empty strings")
    domain_scope: int | list[int] | None = (
        domain_ids[0]
        if len(domain_ids) == 1
        else (domain_ids if domain_ids else None)
    )
    if store is None:
        store = globals()["store"]
    builder = PromptBuilder(
        store,
        embed_query,
        # A semantic-model-wide request can span sales, inventory and after-sales.
        # Keep a slightly wider final set in AUTO mode; scoped queries stay compact.
        top_k=3 if len(domain_ids) == 1 else 4,
        semantic_model_id=semantic_model_id,
        business_domain_id=business_domain_id,
        business_domain_ids=domain_ids,
        preferred_metric_codes=metric_codes,
        authoritative_entity_scope=(
            metric_selection_authoritative and not metric_codes
        ),
    )
    execution_query = query
    if (
        metric_selection_authoritative
        and not metric_codes
        and intent_asl_contract is None
    ):
        execution_query += (
            "\n调用方已确认本次不选择聚合指标。若问题询问的是已发布实体属性值，"
            "请生成无指标明细投影，并同时保留语义层中可用的配套单位字段；"
            "不得仅因 metrics 为空要求用户补充指标。"
        )
    if intent_asl_contract is not None:
        execution_query += (
            "\n调用方已确认以下 Intent-ASL 结构契约。它只约束查询形状；"
            "语义标签必须从本轮召回元数据解析为已注册字段，不得猜测物理列。"
            "\nintent_asl_contract="
            + json.dumps(intent_asl_contract, ensure_ascii=False, separators=(",", ":"))
        )
    if analysis_operator is not None and result_contract is not None:
        execution_query += (
            "\n本次查询服务于确定性分析算子，必须严格按以下结构化结果契约生成ASL。"
            "选择能够分别表达各语义角色的指标/维度，保留要求的分组粒度，不得返回原始明细代替契约结果。"
            f"\nanalysis_operator={analysis_operator}"
            "\nresult_contract="
            + json.dumps(result_contract, ensure_ascii=False, separators=(",", ":"))
        )
    if exploration_requirements is not None:
        execution_query += (
            "\n本次是开放式数据探索。只能从召回的语义元数据中选择字段，并严格遵守以下受限查询形状；"
            "返回适合趋势、异常、集中度和相关性检测的聚合数据，不返回无边界明细，不得虚构指标或维度。"
            "\nexploration_requirements="
            + json.dumps(exploration_requirements, ensure_ascii=False, separators=(",", ":"))
        )
    # Semantic recall and deterministic business validation must only see the
    # user's business wording.  ``execution_query`` may contain caller-owned
    # instructions such as "不得添加最近一年"; treating those control words as
    # user semantics can manufacture a date range from the negated example.
    semantic_user_query = retrieval_query or query

    # Semantic recall must only see the user's business wording. The structured
    # contract is an execution constraint, not retrieval evidence; embedding its
    # aliases and JSON can displace the actual metric and dimension candidates.
    mprompt = builder.build(semantic_user_query)
    # 提示词中可能包含实体属性值或业务元数据，禁止完整写入控制台和日志。
    logger.info(
        "已组装ASL提示词: sm=%s, bd=%s, prompt_chars=%s",
        semantic_model_id,
        domain_ids or None,
        len(mprompt),
    )
    vector_ambiguities = _vector_semantic_ambiguities(
        getattr(builder, "last_knowledge", {}),
        semantic_user_query,
        preferred_metric_codes=metric_codes,
    )
    if vector_ambiguities:
        clarified = _ambiguity_asl(vector_ambiguities)
        logger.info(
            "vector semantic ambiguity requires confirmation: sm=%s, bds=%s, count=%s",
            semantic_model_id,
            domain_ids or None,
            len(vector_ambiguities),
        )
        if not include_evidence:
            return clarified
        return {
            "result": clarified,
            "semantic_evidence": _build_semantic_evidence(
                clarified,
                getattr(builder, "last_knowledge", {}),
                semantic_model_id,
                domain_ids,
            ),
            "asl_contract": intent_asl_contract,
            "asl_validation": "PASS",
            "asl_validation_error_code": None,
            "asl_repair": [],
        }
    if exploration_requirements is not None:
        recalled_metric_codes = sorted(_known_codes(
            getattr(builder, "last_knowledge", {}), "metrics", "metric_code"
        ))
        logger.info(
            "ASL exploration scope: sm=%s, bds=%s, retrieval_chars=%s, "
            "recalled_metric_codes=%s, recalled_dimensions=%s",
            semantic_model_id,
            domain_ids or None,
            len(semantic_user_query),
            recalled_metric_codes,
            len(getattr(builder, "last_knowledge", {}).get("dimensions", [])),
        )

    agent = create_deep_agent(
        model=_get_chat_model(),
        system_prompt=mprompt,
    )

    res = agent.invoke({"messages": execution_query})
    content = res["messages"][-1].content
    try:
        json.loads(_strip_code_fence(content))
    except (TypeError, ValueError):
        # Retry only malformed transport format. Semantic safety failures are
        # still rejected later by the deterministic ASL validator.
        logger.warning(
            "model returned malformed ASL JSON; retrying format generation once: sm=%s, bds=%s",
            semantic_model_id,
            domain_ids or None,
        )
        retry_message = (
            f"用户问题：{execution_query}\n"
            "上一次输出不是合法 JSON。请重新生成，并且只返回一个完整的 ASL 2.0 JSON 对象；"
            "不要输出解释、Markdown 或代码块。"
        )
        res = agent.invoke({"messages": retry_message})
        content = res["messages"][-1].content
    # 剥离 LLM 可能附加的 ```json 代码块包裹
    normalized = _normalize_caller_bound_metrics(
        _strip_code_fence(content),
        getattr(builder, "last_knowledge", {}),
        metric_codes,
        semantic_user_query,
    )
    if exploration_requirements is not None and not metric_codes:
        normalized = _normalize_exploration_metrics(
            normalized,
            getattr(builder, "last_knowledge", {}),
            exploration_requirements,
        )
    normalized = _normalize_semantic_references(
        normalized,
        getattr(builder, "last_knowledge", {}),
        semantic_user_query,
        semantic_model_id,
        domain_scope,
    )
    if exploration_requirements is not None and not metric_codes:
        normalized = _normalize_exploration_metrics(
            normalized,
            getattr(builder, "last_knowledge", {}),
            exploration_requirements,
        )
    normalized = _normalize_explicit_time_granularity(
        normalized,
        getattr(builder, "last_knowledge", {}),
        execution_query,
    )
    normalized = _normalize_all_time_snapshot_scope(
        normalized,
        getattr(builder, "last_knowledge", {}),
        all_time_scope=(
            "时间口径为截至业务数据水位的全部可用历史数据" in execution_query
            or "TIME_SCOPE=ALL_TIME" in execution_query
        ),
    )
    normalized = _normalize_dynamic_subject(
        normalized,
        semantic_model_id,
        domain_scope,
        getattr(builder, "last_knowledge", {}),
    )
    normalized, contract_repairs = _apply_intent_asl_contract(
        normalized,
        getattr(builder, "last_knowledge", {}),
        intent_asl_contract,
        semantic_model_id,
        domain_scope,
    )
    if intent_asl_contract is not None:
        repaired_ast = json.loads(normalized)
        _dedupe_equivalent_dimensions(
            repaired_ast, getattr(builder, "last_knowledge", {})
        )
        normalized = json.dumps(repaired_ast, ensure_ascii=False)
    validation_args = (
        normalized,
        getattr(builder, "last_knowledge", {}),
        semantic_user_query,
    )
    validated = (
        _validate_asl_output(*validation_args, metric_codes)
        if metric_selection_authoritative or metric_codes
        else _validate_asl_output(*validation_args)
    )
    _validate_intent_asl_contract(
        validated,
        intent_asl_contract,
        getattr(builder, "last_knowledge", {}),
    )
    if analysis_operator is not None and result_contract is not None:
        validated_object = json.loads(validated)
        validated_object["analysis_contract"] = {
            "contract_version": "1.0",
            "analysis_operator": analysis_operator,
            "result_contract": result_contract,
            "producer": "OAGNET",
        }
        validated = json.dumps(validated_object, ensure_ascii=False)
    if exploration_requirements is not None:
        validated_object = json.loads(validated)
        if not validated_object.get("ambiguity"):
            _normalize_exploration_time_granularity(
                validated_object, semantic_user_query
            )
            metric_count = len(validated_object.get("metrics") or [])
            dimension_count = len(validated_object.get("dimensions") or [])
            if not (
                exploration_requirements["minimum_numeric_metrics"]
                <= metric_count
                <= exploration_requirements["maximum_numeric_metrics"]
            ):
                raise ValueError("ASL metric selection does not satisfy exploration requirements")
            if not (
                exploration_requirements["minimum_dimensions"]
                <= dimension_count
                <= exploration_requirements["maximum_dimensions"]
            ):
                raise ValueError("ASL dimension selection does not satisfy exploration requirements")
            if exploration_requirements["prefer_time_dimension"]:
                has_time = bool(validated_object.get("time_context")) or any(
                    isinstance(item, dict) and item.get("granularity")
                    for item in (validated_object.get("dimensions") or [])
                )
                if not has_time:
                    raise ValueError("ASL exploration omitted the preferred time dimension")
        validated_object["analysis_exploration"] = {
            "contract_version": "1.0",
            "requirements": exploration_requirements,
            "producer": "OAGNET",
        }
        validated = json.dumps(validated_object, ensure_ascii=False)
    if metric_codes:
        selected_codes = [
            str(item.get("name"))
            for item in json.loads(validated).get("metrics", [])
            if isinstance(item, dict) and item.get("name")
        ]
        if set(selected_codes) != set(metric_codes) or len(selected_codes) != len(metric_codes):
            raise ValueError("ASL metric selection does not match caller-bound metrics")
    if not include_evidence:
        return validated
    logger.info(
        "Intent-ASL contract validated: intent=%s query_object=%s metric_required=%s "
        "metrics=%s detail_projection=%s filters=%s negative_filters=%s sorting=%s "
        "asl_contract=PASS asl_validation=PASS asl_validation_error_code=None "
        "asl_repair=%s final_projection=%s",
        (intent_asl_contract or {}).get("intent"),
        (intent_asl_contract or {}).get("query_object"),
        (intent_asl_contract or {}).get("metric_required"),
        [item.get("name") for item in json.loads(validated).get("metrics", []) if isinstance(item, dict)],
        (intent_asl_contract or {}).get("required_projections"),
        (intent_asl_contract or {}).get("filters"),
        (intent_asl_contract or {}).get("negative_filters"),
        (intent_asl_contract or {}).get("sorting"),
        contract_repairs,
        [item.get("name") for item in json.loads(validated).get("dimensions", []) if isinstance(item, dict)],
    )
    return {
        "result": validated,
        "semantic_evidence": _build_semantic_evidence(
            validated,
            getattr(builder, "last_knowledge", {}),
            semantic_model_id,
            domain_ids,
        ),
        "asl_contract": intent_asl_contract,
        "asl_validation": "PASS",
        "asl_validation_error_code": None,
        "asl_repair": contract_repairs,
    }

if __name__ == "__main__":
    query = "30 ——40年龄段 购买量前十的商品"
    # 默认限定到「商超业务数据」(sm=6) 的「销售交易域」(bd=7)
    res = main(query, semantic_model_id=6, business_domain_id=7)
    print(res)
