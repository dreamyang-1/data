"""Lower current mentions and offered fields to source-proved value choices."""
from itertools import product
from math import log2

from pydantic import ConfigDict, Field, model_validator

from . import models as m
from .authorized_contract import (CatalogBindingEvidence, contract_digest,
    validate_source_value_fields, SourceValueBindingEvidence)
from .recognition_client import RecognitionFailure
from .enums import CatalogType
from .pipeline import collect_bound_refs
from .slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint
from .state_machine import PendingBlocker
from .structured_edits import filter_targets


class SourceValueRequestDraft(m.StrictModel):
    # Export the same exclusive alternatives as field_or_reference; a
    # Python-only validator is not visible to structured-output generation.
    model_config = ConfigDict(json_schema_extra={'oneOf': [
        {'required': ['field_binding_handles'], 'properties': {
            'field_binding_handles': {'minItems': 1}, 'target_filter_handle': {'type': 'null'}}},
        {'required': ['target_filter_handle'], 'properties': {
            'field_binding_handles': {'maxItems': 0}, 'target_filter_handle': {'type': 'string'}}},
    ]})
    request_id: m.Identifier
    mention_id: m.Identifier
    field_binding_handles: list[m.Identifier] = Field(default_factory=list, max_length=10)
    target_filter_handle: m.Identifier | None = None

    @model_validator(mode='after')
    def field_or_reference(self):
        if bool(self.field_binding_handles) == (self.target_filter_handle is not None):
            raise ValueError('source value requires offered fields or one current filter target')
        return self


def preserve_new_task_entity_instance(session, parse, patch, *, base, target):
    """Keep one exact current entity instance from collapsing to its entity type.

    The model-selected subject supplies only the entity *type*.  A different
    current-turn surface may be an instance, but it becomes a filter only when
    the pinned catalog exposes one governed main attribute and an exact source
    lookup proves one value.  Generic type names, grouping mentions, restored
    tasks, fuzzy matches and unproved values are never promoted here.
    """
    if target is not None or base != 0:
        return patch, []
    tentative = apply_task_patch(m.TaskSemanticState(), patch).semantics
    subject = tentative.subject
    if subject is None or subject.catalog_type != 'ENTITY':
        return patch, []
    proof = session._bindings.get(contract_digest(subject.model_dump(mode='json')))
    if not isinstance(proof, CatalogBindingEvidence):
        raise RecognitionFailure('V2_EXPLICIT_ENTITY_INSTANCE_SUBJECT_EVIDENCE_REQUIRED')
    entity = session._rows.get(proof.record_id)
    if entity is None or entity.metadata.get('type') != 'entity':
        raise RecognitionFailure('V2_EXPLICIT_ENTITY_INSTANCE_SUBJECT_EVIDENCE_REQUIRED')
    metadata = entity.metadata
    mentions = {mention.mention_id: mention for mention in parse.mentions}
    source_prefix = session._request.message_id + ':'
    source_ids = [value.removeprefix(source_prefix) for value in subject.source_mention_ids
                  if value.startswith(source_prefix)]
    if len(source_ids) != 1 or source_ids[0] not in mentions:
        return patch, []
    mention = mentions[source_ids[0]]
    if (not mention.explicit or mention.source_turn_id != parse.turn_id
            or mention.mention_id not in parse.explicit_slot_mentions.get('subject', ())
            or 'SUBJECT_ENTITY' not in mention.candidate_roles
            or 'GROUP_BY' in mention.candidate_roles):
        return patch, []

    from .pending_recognition import governed_aliases
    generic_terms = {metadata.get('entity_name'), metadata.get('entity_code'),
                     subject.display_name, subject.canonical_code, *governed_aliases(metadata)}
    normalized = mention.normalized_surface.strip().casefold()
    if normalized in {value.strip().casefold() for value in generic_terms
                      if isinstance(value, str) and value.strip()}:
        return patch, []

    declared = [value for value in metadata.get('attributes', ())
                if isinstance(value, dict) and value.get('is_main_attribute') is True]
    eligible = set(session._pin.entity_value_lookup_fields())
    attributes = []
    for candidate in session.candidates(CatalogType.ATTRIBUTE):
        row = session._rows[candidate['candidate_id']].metadata
        if (candidate['candidate_id'] in eligible and row.get('parent') == metadata.get('entity_code')
                and row.get('business_domain_id') == metadata.get('business_domain_id')
                and any(str(row.get('attribute_id')) == str(item.get('attribute_id'))
                        and row.get('attr_code') == item.get('attr_code')
                        and row.get('field_mapping') == item.get('field_mapping') for item in declared)):
            attributes.append(candidate['candidate_id'])
    if len(attributes) != 1:
        reason = ('V2_EXPLICIT_ENTITY_INSTANCE_FIELD_AMBIGUOUS' if len(attributes) > 1
                  else 'V2_EXPLICIT_ENTITY_INSTANCE_FIELD_UNPROVEN')
        raise RecognitionFailure(reason)
    attribute = attributes[0]

    source_id = source_prefix + mention.mention_id
    existing = [ref for ref in collect_bound_refs(tentative.filter_expression)
                if ref.semantic_role == 'FILTER_VALUE' and source_id in ref.source_mention_ids]
    for ref in existing:
        binding = session._bindings.get(contract_digest(ref.model_dump(mode='json')))
        if isinstance(binding, SourceValueBindingEvidence) and binding.attribute_record_id == attribute:
            return patch, []

    candidates = session.lookup_source_values(attribute, mention.surface, implicit=True)
    exact = [candidate for candidate in candidates if candidate.get('display_name') == mention.surface]
    selected = exact or candidates
    if len(selected) != 1:
        reason = ('V2_EXPLICIT_ENTITY_INSTANCE_AMBIGUOUS' if selected
                  else 'V2_EXPLICIT_ENTITY_INSTANCE_UNPROVEN')
        raise RecognitionFailure(reason)
    value_ref = session.bind(selected[0]['candidate_id'], 'FILTER_VALUE', (source_id,))
    field_ref = session.bind(attribute, 'FILTER_FIELD', (source_id,))
    predicate = m.Predicate(field_ref=field_ref, operator='EQ', value=m.EntityValueRef(ref=value_ref),
        source='USER_EXPLICIT', mention_ids=[mention.mention_id], scope='CURRENT_TASK',
        validation_status='UNKNOWN')
    expression = (m.BooleanFilterGroup(operator='AND',
        children=[tentative.filter_expression, predicate]) if tentative.filter_expression else predicate)

    all_operations = [operation
        for name in ('clears', 'removes', 'replacements', 'sets', 'adds', 'inherit_requests')
        for operation in getattr(patch, name)]
    filter_operations = [operation for operation in all_operations
                         if operation.slot_path == 'filter_expression']
    operations = [operation for operation in all_operations
                  if operation.slot_path != 'filter_expression']
    evidence = sorted({mention.mention_id, *(mention_id
        for operation in filter_operations for mention_id in operation.evidence_mention_ids)})
    operations.append(m.SlotOperation(operation_id='catalog:entity-instance:' + mention.mention_id,
        slot_path='filter_expression', operation='SET', new_value=expression.model_dump(mode='json'),
        source='CURRENT_EXPLICIT', reason_code='CURRENT_EXACT_ENTITY_INSTANCE',
        base_task_version=base, presence='PRESENT', evidence_mention_ids=evidence))
    result = TaskPatch.compile(operations, base_task_version=base)
    reduced = apply_task_patch(m.TaskSemanticState(), result).semantics
    validate_source_value_fields(reduced, tuple(session._bindings.values()))
    from .structured_edits import StructuredEditTrace
    trace = StructuredEditTrace(slot_path='filter_expression', operation='SET',
        target='entity-instance:' + mention.mention_id,
        evidence_mention_ids=[mention.mention_id],
        before_digest=semantic_fingerprint(tentative.filter_expression),
        after_digest=semantic_fingerprint(expression), lowered_operation='REPLACE')
    return result, [trace]


