from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app.semantic_v2.recognition import semantic_task_schema
from test_v2_raw_turn_recognition import catalog, planner, turns, parse, binding, edit, metric_step


def step(kind='object'):
    text='销售额和订单笔数' if kind=='multi' else '销售额'
    specs=[('销售额','MEASURE','metrics','SET')]
    if kind=='multi': specs.append(('订单笔数','MEASURE','metrics','SET'))
    def draft(c):
        one=binding(c,'销售额','MEASURE')
        value=[one,binding(c,'订单笔数','MEASURE','m1')] if kind=='multi' else [one] if kind=='list' else one
        return dict(payload_type='SCALAR_AGGREGATE',edits=[edit('metrics',value,ids=('m0','m1') if kind=='multi' else ('m0',))])
    return text,parse(text,specs),draft


@pytest.mark.asyncio
async def test_new_single_member_list_keeps_native_semantics(catalog):
    current=step('list');engine,transport=planner(catalog,[current])
    result=(await turns(engine,[current]))[0]
    assert len(transport.calls)==2
    assert [r['canonical_code'] for r in result.plan['logical_plan']['payload']['measures']]==['amount']
    assert result.resolution['task_patch']['sets'][0]['reason_code']=='CURRENT_TURN_EVIDENCE'


@pytest.mark.asyncio
async def test_schema_fix_does_not_relax_native_scalar_set_rejection(catalog):
    current=step('object')
    with pytest.raises(ValueError,match='V2_CONTRACT_VALIDATION_FAILURE'):
        await turns(planner(catalog,[current])[0],[current])


@pytest.mark.asyncio
async def test_multi_element_list_preserves_all_members(catalog):
    current=step('multi');result=(await turns(planner(catalog,[current])[0],[current]))[0]
    assert [r['canonical_code'] for r in result.plan['logical_plan']['payload']['measures']]==['amount','orders']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['null','empty_object','unknown_field','string','nested','forged_handle','scope'])
async def test_invalid_initial_object_never_becomes_a_valid_collection(catalog,fault):
    text,parsed,good=step()
    def bad(c):
        value=good(c);operand=value['edits'][0]['value']
        replacement={'null':None,'empty_object':{},'string':'amount','nested':{'binding_handle':operand},
            'forged_handle':{'binding_handle':'forged'},'unknown_field':{**operand,'unexpected':True},
            'scope':{**operand,'semantic_model_id':'82'}}[fault]
        value['edits'][0]['value']=replacement;return value
    # A nested handle is already rejected by the existing mention-repair
    # boundary (TypeError); do not hide that preexisting diagnostic limitation.
    error = TypeError if fault == 'nested' else ValueError
    with pytest.raises(error):await turns(planner(catalog,[(text,parsed,bad)])[0],[(text,parsed,bad)])


@pytest.mark.asyncio
async def test_wrong_role_in_valid_array_does_not_pass(catalog):
    text='城市'
    current=(text,parse(text,[(text,'GROUP_BY','metrics','SET')]),lambda c:dict(payload_type='SCALAR_AGGREGATE',
        edits=[edit('metrics',[binding(c,text,'GROUP_BY')])]))
    with pytest.raises(ValueError):await turns(planner(catalog,[current])[0],[current])


@pytest.mark.asyncio
async def test_nonempty_task_set_does_not_gain_scalar_compatibility(catalog):
    first=metric_step('销售额');text,parsed,draft=step()
    parsed['reference_signals']=['ELLIPSIS']
    steps=[first,(text,parsed,draft)]
    with pytest.raises(ValueError):await turns(planner(catalog,steps)[0],steps)


@pytest.mark.parametrize('operation,slot', [('REPLACE','metrics'),('ADD','metrics'),
    ('REMOVE','metrics'),('CLEAR','metrics'),('SET','subject'),('SET','time_spec')])
def test_joint_schema_does_not_change_other_operation_or_slot_contracts(operation,slot):
    schema=semantic_task_schema(SimpleNamespace(reference_signals=[],topic_shift_signals=[]),{})
    for rule in schema['$defs']['SlotEditDraft']['allOf']:
        if rule.get('then',{}).get('properties',{}).get('value',{}).get('type')=='array':
            assert not Draft202012Validator(rule['if']).is_valid(dict(slot_path=slot,operation=operation))


@pytest.mark.parametrize('slot',['metrics','dimensions'])
@pytest.mark.parametrize('operand,valid',[([{'binding_handle':'x'}],True),
    ([{'binding_handle':'x'},{'binding_handle':'y'}],True),({'binding_handle':'x'},False),
    (None,False),('x',False),([{'binding_handle':'x','other':True}],False)])
def test_generation_joint_set_contract_requires_list(slot,operand,valid):
    schema=semantic_task_schema(SimpleNamespace(reference_signals=[],topic_shift_signals=[]),{})
    Draft202012Validator.check_schema(schema)
    value=dict(payload_type='SCALAR_AGGREGATE',edits=[edit(slot,operand)])
    assert Draft202012Validator(schema).is_valid(value)==valid
