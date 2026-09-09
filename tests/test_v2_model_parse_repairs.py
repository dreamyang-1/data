import json
from pathlib import Path
import pytest
from app.semantic_v2.pipeline import CurrentTurnSemanticParse, CurrentTurnParser
from app.semantic_v2.recognition_repairs import repair_model_parse, repair_collection_handle_mentions
from app.semantic_v2.enums import SemanticRole


def parse(text, *, surface, start, end, turn='t', negated=False, negations=()):
    return CurrentTurnSemanticParse.model_validate({'mentions':[{'mention_id':'m','surface':surface,
        'normalized_surface':surface,'start_char':start,'end_char':end,'candidate_roles':['MEASURE'],
        'source_turn_id':turn,'negated':negated}],'negations':list(negations)})


def validate(text, value):
    return CurrentTurnParser.parse(text=text,turn_id='t',text_ref='t',parsed=value)


@pytest.mark.parametrize('text,surface,start,end,expected',[
    ('请告诉我销售总成本','销售总成本',3,8,(4,9)),
    ('结果中只返回销售员姓名','销售员姓名',7,12,(6,11)),
    ('📊销售额','销售额',2,5,(1,4)),
    ('查看A\u0301编码','A\u0301',1,3,(2,4)),
])
def test_exact_unique_surface_repairs_only_offsets(text,surface,start,end,expected):
    original=parse(text,surface=surface,start=start,end=end)
    before=original.model_dump()
    repaired,trace=repair_model_parse(original,text=text,turn_id='t')
    checked=validate(text,repaired)
    assert (checked.mentions[0].start_char,checked.mentions[0].end_char)==expected
    assert original.model_dump()==before
    assert trace[0]['reason_code']=='EXACT_UNIQUE_CURRENT_SURFACE_SPAN'
    assert surface not in json.dumps(trace,ensure_ascii=False)
    for key in before:
        if key!='mentions':assert repaired.model_dump()[key]==before[key]


@pytest.mark.parametrize('text,surface,start,end',[
    ('销售额和销售額','销售额',0,3), # Already valid; no normalization to another spelling.
    ('销售额与销售额','销售额',4,7), # Valid second occurrence stays second.
])
def test_valid_original_span_is_not_relocated(text,surface,start,end):
    value=parse(text,surface=surface,start=start,end=end)
    result,trace=repair_model_parse(value,text=text,turn_id='t')
    assert trace==[] and result.model_dump()==value.model_dump()
    validate(text,result)


@pytest.mark.parametrize('text,surface,start,end',[
    ('销售额与销售额','销售额',1,4),
    ('aaa','aa',2,4), # Overlapping occurrences are still ambiguous.
    ('销售额','销售量',0,3),
    ('销售額','销售额',0,3),
])
def test_ambiguous_absent_or_fuzzy_surface_stays_rejected(text,surface,start,end):
    value=parse(text,surface=surface,start=start,end=end)
    result,trace=repair_model_parse(value,text=text,turn_id='t')
    assert not trace
    with pytest.raises(ValueError):validate(text,result)


def test_foreign_turn_is_never_made_current_by_matching_text():
    value=parse('销售额',surface='销售额',start=1,end=4,turn='history')
    result,trace=repair_model_parse(value,text='销售额',turn_id='t')
    assert not trace and result.mentions[0].source_turn_id=='history'
    with pytest.raises(ValueError):validate('销售额',result)


def test_negation_surface_maps_only_to_already_declared_unique_negated_mention():
    text='不要订单笔数';value=parse(text,surface=text,start=0,end=6,negated=True,negations=['不要'])
    result,trace=repair_model_parse(value,text=text,turn_id='t')
    assert result.negations==['m'] and value.negations==['不要']
    assert result.mentions[0].negated and result.mentions[0].surface==text
    assert trace==[{'reason_code':'EXACT_TOKEN_IN_UNIQUE_DECLARED_NEGATED_MENTION','mention_id':'m','field':'negations[0]'}]
    validate(text,result)