async def resolve_requests(session, parse, draft, handles, target, model):
    mentions = {item.mention_id: item for item in parse.mentions}
    alternatives = {}
    def requested_ids(value):
        if isinstance(value, dict):
            return ([value['value_request_id']] if 'value_request_id' in value else []) + [
                key for child in value.values() for key in requested_ids(child)]
        if isinstance(value, (list, tuple)):
            return [key for child in value for key in requested_ids(child)]
        return []
    used = set(requested_ids([e.model_dump(mode='json') for e in [*draft.edits, *draft.filter_edits]]))
    if used != {r.request_id for r in draft.source_value_requests}:
        raise RecognitionFailure('V2_SOURCE_VALUE_REQUEST_NOT_APPLIED')
    for request in draft.source_value_requests:
        mention = mentions.get(request.mention_id)
        if request.request_id in alternatives or mention is None or 'FILTER_VALUE' not in mention.candidate_roles:
            raise RecognitionFailure('V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED')
        candidates = {}
        fields = []
        if request.target_filter_handle is not None:
            current = filter_targets(target).get(request.target_filter_handle)
            if current is None or not isinstance(current[1], m.Predicate) or current[1].field_ref.catalog_type != 'ATTRIBUTE':
                raise RecognitionFailure('V2_SOURCE_VALUE_FILTER_TARGET_INVALID')
            if not any(e.target_handle == request.target_filter_handle and request.request_id in requested_ids(e.value)
                       for e in draft.filter_edits):
                raise RecognitionFailure('V2_SOURCE_VALUE_FILTER_TARGET_NOT_APPLIED')
            field = current[1].field_ref
            offered = session.candidates(CatalogType.ATTRIBUTE)
            found = [c for c in offered if session._rows[c['candidate_id']].metadata['catalog_logical_id'] == field.canonical_id]
            if len(found) != 1:
                raise RecognitionFailure('V2_SOURCE_VALUE_FILTER_TARGET_INVALID')
            fields.append((found[0]['candidate_id'], False))
        for handle in request.field_binding_handles:
            if handle not in handles:
                raise RecognitionFailure('V2_SOURCE_VALUE_FIELD_NOT_OFFERED')
            attribute, role, mid = handles[handle]
            if role != 'FILTER_FIELD':
                raise RecognitionFailure('V2_SOURCE_VALUE_FIELD_ROLE_INVALID')
            field_mention = mentions[mid]
            from .pending_recognition import governed_aliases
            metadata = session._rows[attribute].metadata
            explicit_names = {metadata.get('attr_name'), metadata.get('attr_code'), *governed_aliases(metadata)}
            implicit = 'FILTER_FIELD' not in field_mention.candidate_roles or field_mention.surface not in explicit_names
            if implicit and mid != mention.mention_id:
                raise RecognitionFailure('V2_SOURCE_VALUE_FIELD_MENTION_MISMATCH')
            fields.append((attribute, implicit))
        from .source_value_target import validate_requested_fields
        validate_requested_fields(draft, request, fields, target, session)
        for attribute, implicit in fields:
            removable = removal_targets(session, draft, request, target, attribute, mention.surface)
            if removable:
                for candidate in removable:
                    candidates[candidate] = (candidate, 'FILTER_VALUE', mention.mention_id)
                continue
            for candidate in session.lookup_source_values(attribute, mention.surface, implicit=implicit):
                candidates[candidate['candidate_id']] = (candidate['candidate_id'], 'FILTER_VALUE', mention.mention_id)
        # Discovery is allowed only after complete empty exact lookups and a
        # single selected field. Do not scan alternative or unrelated fields.
        selected_fields = set(fields)
        if not candidates and len(selected_fields) == 1:
            from .source_value_probe import select_probed_value
            attribute, implicit = next(iter(selected_fields))
            for candidate in await select_probed_value(model, session, attribute, mention, implicit=implicit):
                candidates[candidate['candidate_id']] = (candidate['candidate_id'], 'FILTER_VALUE', mention.mention_id)
        if not candidates:
            raise RecognitionFailure('V2_SOURCE_VALUE_NOT_FOUND')
        # A literal exact canonical value is stronger than only a normalized
        # spelling match. Multiple equally exact field/value matches stay open.
        exact = [choice for choice in candidates.values()
                 if session._value_candidates[choice[0]]['canonical_value'] == mention.surface]
        alternatives[request.request_id] = exact or list(candidates.values())
    count = 1
    for choices in alternatives.values(): count *= len(choices)
    if count > 10:
        raise RecognitionFailure('V2_SOURCE_VALUE_OPTION_BUDGET_EXCEEDED')
    return [dict(zip(alternatives, choices)) for choices in product(*alternatives.values())] if alternatives else [{}]


