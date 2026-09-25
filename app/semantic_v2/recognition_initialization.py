"""Representation adapters for current explicit assignments on an empty new task.

These adapters cannot choose a task, field, entity value, or inherited value.
Existing evidence/binding/operation validators consume their output unchanged.
"""
from types import SimpleNamespace
from copy import deepcopy

from pydantic import TypeAdapter

from . import models as m
from .explicit_time import metric_anchor,normalize_initial_assignment
from .recognition_client import RecognitionFailure
from .structured_edits import FilterEditDraft, filter_targets


def collection_set_schema(schema, task_schema):
    """Export the existing metric/dimension SET array contract, not JsonValue."""
    handle = deepcopy(task_schema['$defs']['BoundSemanticRef']['anyOf'][0])
    rules = []
    for slot in ('metrics', 'dimensions'):
        value = deepcopy(task_schema['properties'][slot])
        value['items'] = handle
        rules.append({'if': {'properties': {'slot_path': {'const': slot}, 'operation': {'const': 'SET'}}},
            'then': {'required': ['value'], 'properties': {'value': value}}})
    schema['$defs']['SlotEditDraft'].setdefault('allOf', []).extend(rules)
    return schema


def align_filter_deletions(parse,draft,*,base,target):
    """Align equivalent whole-subtree deletions with the declared operation.

Native filter_edits deletes the same scoped subtree for REMOVE/CLEAR with no
value. Member REMOVE, collection CLEAR, other targets and other edits are never
interchangeable. The native lowering still creates the original clear barrier.
"""
    if target is None or base!=target.active_version:
        return draft,[]
    targets=filter_targets(target);edits=[];traces=[]
    for edit in draft.filter_edits:
        declared={marker.operation_hint for marker in parse.operation_markers
            if marker.slot_name=='filter_expression' and marker.mention_id in edit.evidence_mention_ids}
        if (edit.operation in {'REMOVE','CLEAR'} and edit.value is None and edit.target_handle in targets
                and len(declared)==1 and declared<={'REMOVE','CLEAR'} and edit.operation not in declared):
            operation=next(iter(declared))
            # Require the same declaration on every affected current mention.
            matched={marker.mention_id for marker in parse.operation_markers
                if marker.slot_name=='filter_expression' and marker.operation_hint==operation}
            if set(edit.evidence_mention_ids)<=matched:
                traces.append(dict(slot='filter_expression',source_operation=edit.operation,operation=operation,
                    reason='CURRENT_SCOPED_SUBTREE_DELETION_EQUIVALENCE'))
                edit=edit.model_copy(update={'operation':operation})
        edits.append(edit)
    return draft.model_copy(update={'filter_edits':edits}),traces