@pytest.mark.parametrize('case',['not_negated','missing_token','repeated_token','nested_negated','unknown_operation_reference'])
def test_reference_repair_does_not_invent_or_discard_evidence(case):
    text='不要订单笔数' if case!='repeated_token' else '不要订单笔数，不要销售额'
    value=parse(text,surface=text,start=0,end=len(text),negated=case!='not_negated',
        negations=['missing' if case=='missing_token' else '不要'])
    if case=='nested_negated':
        other=value.mentions[0].model_copy(update={'mention_id':'m2','surface':'不要','normalized_surface':'不要','end_char':2})
        value.mentions.append(other)
    if case=='unknown_operation_reference':
        from app.semantic_v2.pipeline import OperationMarker
        value.operation_markers=[OperationMarker(mention_id='missing',operation_hint='REMOVE',slot_name='metrics')]
    result,_=repair_model_parse(value,text=text,turn_id='t')
    with pytest.raises(ValueError):validate(text,result)
    if case!='unknown_operation_reference':assert result.negations==value.negations


def test_unknown_temporal_and_coordination_refs_are_not_repaired_or_dropped():
    value=parse('本月销售额',surface='本月',start=0,end=2)
    value.temporal_expressions=['本月'];value.coordination_groups=[['missing']]
    result,_=repair_model_parse(value,text='本月销售额',turn_id='t')
    assert result.temporal_expressions==['本月'] and result.coordination_groups==[['missing']]
    with pytest.raises(ValueError):validate('本月销售额',result)


@pytest.mark.parametrize('surface,reference,role',[('按季度','季度','TIME_GRAIN'),('本月','本月','TIME_RANGE')])
def test_temporal_token_needs_existing_time_role_and_unique_span(surface,reference,role):
    value=parse(surface,surface=surface,start=0,end=len(surface))
    value.mentions[0].candidate_roles=[SemanticRole(role)];value.temporal_expressions=[reference]
    result,trace=repair_model_parse(value,text=surface,turn_id='t')
    assert result.temporal_expressions==['m']
    assert value.temporal_expressions==[reference]
    assert trace[0]['reason_code']=='EXACT_TOKEN_IN_UNIQUE_DECLARED_TEMPORAL_MENTION'
    validate(surface,result)


def test_repeated_time_token_cannot_select_an_occurrence():
    text='本月和本月';value=parse(text,surface='本月',start=0,end=2)
    value.mentions[0].candidate_roles=[SemanticRole.TIME_RANGE];value.temporal_expressions=['本月']
    result,trace=repair_model_parse(value,text=text,turn_id='t')
    assert not trace and result.temporal_expressions==['本月']
    with pytest.raises(ValueError):validate(text,result)


def test_generation_schema_closes_slots_without_changing_frozen_models():
    from app.semantic_v2.recognition import current_turn_schema,EDIT_SLOTS
    from app.semantic_v2.registries import SlotDefinitionRegistry
    before=CurrentTurnSemanticParse.model_json_schema()
    schema=current_turn_schema()
    keys=schema['properties']['explicit_slot_mentions']
    assert keys['additionalProperties'] is False and set(keys['properties'])==set(EDIT_SLOTS)
    assert set(schema['$defs']['OperationMarker']['properties']['slot_name']['enum'])==set(EDIT_SLOTS)
    assert 'limit' not in keys['properties']
    for name in keys['properties']:SlotDefinitionRegistry.get(name)
    assert CurrentTurnSemanticParse.model_json_schema()==before


def test_unknown_limit_slot_is_never_guessed_as_ranking_or_display():
    from app.semantic_v2.pipeline import OperationMarker
    text='前5条';value=parse(text,surface=text,start=0,end=3)
    value.explicit_slot_mentions={'limit':['m']}
    value.operation_markers=[OperationMarker(mention_id='m',operation_hint='SET',slot_name='limit')]
    result,trace=repair_model_parse(value,text=text,turn_id='t')
    assert not trace and result.explicit_slot_mentions=={'limit':['m']}
    with pytest.raises(ValueError,match='unknown task slot'):validate(text,result)


