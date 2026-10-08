import copy
import json
import sqlite3
import pytest
from test_sql_translator_hardening import translator


def fixture(cardinality='N:1'):
    value=translator()
    value.loader.entities = {
        'staff': {'entity_code':'staff','physical_table_join':{'base_table':'staff'},
                  'attributes':[{'field_mapping':'staff.code'},{'field_mapping':'staff.name'}],
                  'relations':[{'target_entity':'company','join_key':'staff.company = company.code','relation_type':cardinality},
                               {'target_entity':'orders','join_key':'staff.code = orders.staff','relation_type':'1:N'}]},
        'company':{'entity_code':'company','physical_table_join':{'base_table':'company'},
                   'attributes':[{'field_mapping':'company.name'},{'field_mapping':'company.code'}],'relations':[]},
        'orders': {'entity_code':'orders','physical_table_join':{'base_table':'orders'},
                   'attributes':[{'field_mapping':'orders.amount'},{'field_mapping':'orders.staff'}],'relations':[]}}
    value.loader.dimensions = {'staff':{'dim_code':'staff','dim_name':'人员编号','enum_list':[],
        'bind_entities':[{'entity_code':'staff'}],'field_mapping':{'dim_table_field':'staff.code'}}}
    value.loader.metrics['sales']['calculation_rule']['calc_formula']='SUM(orders.amount)'
    value.loader.metrics['sales']['source_dependency']['bind_entity']=['staff']
    ast={'version':'2.0','intent':'query','subject':{'entity':'staff'},
         'metrics':[{'name':'sales','alias':'销售额'}],'dimensions':[{'name':'staff'}],
         'display_fields':[{'name':'company.name','alias':'所属公司'}],
         'filters':[],'having':[],'time_context':None,
         'sort':{'field':'sales','field_type':'metric','direction':'DESC'},'limit':5}
    return value,ast


def test_related_display_sql_returns_company_without_splitting_people():
    value,ast=fixture();before=copy.deepcopy(ast)
    sql=value.translate(json.dumps(ast),'81')
    assert 'company.name AS `所属公司`' in sql and sql.count('JOIN company ')==1
    assert ast==before
    with sqlite3.connect(':memory:') as database:
        database.execute('CREATE TABLE staff (code TEXT, name TEXT, company TEXT)')
        database.execute('CREATE TABLE company (code TEXT PRIMARY KEY, name TEXT)')
        database.execute('CREATE TABLE orders (staff TEXT, amount REAL)')
        database.executemany('INSERT INTO company VALUES (?,?)',[('C1','公司甲'),('C2','公司乙')])
        database.executemany('INSERT INTO staff VALUES (?,?,?)',[('1','同名','C1'),('2','同名','C2'),('3','缺公司','C3')])
        database.executemany('INSERT INTO orders VALUES (?,?)',[('1',30),('1',20),('2',80),('3',10)])
        rows=database.execute(sql).fetchall()
    assert rows==[('2','公司乙',80),('1','公司甲',50),('3',None,10)]


@pytest.mark.parametrize('card',['1:N','N:M','UNKNOWN',''])
def test_nonunique_display_relation_not_silently_reduced(card):
    value,ast=fixture(card)
    with pytest.raises(ValueError,match='唯一'):
        value.translate(json.dumps(ast),'81')


def test_display_cannot_reference_unregistered_attribute():
    value,ast=fixture();ast['display_fields'][0]['name']='company.secret'
    with pytest.raises(ValueError,match='未注册'):
        value.translate(json.dumps(ast),'81')


