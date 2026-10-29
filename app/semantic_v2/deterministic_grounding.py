"""Bounded deterministic edits from current-turn evidence.

This is a fast path between the current-turn model and SemanticEdits.  It only
publishes when every required semantic value is proved by the pinned catalog or
an exact source-value observation.  A declined fast path leaves the existing
SemanticEdits path unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from . import models as m
from .authorized_contract import contract_digest
from .enums import CatalogType
from .pending_recognition import governed_aliases
from .recognition_client import RecognitionFailure
from .structured_edits import filter_targets


SUPPORTED_SHAPES = {
    "SCALAR_AGGREGATE": "SCALAR_AGGREGATE",
    "GROUPED_AGGREGATE": "GROUPED_AGGREGATE",
    "RANKING": "RANKING",
    "METADATA_LOOKUP": "METADATA",
    "DETAIL_ROWS": "DETAIL_ROWS",
}
SUPPORTED_SLOTS = {"subject", "metrics", "dimensions", "filter_expression", "time_spec", "ranking_spec"}
COMPLEX_ROLES = {"RELATIONSHIP", "RELATION_TARGET", "COMPARISON_BASELINE", "DATASET_SOURCE"}
GROUNDABLE_ROLES = {"MEASURE", "GROUP_BY", "SUBJECT_ENTITY", "FILTER_VALUE", "TIME_RANGE", "LIMIT"}


@dataclass(frozen=True)
class DeterministicGrounding:
    draft: dict
    derived_evidence: frozenset[tuple[str, str]] = frozenset()
    reason_codes: tuple[str, ...] = ()


def _text(value: object) -> str:
    return "".join(str(value).split()).casefold()


def _candidate_matches(candidate: dict, surface: str) -> bool:
    terms = {candidate.get("name"), candidate.get("code"), *candidate.get("aliases", ())}
    return _text(surface) in {_text(value) for value in terms if isinstance(value, str) and value.strip()}


def _candidate_match_score(candidate: dict, surface: str, *, allow_contained: bool) -> int:
    normalized = _text(surface)
    terms = {candidate.get("name"), candidate.get("code"), *candidate.get("aliases", ())}
    scores = []
    for value in terms:
        if not isinstance(value, str) or not value.strip():
            continue
        term = _text(value)
        if normalized == term:
            scores.append(10000 + len(term))
        elif allow_contained and len(term) >= 2 and term in normalized:
            scores.append(len(term))
    return max(scores, default=0)


def _unique_handle(candidates: list[dict], mention_id: str, role: str, surface: str,
                   *, catalog_type: str | None = None) -> str | None:
    rows = [row for row in candidates
            if row["mention_id"] == mention_id and str(row["role"]) == role
            and (catalog_type is None or str(row["catalog_type"]) == catalog_type)]
    # Metrics and dimensions commonly include an exact governed synonym inside
    # a larger grammatical surface (for example a unit or qualifier). Choose
    # only one longest catalog term. Entity types remain whole-surface exact so
    # an entity instance can never collapse to its generic type.
    allow_contained = catalog_type in {"METRIC", "DIMENSION"}
    scored = [(row, _candidate_match_score(row, surface, allow_contained=allow_contained))
              for row in rows]
    best = max((score for _, score in scored), default=0)
    matched = {row["binding_handle"] for row, score in scored if score == best and score > 0}
    if not matched and allow_contained and len(_text(surface)) >= 2:
        # Short noun phrases such as ``科室`` can denote a uniquely offered
        # governed count metric such as ``科室总数量``.  Accept this direction
        # only when the complete candidate set proves one handle; competing
        # metrics keep the model path and its ambiguity handling.
        normalized = _text(surface)
        matched = {row["binding_handle"] for row in rows
            if any(normalized in _text(value) for value in
                {row.get("name"), row.get("code"), *row.get("aliases", ())}
                if isinstance(value, str) and value.strip())}
    return next(iter(matched)) if len(matched) == 1 else None


def _operation(parse, slot: str, mention_ids: list[str], *, default: str = "SET") -> str | None:
    marked = {str(marker.operation_hint) for marker in parse.operation_markers
              if marker.slot_name == slot and marker.mention_id in mention_ids}
    if len(marked) > 1:
        return None
    return next(iter(marked), default)


def _eligible_source_attributes(session, surface: str) -> list[dict]:
    eligible = set(session._pin.entity_value_lookup_fields())
    owners = {}
    for candidate in session.candidates(CatalogType.ENTITY):
        metadata = session._rows[candidate["candidate_id"]].metadata
        terms = {candidate.get("display_name"), candidate.get("canonical_code"), *governed_aliases(metadata)}
        if any(_text(surface).endswith(_text(term)) and _text(surface) != _text(term)
               for term in terms if isinstance(term, str) and term.strip()):
            owners[candidate["canonical_code"]] = candidate
    result = []
    for candidate in session.candidates(CatalogType.ATTRIBUTE):
        if candidate["candidate_id"] not in eligible:
            continue
        metadata = session._rows[candidate["candidate_id"]].metadata
        if (metadata.get("is_main_attribute") is True and metadata.get("field_mapping")
                and (len(owners) != 1 or metadata.get("parent") in owners)):
            result.append(candidate)
    if len(result) > 64:
        raise RecognitionFailure("V2_DETERMINISTIC_SOURCE_FIELD_BUDGET_EXCEEDED")
    return result


def _source_matches(session, attributes: list[dict], surface: str) -> list[tuple[dict, dict]]:
    matched = []
    for attribute in attributes:
        values = session.lookup_source_values(attribute["candidate_id"], surface, implicit=True)
        exact = [value for value in values if value.get("display_name") == surface]
        selected = exact or values
        if len(selected) == 1:
            matched.append((attribute, selected[0]))
        elif len(selected) > 1:
            return []
    return matched


def _add_handle(handles: dict, mention_id: str, role: str, candidate_id: str) -> str:
    handle = "binding:" + contract_digest([mention_id, role, candidate_id])[:32]
    handles[handle] = (candidate_id, role, mention_id)
    return handle


def _owner_entity_handle(session, handles: dict, mention_id: str, attribute: dict) -> str | None:
    owner = session._rows[attribute["candidate_id"]].metadata.get("parent")
    entities = [candidate for candidate in session.candidates(CatalogType.ENTITY)
                if candidate.get("canonical_code") == owner]
    if len(entities) != 1:
        return None
    return _add_handle(handles, mention_id, "SUBJECT_ENTITY", entities[0]["candidate_id"])


def _dimension_entity_handle(session, handles: dict, mention_id: str,
                             dimension_handle: str) -> str | None:
    candidate_id, role, bound_mention = handles[dimension_handle]
    if role != "GROUP_BY" or bound_mention != mention_id:
        return None
    metadata = session._rows[candidate_id].metadata
    entity_ids = {str(binding.get("entity")) for binding in metadata.get("bind_entities", ())
                  if isinstance(binding, dict) and binding.get("entity")}
    if len(entity_ids) != 1:
        return None
    entities = [candidate for candidate in session.candidates(CatalogType.ENTITY)
                if str(session._rows[candidate["candidate_id"]].metadata.get("entity_id")) in entity_ids]
    if len(entities) != 1:
        return None
    return _add_handle(handles, mention_id, "SUBJECT_ENTITY", entities[0]["candidate_id"])


def _filter_attribute(session, node) -> dict | None:
    if not isinstance(node, m.Predicate) or node.field_ref.catalog_type != "ATTRIBUTE":
        return None
    eligible = set(session._pin.entity_value_lookup_fields())
    found = [candidate for candidate in session.candidates(CatalogType.ATTRIBUTE)
        if candidate["candidate_id"] in eligible and
        str(session._rows[candidate["candidate_id"]].metadata.get("catalog_logical_id")) ==
        str(node.field_ref.canonical_id)]
    return found[0] if len(found) == 1 else None


def _replacement_filter_target(session, target, surface: str) -> tuple[str, dict] | None:
    matched = []
    for target_handle, (_, node) in filter_targets(target).items():
        attribute = _filter_attribute(session, node)
        if attribute is None:
            continue
        values = session.lookup_source_values(attribute["candidate_id"], surface, implicit=True)
        exact = [value for value in values if value.get("display_name") == surface]
        selected = exact or values
        if len(selected) > 1:
            return None
        if len(selected) == 1:
            matched.append((target_handle, attribute))
    return matched[0] if len(matched) == 1 else None


def _clear_filter_target(session, target) -> str | None:
    state = next(version.semantics for version in target.versions
                 if version.version == target.active_version)
    subject_code = state.subject.canonical_code if state.subject is not None else None
    eligible = []
    for target_handle, (_, node) in filter_targets(target).items():
        attribute = _filter_attribute(session, node)
        if attribute is None:
            continue
        owner = session._rows[attribute["candidate_id"]].metadata.get("parent")
        # A concrete subject is represented by its main-attribute predicate.
        # A generic "unbounded" edit must not erase that identity.  It may
        # clear the sole additional predicate only; zero/multiple choices fall
        # back to the existing model and target-selection guards.
        if subject_code is None or owner != subject_code:
            eligible.append(target_handle)
    return eligible[0] if len(eligible) == 1 else None


def _limit(surface: str) -> int | None:
    compact = "".join(surface.split()).casefold()
    matched = re.fullmatch(r"(?:排名)?前([1-9][0-9]{0,3})|top([1-9][0-9]{0,3})", compact)
    if matched is None:
        return None
    value = int(next(group for group in matched.groups() if group is not None))
    return value if value <= 10000 else None


def build_deterministic_grounding(*, session, parse, candidates: list[dict], handles: dict,
                                  context_trace: dict, current, pending, now) -> DeterministicGrounding | None:
    """Return a complete safe draft, or ``None`` to use SemanticEdits.

    There is no partial merge: once any declared semantic surface cannot be
    proved, the whole deterministic proposal is discarded.
    """
    relation = context_trace.get("FINAL_RELATION")
    if (relation not in {"NEW_TASK", "MODIFY", "ADD", "REPLACE", "REMOVE", "CLEAR", "CONTINUE"}
            or pending is not None or current.pending is not None
            or parse.query_shape_prediction is None
            or str(parse.query_shape_prediction) not in SUPPORTED_SHAPES
            or any(COMPLEX_ROLES.intersection(map(str, mention.candidate_roles)) for mention in parse.mentions)):
        return None
    if relation == "NEW_TASK" and (parse.reference_signals or parse.followup_signals):
        return None
    if relation != "NEW_TASK" and context_trace.get("FINAL_TARGET") not in current.tasks:
        return None
    declared_slots = {slot for slot, mention_ids in parse.explicit_slot_mentions.items()
                      if mention_ids}
    if declared_slots - SUPPORTED_SLOTS:
        return None

    mentions = {mention.mention_id: mention for mention in parse.mentions}
    edits: list[dict] = []
    filter_edits: list[dict] = []
    temporal_edits: list[dict] = []
    source_requests: list[dict] = []
    derived: set[tuple[str, str]] = set()
    covered: set[tuple[str, str]] = set()
    source_attributes: list[dict] | None = None

    def ids_for(slot: str) -> list[str]:
        return list(dict.fromkeys(parse.explicit_slot_mentions.get(slot, ())))

    def add_edit(slot: str, ids: list[str], value, operation: str | None = None):
        operation = operation or _operation(parse, slot, ids)
        if operation is None:
            raise LookupError
        edits.append(dict(slot_path=slot, operation=operation,
                          evidence_mention_ids=ids, value=value))
        covered.update((slot, mention_id) for mention_id in ids)

    try:
        metric_ids = ids_for("metrics")
        metric_handles = []
        for mention_id in metric_ids:
            mention = mentions[mention_id]
            handle = _unique_handle(candidates, mention_id, "MEASURE", mention.surface,
                                    catalog_type="METRIC")
            if handle is None:
                return None
            metric_handles.append({"binding_handle": handle})
        if metric_ids:
            add_edit("metrics", metric_ids, metric_handles)

        dimension_ids = ids_for("dimensions")
        if str(parse.query_shape_prediction) == "RANKING" and not dimension_ids:
            possible = [mention.mention_id for mention in parse.mentions
                        if "GROUP_BY" in map(str, mention.candidate_roles)]
            if len(possible) == 1:
                dimension_ids = possible
                derived.add(("dimensions", possible[0]))
        dimension_handles = []
        for mention_id in dimension_ids:
            mention = mentions[mention_id]
            handle = _unique_handle(candidates, mention_id, "GROUP_BY", mention.surface,
                                    catalog_type="DIMENSION")
            if handle is None:
                return None
            dimension_handles.append({"binding_handle": handle})
        if dimension_ids:
            add_edit("dimensions", dimension_ids, dimension_handles)

        subject_ids = ids_for("subject")
        if len(subject_ids) > 1:
            return None
        if subject_ids:
            mention_id = subject_ids[0]
            mention = mentions[mention_id]
            handle = _unique_handle(candidates, mention_id, "SUBJECT_ENTITY", mention.surface,
                                    catalog_type="ENTITY")
            if handle is None and mention_id in dimension_ids:
                dimension_index = dimension_ids.index(mention_id)
                handle = _dimension_entity_handle(session, handles, mention_id,
                    dimension_handles[dimension_index]["binding_handle"])
            if handle is None:
                # Replacing a concrete instance means editing its existing
                # filter identity.  A subject-type edit alone would retain the
                # old instance and silently drop the new one.
                if relation != "NEW_TASK":
                    return None
                source_attributes = source_attributes or _eligible_source_attributes(session, mention.surface)
                matches = _source_matches(session, source_attributes, mention.surface)
                if len(matches) != 1:
                    return None
                handle = _owner_entity_handle(session, handles, mention_id, matches[0][0])
            if handle is None:
                return None
            add_edit("subject", subject_ids, {"binding_handle": handle})

        filter_ids = ids_for("filter_expression")
        if filter_ids:
            if len(filter_ids) != 1:
                return None
            mention_id = filter_ids[0]
            mention = mentions[mention_id]
            operation = _operation(parse, "filter_expression", filter_ids)
            if operation is None:
                return None
            if relation == "NEW_TASK":
                if operation != "SET" or "FILTER_VALUE" not in map(str, mention.candidate_roles):
                    return None
                source_attributes = source_attributes or _eligible_source_attributes(session, mention.surface)
                matches = _source_matches(session, source_attributes, mention.surface)
                if len(matches) != 1:
                    return None
                attribute, _ = matches[0]
                field_handle = _add_handle(handles, mention_id, "FILTER_FIELD", attribute["candidate_id"])
                request_id = "deterministic-source:" + contract_digest([parse.turn_id, mention_id])[:24]
                source_requests.append(dict(request_id=request_id, mention_id=mention_id,
                                            field_binding_handles=[field_handle]))
                predicate = dict(node_type="PREDICATE",
                    field_ref={"value_field_request_id": request_id}, operator="EQ",
                    value={"value_request_id": request_id}, source="USER_EXPLICIT",
                    mention_ids=[mention_id], scope="CURRENT_TASK", validation_status="UNKNOWN")
                add_edit("filter_expression", filter_ids, predicate)
            else:
                target = current.tasks[context_trace["FINAL_TARGET"]]
                if operation == "REPLACE" and "FILTER_VALUE" in map(str, mention.candidate_roles):
                    matched = _replacement_filter_target(session, target, mention.surface)
                    if matched is None:
                        return None
                    target_handle, _ = matched
                    request_id = "deterministic-source:" + contract_digest([parse.turn_id, mention_id])[:24]
                    source_requests.append(dict(request_id=request_id, mention_id=mention_id,
                                                target_filter_handle=target_handle))
                    filter_edits.append(dict(operation="REPLACE", target_handle=target_handle,
                        evidence_mention_ids=filter_ids, value={"value_request_id": request_id}))
                elif operation == "CLEAR":
                    target_handle = _clear_filter_target(session, target)
                    if target_handle is None:
                        return None
                    filter_edits.append(dict(operation="CLEAR", target_handle=target_handle,
                        evidence_mention_ids=filter_ids, value=None))
                else:
                    return None
                covered.update(("filter_expression", item) for item in filter_ids)

        time_ids = ids_for("time_spec")
        if time_ids:
            if any("TIME_RANGE" not in map(str, mentions[mention_id].candidate_roles)
                   for mention_id in time_ids):
                return None
            operation = _operation(parse, "time_spec", time_ids)
            if operation is None:
                return None
            if relation == "NEW_TASK":
                add_edit("time_spec", time_ids, dict(source="USER_EXPLICIT",
                    boundary="LEFT_CLOSED_RIGHT_OPEN", calendar="NATURAL"), operation)
            else:
                if operation not in {"SET", "REPLACE", "CLEAR"}:
                    return None
                value = None
                if operation != "CLEAR":
                    from .explicit_time import normalize_range
                    ranges = [normalize_range(mentions[mention_id].surface, now)
                              for mention_id in time_ids]
                    if any(value != ranges[0] for value in ranges[1:]):
                        return None
                    value = ranges[0].model_dump(mode="json")
                temporal_edits.append(dict(component="RANGE", operation=operation,
                    evidence_mention_ids=time_ids, value=value))
                covered.update(("time_spec", mention_id) for mention_id in time_ids)

        ranking_ids = ids_for("ranking_spec")
        if str(parse.query_shape_prediction) == "RANKING":
            limit_mentions = [mention for mention in parse.mentions
                              if "LIMIT" in map(str, mention.candidate_roles)]
            if len(metric_ids) != 1 or len(dimension_ids) != 1 or len(limit_mentions) != 1:
                return None
            limit = _limit(limit_mentions[0].surface)
            if limit is None:
                return None
            evidence = list(dict.fromkeys([*ranking_ids, metric_ids[0], limit_mentions[0].mention_id]))
            if not ranking_ids:
                ranking_ids = [limit_mentions[0].mention_id]
                derived.add(("ranking_spec", limit_mentions[0].mention_id))
            add_edit("ranking_spec", evidence, dict(rank_by=metric_handles[0], direction="DESC",
                limit=limit, ties_policy="EXCLUDE_TIES", nulls_policy="EXCLUDE",
                stable_tiebreakers=[]), _operation(parse, "ranking_spec", ranking_ids))

        declared = {(slot, mention_id) for slot, ids in parse.explicit_slot_mentions.items()
                    for mention_id in ids}
        marked = {(marker.slot_name, marker.mention_id) for marker in parse.operation_markers}
        if not (declared | marked) <= covered | derived:
            return None
        covered_ids = {mention_id for _, mention_id in covered | derived}
        for mention in parse.mentions:
            roles = set(map(str, mention.candidate_roles))
            if (mention.explicit and roles & GROUNDABLE_ROLES and mention.mention_id not in covered_ids):
                return None

        shape = str(parse.query_shape_prediction)
        if relation == "NEW_TASK" and shape in {"SCALAR_AGGREGATE", "GROUPED_AGGREGATE", "RANKING"} and not metric_ids:
            return None
        if relation == "NEW_TASK" and shape in {"GROUPED_AGGREGATE", "RANKING"} and not dimension_ids:
            return None
        if relation == "NEW_TASK" and shape in {"METADATA_LOOKUP", "DETAIL_ROWS"} and not subject_ids:
            return None
        if not edits and not filter_edits and not temporal_edits:
            return None
        payload = "INHERIT" if relation != "NEW_TASK" else SUPPORTED_SHAPES[shape]
        return DeterministicGrounding(draft=dict(edits=edits, filter_edits=filter_edits,
            temporal_edits=temporal_edits,
            source_value_requests=source_requests, payload_type=payload),
            derived_evidence=frozenset(derived),
            reason_codes=("CURRENT_TURN_CATALOG_EXACT", "SOURCE_VALUE_EXACT" if source_requests else "CATALOG_EXACT"))
    except (KeyError, LookupError, RecognitionFailure):
        return None


def can_publish_from_deterministic_grounding(**kwargs) -> DeterministicGrounding | None:
    """Expose the single complete-or-fallback publication decision."""
    return build_deterministic_grounding(**kwargs)