def initial_assignments(parse,draft,*,base,target,prior,edit_model,handles):
    """An unmarked ADD into a proven empty slot is an initial assignment.

Only an entirely new empty task qualifies. Any declared operation in the slot,
mixed edits, missing current evidence, or historical target keeps native guards.
Multiple initial additions become ONE SET; separate SETs would drop members.
"""
    if target is not None or base!=0 or prior!=m.TaskSemanticState():
        return draft,[]
    marked={marker.slot_name for marker in parse.operation_markers}
    explicit={slot:set(ids) for slot,ids in parse.explicit_slot_mentions.items()}
    declared={mention.mention_id for mention in parse.mentions if mention.explicit}
    def own_handles(value,evidence):
        # Merging edit evidence must not make a foreign mention legal. Check
        # each original operand BEFORE taking the union for the one SET.
        if isinstance(value,dict):
            handle=value.get('binding_handle')
            if 'value_request_id' in value or 'value_field_request_id' in value:
                handle='source-value:'+str(value.get('value_request_id',value.get('value_field_request_id')))
            if handle is not None:
                return len(value)==1 and handle in handles and handles[handle][2] in evidence
            return all(own_handles(v,evidence) for v in value.values())
        return all(own_handles(v,evidence) for v in value) if isinstance(value,list) else True
    def justified(slot,edits):
        return (slot not in marked and all(edit.evidence_mention_ids
            and set(edit.evidence_mention_ids)<=explicit.get(slot,set()) & declared
            and own_handles(edit.value,set(edit.evidence_mention_ids)) for edit in edits))
    edits=list(draft.edits);filters=list(draft.filter_edits);traces=[]
    # Some structured-output providers validate the object shape but do not
    # enforce the exported conditional which reserves whole-filter ADD for the
    # structured channel.  Relocate only the exact, already-declared operation
    # on a completely empty new task.  The predicate, source-value handles,
    # current mention evidence and operation are unchanged; the native initial
    # assignment below remains the sole ADD-to-empty lowering authority.
    whole_adds=[edit for edit in edits
        if edit.slot_path=='filter_expression' and edit.operation=='ADD']
    def declared_whole_add(edit):
        evidence=set(edit.evidence_mention_ids)
        return (evidence
            and evidence <= explicit.get('filter_expression',set()) & declared
            and own_handles(edit.value,evidence)
            and evidence <= {marker.mention_id for marker in parse.operation_markers
                if marker.slot_name=='filter_expression' and marker.operation_hint=='ADD'})
    if (whole_adds and not filters
            and all(edit.value is not None
                and isinstance(edit.value,dict)
                and edit.value.get('node_type') in {'PREDICATE','ALIASED_PREDICATE'}
                and edit.value.get('source')=='USER_EXPLICIT'
                and edit.value.get('scope')=='CURRENT_TASK'
                for edit in whole_adds)
            and all(declared_whole_add(edit) for edit in whole_adds)):
        filters=[FilterEditDraft(operation='ADD',target_handle=None,
            evidence_mention_ids=edit.evidence_mention_ids,value=edit.value)
            for edit in whole_adds]
        edits=[edit for edit in edits if edit not in whole_adds]
        traces.append(dict(slot='filter_expression',source_operation='ADD',operation='ADD',
            reason='EMPTY_NEW_TASK_STRUCTURED_FILTER_CHANNEL',operand_count=len(filters)))
    for slot in ('metrics','dimensions'):
        selected=[edit for edit in edits if edit.slot_path==slot]
        if not selected or any(edit.operation!='ADD' for edit in selected) or not justified(slot,selected):
            continue
        values=[]
        for edit in selected:
            values.extend(edit.value if isinstance(edit.value,list) else [edit.value])
        replacement=edit_model(slot_path=slot,operation='SET',value=values,
            evidence_mention_ids=sorted({i for edit in selected for i in edit.evidence_mention_ids}))
        position=edits.index(selected[0]);edits=[edit for edit in edits if edit.slot_path!=slot]
        edits.insert(position,replacement);traces.append(dict(slot=slot,source_operation='ADD',operation='SET',
            reason='EMPTY_NEW_TASK_CURRENT_EXPLICIT_ASSIGNMENT',operand_count=len(values)))
    if (filters and not any(edit.slot_path=='filter_expression' for edit in edits)
            and all(edit.operation=='ADD' and edit.target_handle is None and edit.value is not None for edit in filters)
            and all(isinstance(edit.value,dict) and edit.value.get('source')=='USER_EXPLICIT'
                and edit.value.get('scope')=='CURRENT_TASK'
                and edit.value.get('node_type') in {'PREDICATE','ALIASED_PREDICATE'} for edit in filters)
            and justified('filter_expression',filters)):
        value=filters[0].value if len(filters)==1 else dict(node_type='BOOLEAN_GROUP',operator='AND',children=[e.value for e in filters])
        edits.append(edit_model(slot_path='filter_expression',operation='SET',value=value,
            evidence_mention_ids=sorted({i for e in filters for i in e.evidence_mention_ids})))
        traces.append(dict(slot='filter_expression',source_operation='ADD',operation='SET',
            reason='EMPTY_NEW_TASK_CURRENT_EXPLICIT_ASSIGNMENT',operand_count=len(filters)))
        filters=[]
    return draft.model_copy(update={'edits':edits,'filter_edits':filters}),traces


