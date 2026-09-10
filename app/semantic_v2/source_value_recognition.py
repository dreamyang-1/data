"""Lower current mentions and offered fields to source-proved value choices."""
from itertools import product
from math import log2

from pydantic import ConfigDict, Field, model_validator

from . import models as m
from .authorized_contract import contract_digest, validate_source_value_fields, SourceValueBindingEvidence
from .recognition_client import RecognitionFailure
from .enums import CatalogType
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