def removal_targets(session, draft, request, target, attribute, surface):
    """Identify an explicitly removed current member without requiring it to exist."""
    def contains(value):
        if isinstance(value, dict):
            return value.get('value_request_id') == request.request_id or any(contains(v) for v in value.values())
        return any(contains(v) for v in value) if isinstance(value, list) else False
    edits = [edit for edit in draft.filter_edits if contains(edit.value)]
    if not edits or any(edit.operation != 'REMOVE' for edit in edits) or any(contains(e.value) for e in draft.edits):
        return []
    possible = None
    fields = filter_targets(target)
    for edit in edits:
        choice = fields.get(edit.target_handle)
        if choice is None or not isinstance(choice[1], m.Predicate) or choice[1].operator not in {'EQ','IN'}:
            return []
        predicate = choice[1]
        if predicate.field_ref.canonical_id != session._rows[attribute].metadata['catalog_logical_id']:
            return []
        members = predicate.value.values if isinstance(predicate.value, m.ListValue) else [predicate.value]
        found = {}
        for member in members:
            if not isinstance(member, m.EntityValueRef) or member.ref.display_name != surface:
                continue
            key = contract_digest(member.ref.model_dump(mode='json'))
            proof = session._bindings.get(key) or session._archived_source_bindings.get(key)
            if isinstance(proof, SourceValueBindingEvidence):
                found[key] = proof
        possible = found if possible is None else {k:v for k,v in possible.items() if k in found}
    result = []
    for key, proof in (possible or {}).items():
        field = session._pin.entity_value_source(attribute)
        if field['mapping_hash'] != proof.mapping_hash:
            raise RecognitionFailure('V2_SOURCE_VALUE_REMOVAL_MAPPING_CHANGED')
        candidate = 'removal-target:' + key
        session._value_candidates[candidate] = dict(attribute_id=attribute, canonical_value=proof.canonical_value,
            field=field, removal_proof=proof)
        result.append(candidate)
    return result