RECORDED=json.loads((Path(__file__).parent/'fixtures/v2_model_parse_repairs.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('row',RECORDED,ids=[r['case_id'] for r in RECORDED])
def test_actual_recorded_failure_replay_without_model_calls(row):
    parsed=CurrentTurnSemanticParse.model_validate(row['parsed'])
    with pytest.raises(ValueError):
        CurrentTurnParser.parse(text=row['text'],turn_id=row['case_id'],text_ref=row['case_id'],parsed=parsed)
    result,trace=repair_model_parse(parsed,text=row['text'],turn_id=row['case_id'])
    CurrentTurnParser.parse(text=row['text'],turn_id=row['case_id'],text_ref=row['case_id'],parsed=result)
    assert trace and parsed.model_dump(mode='json')==row['parsed']
    assert result.operation_markers==parsed.operation_markers
    assert result.query_shape_prediction==parsed.query_shape_prediction


def collection_fixture(*, slot='metrics', array=False):
    from app.semantic_v2.recognition import SemanticTaskDraft
    role='MEASURE' if slot=='metrics' else 'GROUP_BY'
    text='销售额销售数量' if slot=='metrics' else '省份城市'
    left,right=('销售额','销售数量') if slot=='metrics' else ('省份','城市')
    parsed=CurrentTurnSemanticParse.model_validate({'mentions':[
        dict(mention_id='m0',surface=left,normalized_surface=left,start_char=0,end_char=len(left),candidate_roles=[role],source_turn_id='t'),
        dict(mention_id='m1',surface=right,normalized_surface=right,start_char=len(left),end_char=len(text),candidate_roles=[role],source_turn_id='t')],
        'explicit_slot_mentions':{slot:['m1']}})
    value={'binding_handle':'old'}
    draft=SemanticTaskDraft.model_validate(dict(payload_type='INHERIT',edits=[dict(slot_path=slot,operation='ADD',
        evidence_mention_ids=['m1'],value=[value] if array else value)]))
    handles={'old':('same-catalog-identity',role,'m0'),'correct':('same-catalog-identity',role,'m1')}
    candidates=[dict(binding_handle=h,mention_id=mid,name=right,role=role,catalog_type='METRIC' if slot=='metrics' else 'DIMENSION')
        for h,mid in [('old','m0'),('correct','m1')]]
    return draft,parsed,handles,candidates


@pytest.mark.parametrize('slot',['metrics','dimensions'])
@pytest.mark.parametrize('array',[True,False])
def test_collection_handle_repair_keeps_identity_role_operation_and_evidence(slot,array):
    from copy import deepcopy
    draft,parsed,handles,candidates=collection_fixture(slot=slot,array=array)
    original=draft.model_dump(mode='json');original_parse=parsed.model_dump(mode='json');original_handles=deepcopy(handles)
    repaired,trace=repair_collection_handle_mentions(draft,parse=parsed,handles=handles,candidates=candidates)
    expected=deepcopy(original)
    value=expected['edits'][0]['value'][0] if array else expected['edits'][0]['value']
    value['binding_handle']='correct'
    assert repaired.model_dump(mode='json')==expected
    assert handles['old'][:2]==handles['correct'][:2]
    assert draft.model_dump(mode='json')==original and parsed.model_dump(mode='json')==original_parse and handles==original_handles
    assert len(trace)==1 and trace[0]['from_mention_id']=='m0' and trace[0]['to_mention_id']=='m1'
    assert '销售数量' not in json.dumps(trace,ensure_ascii=False) and '城市' not in json.dumps(trace,ensure_ascii=False)


@pytest.mark.parametrize('fault',['repeated_surface','catalog_collision','alias_only','outside_evidence','unresolved',
    'foreign_handle','wrong_role','authority_fields','missing_correct_handle','different_identity'])
def test_collection_handle_repair_does_not_guess_ambiguous_or_foreign_evidence(fault):
    from copy import deepcopy
    draft,parsed,handles,candidates=collection_fixture()
    if fault=='repeated_surface':parsed.mentions.append(parsed.mentions[1].model_copy(update={'mention_id':'m2'}))
    if fault=='catalog_collision':
        candidates.append({**candidates[1],'binding_handle':'collision'})
        handles['collision']=('different-identity','MEASURE','m1')
    if fault=='alias_only':
        for c in candidates:c['name']='另一名称';c['aliases']=['销售数量']
    if fault=='outside_evidence':draft.edits[0].evidence_mention_ids=['m2']
    if fault=='unresolved':draft.unresolved_mention_ids=['m1']
    if fault=='foreign_handle':draft.edits[0].value={'binding_handle':'not-offered'}
    if fault=='wrong_role':candidates[0]['role']='GROUP_BY'
    if fault=='authority_fields':draft.edits[0].value['semantic_model_id']=82
    if fault=='missing_correct_handle':handles.pop('correct')
    if fault=='different_identity':handles['correct']=('different-identity','MEASURE','m1')
    original=deepcopy(draft.model_dump(mode='json'))
    repaired,trace=repair_collection_handle_mentions(draft,parse=parsed,handles=handles,candidates=candidates)
    assert repaired.model_dump(mode='json')==original and not trace