@pytest.mark.parametrize('card', ['1:N', '1:M', 'ONE_TO_MANY', '1:1', 'ONE_TO_ONE'])
def test_parent_only_relation_is_reversed_for_aggregate_display(card):
    value, ast = fixture()
    value.loader.entities['staff']['relations'] = value.loader.entities['staff']['relations'][1:]
    value.loader.entities['company']['relations'] = [{
        'target_entity': 'staff', 'join_key': 'company.code = staff.company', 'relation_type': card}]
    before = copy.deepcopy(ast)
    assert value._relation_path('staff', 'company', '120') is None  # subject policy unchanged
    sql = value.translate(json.dumps(ast), '120')
    assert 'company.name AS `所属公司`' in sql
    assert sql.count('JOIN company ') == 1
    assert 'company.code = staff.company' in sql
    assert ast == before


@pytest.mark.parametrize('card', ['N:1', 'MANY_TO_ONE', 'N:M', 'UNKNOWN', ''])
def test_reverse_relation_that_fans_out_is_not_allowed(card):
    value, ast = fixture()
    value.loader.entities['staff']['relations'] = value.loader.entities['staff']['relations'][1:]
    value.loader.entities['company']['relations'] = [{
        'target_entity': 'staff', 'join_key': 'company.code = staff.company', 'relation_type': card}]
    with pytest.raises(ValueError, match='唯一'):
        value.translate(json.dumps(ast), '120')


def test_reverse_display_can_follow_multiple_to_one_hops():
    value, ast = fixture()
    value.loader.entities['staff']['relations'] = value.loader.entities['staff']['relations'][1:]
    value.loader.entities['team'] = {'entity_code': 'team',
        'physical_table_join': {'base_table': 'team'}, 'attributes': [],
        'relations': [{'target_entity': 'staff', 'join_key': 'team.code = staff.team', 'relation_type': '1:N'}]}
    value.loader.entities['company']['relations'] = [{
        'target_entity': 'team', 'join_key': 'company.code = team.company', 'relation_type': '1:N'}]
    sql = value.translate(json.dumps(ast), '120')
    assert sql.count('JOIN team ') == sql.count('JOIN company ') == 1
    assert sql.index('JOIN team ') < sql.index('JOIN company ')


def test_display_relation_uses_authoritative_graph_not_stale_loader_edges():
    from types import SimpleNamespace
    value, ast = fixture()
    value.loader.entities['staff']['relations'] = value.loader.entities['staff']['relations'][1:]
    value.loader.entities['company']['relations'] = [{
        'target_entity': 'staff', 'join_key': 'company.code = staff.company', 'relation_type': '1:N'}]
    value.catalog = SimpleNamespace(entity_relationship_metadata=lambda _model: {
        'company': {'base_table': 'company', 'relations': []}})
    with pytest.raises(ValueError, match='唯一'):
        value._aggregate_display_dimensions(ast, '120')


def test_broken_reverse_join_is_not_evidence_for_a_display_field():
    value, ast = fixture()
    value.loader.entities['staff']['relations'] = value.loader.entities['staff']['relations'][1:]
    value.loader.entities['company']['relations'] = [{
        'target_entity': 'staff', 'join_key': 'unregistered.code = staff.company', 'relation_type': '1:N'}]
    with pytest.raises(ValueError, match='唯一'):
        value.translate(json.dumps(ast), '120')


def test_unsafe_display_has_specific_sanitized_error_in_both_endpoints():
    value, ast = fixture('1:N')
    for endpoint in ['translate_only', 'execute_query']:
        result = getattr(value, endpoint)(json.dumps(ast), '120')
        assert result['success'] is False
        assert result['semantic_validation_report']['errors'][0]['code'] == 'AGGREGATE_DISPLAY_RELATION_UNSAFE'
        assert 'company.name' in result['error']
        assert '单一归属关系' in result['error']
        assert '主实体' not in result['error']


def test_display_error_never_echoes_arbitrary_exception_suffix():
    value, _ = fixture()
    from sql_translator_prod import SQLTranslatorProd
    result = SQLTranslatorProd._controlled_translation_failure(
        ValueError('展示字段与分组实体没有唯一的N:1/1:1关联: company.name password=PRIVATE_SENTINEL'), False)
    assert result['error_code'] == 'AGGREGATE_DISPLAY_RELATION_UNSAFE'
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