def choice_handles(handles, choices):
    return {**handles, **{'source-value:' + key: value for key, value in choices.items()}}


def hydrate_choice(value, handles, session):
    key = 'value_request_id' if 'value_request_id' in value else 'value_field_request_id'
    handle = 'source-value:' + str(value[key])
    if len(value) != 1 or handle not in handles:
        raise RecognitionFailure('V2_SOURCE_VALUE_REQUEST_NOT_OFFERED')
    candidate, role, mention = handles[handle]
    ref = session.bind(candidate, role, (session._request.message_id + ':' + mention,))
    proof_key = contract_digest(ref.model_dump(mode='json'))
    proof = session._bindings.get(proof_key) or session._archived_source_bindings[proof_key]
    return (m.EntityValueRef(ref=ref).model_dump(mode='json') if key == 'value_request_id'
            else proof.field_ref.model_dump(mode='json'))


async def source_filter_patch(planner, session, parse, draft, handles, base, now, *, deferred, prior, target):
    from .source_value_repairs import align_source_value_requests
    draft, repairs = align_source_value_requests(parse, draft, handles, base=base, target=target)
    if repairs:
        import logging
        logging.getLogger(__name__).info('V2 current source-value representation repaired',
            extra={'message_id': session._request.message_id, 'source_value_repairs': repairs})
    variants = await resolve_requests(session, parse, draft, handles, target, planner.model)
    patches, labels = {}, {}
    last_rejected = None
    for choices in variants:
        selected = choice_handles(handles, choices)
        try:
            patch, traces = planner._patch(session, parse, draft, selected, base, now,
                deferred=deferred, prior=prior, target=target)
            reduced = apply_task_patch(prior, patch, clear_barriers=target.clear_barriers if target else [])
            validate_source_value_fields(reduced.semantics, tuple(session._bindings.values()))
        except (ValueError, RecognitionFailure) as exc:
            # Current explicit target/positive-membership facts can eliminate a
            # candidate without asking the user or changing a field/operation.
            if str(exc) not in {'SOURCE_VALUE_FILTER_FIELD_MISMATCH', 'V2_FILTER_REMOVE_VALUE_NOT_PRESENT'}:
                raise
            last_rejected = exc
            continue
        fingerprint = semantic_fingerprint(reduced.semantics.filter_expression)
        patches[fingerprint] = (patch, traces, reduced)
        names = []
        for candidate, _, _ in choices.values():
            item = session._value_candidates[candidate]
            metadata = session._rows[item['attribute_id']].metadata
            names.append(f"{metadata.get('parent_name') or metadata['parent']}的{metadata['attr_name']}：{item['canonical_value']}")
        labels[fingerprint] = '；'.join(names)
    if not patches:
        raise last_rejected or RecognitionFailure('V2_SOURCE_VALUE_FILTER_UNRESOLVED')
    if len(patches) == 1:
        patch, traces, _ = next(iter(patches.values()))
        return patch, traces, [], {}
    nonfilters = []
    options = []
    for fingerprint, (patch, traces, reduced) in patches.items():
        data = patch.model_dump(mode='json')
        for group in ('sets','adds','replacements','removes','clears','inherit_requests'):
            data[group] = [op for op in data[group] if op['slot_path'] != 'filter_expression']
        nonfilters.append(TaskPatch.model_validate(data))
        options.append(m.ClarificationOption(option_id='option:' + fingerprint[:24],
            display_label=labels[fingerprint], filter_choice=m.FilterChoice(expression=reduced.semantics.filter_expression),
            evidence=['VERIFIED_SOURCE_VALUE_COLLISION','CURRENT_FILTER_OPERATION_PRESERVED']))
    if len({semantic_fingerprint(p) for p in nonfilters}) != 1:
        raise RecognitionFailure('V2_SOURCE_VALUE_AFFECTS_NONFILTER_SLOT')
    if len({o.display_label for o in options}) != len(options):
        raise RecognitionFailure('V2_SOURCE_VALUE_OPTION_LABELS_NOT_DISTINCT')
    identifier = 'blocker:' + contract_digest([parse.turn_id, 'source_value_filter', sorted(patches)])[:24]
    blocker = PendingBlocker(blocker_id=identifier, plan_path='filter_expression', expected_answer_type='OPTION_ID',
        candidate_ids=[o.option_id for o in options], information_gain=log2(len(options)), options=options)
    return nonfilters[0], [], [blocker], {identifier: 'REPLACE' if prior.filter_expression else 'SET'}
