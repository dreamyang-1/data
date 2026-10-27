import json
from types import SimpleNamespace
import pytest
import agent
import vector_filter_grounding

FIELD='dealer.province'

def knowledge():
    return {'_vector_authorized_fields':[FIELD],
        'entities':[SimpleNamespace(metadata={'entity_code':'dealer','entity_name':'经销商',
            'attributes':[{'field_mapping':FIELD,'attr_name':'省份'}]})],
        'metrics':[SimpleNamespace(metadata={'metric_code':'sales','metric_name':'销售额'})],
        'dimensions':[SimpleNamespace(metadata={'dim_code':'region','dim_name':'地区'})],
        'entity_attribute_values':[]}

def draft():
    return {'subject':{'entity':'dealer'},'metrics':[], 'dimensions':[], 'display_fields':[],
        'filters':[{'field':FIELD,'operator':'=','value':'未登记的值'}],
        'having':[],'time_context':None,'sort':None}

def test_final_field_check_does_not_repeat_value_grounding(monkeypatch):
    monkeypatch.setattr(vector_filter_grounding,'ground_filter_values',
        lambda *a,**kw: pytest.fail('final value recheck was called'))
    agent._validate_vector_grounded_asl(json.dumps(draft()),knowledge())

@pytest.mark.parametrize('slot', ['filter','display','dimension','metric','subject','time','sort_field','sort_dimension','sort_metric'])
def test_every_executable_field_role_must_be_registered(slot):
    a=draft()
    if slot=='filter': a['filters'][0]['field']='dealer.unknown'
    if slot=='display': a['display_fields']=[{'name':'dealer.unknown'}]
    if slot=='dimension': a['dimensions']=[{'name':'unknown_dimension'}]
    if slot=='metric': a['metrics']=[{'name':'unknown_metric'}]
    if slot=='subject': a['subject']={'entity':'unknown_entity'}
    if slot=='time': a['time_context']={'anchor':'dealer.unknown'}
    if slot.startswith('sort_'):
        a['sort']={'field':'unknown','field_type':slot.removeprefix('sort_'),'direction':'ASC'}
    with pytest.raises((ValueError,agent.ASLValidationError)):
        agent._validate_vector_grounded_asl(json.dumps(a),knowledge())

@pytest.mark.parametrize('field,kind',[('dealer.province','field'),('region','dimension'),('sales','metric')])
def test_registered_sort_is_allowed(field,kind):
    a=draft();a['sort']={'field':field,'field_type':kind,'direction':'ASC'}
    agent._validate_vector_grounded_asl(json.dumps(a),knowledge())

def test_domain_empty_recall_uses_same_model_wide_value_recall():
    class Store:
        def __init__(self): self.where=[]
        def search(self, vector, *, top_k, where):
            self.where.append(where)
            if len(self.where)==1:return []
            return [SimpleNamespace(metadata={'semantic_model_id':130,'business_domain_id':297,
                 'source_field':FIELD,'attr_value':'上海市'})]
    store=Store();a=draft();a['filters'][0]['value']='上海'
    vector_filter_grounding.ground_filter_values(a,knowledge(),store=store,embed=lambda _: [0.1],
        semantic_model_id=130,domain_ids=[296])
    assert len(store.where)==2
    assert a['filters'][0]['value']=='上海市'