def initial_time_assignment(session,parse,edits,metrics,now,hydrate):
    """Initialize only SET components backed by current evidence and Catalog.

A range alone receives the governed metric anchor, never a guessed model field.
Explicit fields, grain, policy, malformed values and unsupported edits remain
subject to the existing time and binding contracts.
"""
    by_component={edit.component:edit for edit in edits}
    if (len(by_component)!=len(edits) or 'RANGE' not in by_component
            or any(edit.operation!='SET' for edit in edits)):
        raise RecognitionFailure('V2_TEMPORAL_BASE_REQUIRED')
    hydrated={}
    for component,edit in by_component.items():
        expected={'RANGE':m.TimeRange,'GRAIN':m.TimeGrain,'ANCHOR':m.BoundSemanticRef}[component]
        hydrated[component]=TypeAdapter(expected).validate_python(hydrate(edit.value,edit.evidence_mention_ids))
        role={'RANGE':'TIME_RANGE','GRAIN':'TIME_GRAIN','ANCHOR':'TIME_FIELD'}[component]
        selected=[mention for mention in parse.mentions if mention.mention_id in edit.evidence_mention_ids]
        if not selected or any(not mention.explicit or role not in mention.candidate_roles for mention in selected):
            raise RecognitionFailure('V2_TEMPORAL_EXPRESSION_EVIDENCE_REQUIRED')
    evidence=sorted({i for edit in edits for i in edit.evidence_mention_ids})
    if 'ANCHOR' in hydrated:
        anchor=hydrated['ANCHOR']
    else:
        if any('TIME_FIELD' in mention.candidate_roles for mention in parse.mentions):
            raise RecognitionFailure('V2_INITIAL_TIME_EXPLICIT_ANCHOR_REQUIRED')
        anchor=metric_anchor(session,metrics,tuple(session._request.message_id+':'+i for i in by_component['RANGE'].evidence_mention_ids))
    value=dict(anchor=anchor.model_dump(mode='json'),range=hydrated['RANGE'].model_dump(mode='json'),
        grain=hydrated.get('GRAIN','NONE'),source='USER_EXPLICIT',timezone='Asia/Shanghai',as_of=now.isoformat())
    actual,_=normalize_initial_assignment(session,parse,SimpleNamespace(evidence_mention_ids=evidence),value,metrics,now)
    return m.SlotOperation(operation_id='initial:time_spec',slot_path='time_spec',operation='SET',new_value=actual,
        source='CURRENT_EXPLICIT',reason_code='CURRENT_INITIAL_TIME_COMPONENTS',base_task_version=0,
        presence='PRESENT',evidence_mention_ids=evidence)


def source_field_schema(schema,candidates,*,parse=None,initial_range=False):
    """Source lookup accepts ATTRIBUTE fields, unlike general FILTER_FIELD refs.

Narrow generation to eligible handles; do not remove a selected invalid handle
or convert a dimension to an attribute at consumption time.
"""
    handles=sorted({c['binding_handle'] for c in candidates
        if c['role']=='FILTER_FIELD' and c['catalog_type']=='ATTRIBUTE'})
    field=schema['$defs']['SourceValueRequestDraft']['properties']['field_binding_handles']
    if handles:field['items']={'type':'string','enum':handles}
    else:field['maxItems']=0
    requests=schema['properties']['source_value_requests']
    mention_ids=sorted({mention.mention_id for mention in getattr(parse,'mentions',())
        if mention.explicit and 'FILTER_VALUE' in mention.candidate_roles})
    if mention_ids:
        schema['$defs']['SourceValueRequestDraft']['properties']['mention_id']={
            'type':'string','enum':mention_ids}
    else:
        requests['maxItems']=0
    if initial_range and not any(c['role']=='TIME_FIELD' for c in candidates):
        # A bare time range offers no anchor handle. Offer the supported initial
        # component representation instead of asking for an invented reference.
        slots=schema['$defs']['SlotEditDraft']['properties']['slot_path']['enum']
        schema['$defs']['SlotEditDraft']['properties']['slot_path']['enum']=[s for s in slots if s!='time_spec']
        schema['properties']['temporal_edits']['description']=(
            'Initial current time: supply RANGE/SET (and GRAIN/SET only if explicitly requested). '
            'The runtime verifies the metric Catalog time anchor; do not invent an anchor handle.')
    schema['$comment']='v2-semantic-initialization-generation-v3'
    return schema
