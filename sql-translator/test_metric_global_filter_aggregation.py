"""Published aggregate conditions run after grouping, including net refunds."""
import json
import sqlite3
import pytest
from test_metric_global_filter_normalization import translator as basic_translator


def fixture(condition='quantity > 0',kind='include'):
    compiler=basic_translator([])
    compiler.loader.entities['ent_order']['attributes'] += [
        {'field_mapping':'order_info.quantity'}]
    compiler.loader.metrics={name:{'metric_code':name,'metric_name':name,'metric_level':level,
        'source_dependency':{'bind_entity':['ent_order']},'params':{},
        'calculation_rule':{'calc_formula':formula,'depend_metrics':dependencies,
            'global_filters':[{'condition':condition,'filter_type':kind}] if name=='price' else []}}
        for name,formula,level,dependencies in [
            ('quantity','quantity=SUM(order_info.quantity)','atomic',[]),
            ('amount','amount=SUM(order_info.pay_amount)','atomic',[]),
            ('price','price=amount / quantity','复合指标',['quantity','amount'])]}
    ast={'version':'2.0','intent':'query','subject':{'entity':'ent_order'},
        'metrics':[{'name':'price','alias':'price'}],'dimensions':[{'name':'order_info.order_id','alias':'id'}],
        'filters':[],'having':[],'limit':100}
    return compiler,ast


def execute(sql):
    with sqlite3.connect(':memory:') as db:
        db.executescript('''CREATE TABLE order_info (order_id TEXT,pay_amount REAL,quantity REAL,status TEXT);
            INSERT INTO order_info VALUES ('A',200,2,'valid'),('A',-100,-1,'valid'),
                ('B',900,3,'valid'),('C',50,0,'valid'),('D',-200,-2,'valid');''')
        return db.execute(sql).fetchall()


@pytest.mark.parametrize('condition,kind',[('quantity > 0','include'),('quantity <= 0','exclude'),
    ('SUM(order_info.quantity) > 0','include')])
def test_aggregate_catalog_predicate_keeps_order_net_quantity(condition,kind):
    compiler,ast=fixture(condition,kind)
    sql=compiler.translate(json.dumps(ast),'130')
    assert 'HAVING' in sql and 'WHERE' not in sql
    assert sorted(execute(sql))==[('A',100.0),('B',300.0)]


def test_scalar_aggregate_condition_does_not_discard_refund_rows():
    compiler,ast=fixture();ast['dimensions']=[]
    assert execute(compiler.translate(json.dumps(ast),'130'))==[(425.0,)]


def test_row_filter_and_aggregate_sample_rule_remain_separate():
    compiler,ast=fixture()
    compiler.loader.metrics['price']['calculation_rule']['global_filters'].append(
        {'condition':"order_info.status = 'valid'",'filter_type':'include'})
    sql=compiler.translate(json.dumps(ast),'130')
    assert "WHERE order_info.status = 'valid'" in sql and 'HAVING' in sql
    assert sorted(execute(sql))==[('A',100.0),('B',300.0)]


@pytest.mark.parametrize('condition',["order_info.status = 'quantity > 0'",'order_info.quantity > 0'])
def test_quoted_values_and_physical_fields_are_not_metric_references(condition):
    compiler,ast=fixture(condition)
    sql=compiler.translate(json.dumps(ast),'130')
    assert condition in sql and 'HAVING' not in sql


def test_selected_composite_reference_is_expanded_with_formula_precedence():
    compiler,ast=fixture('price > 150')
    assert execute(compiler.translate(json.dumps(ast),'130'))==[('B',300.0)]


def test_aggregate_condition_tables_participate_in_existing_join_assembly():
    from test_metric_global_filter_joins import translator,dealer_subject_ast
    compiler=translator();ast=dealer_subject_ast()
    compiler.loader.metrics['cooperating_hospital_count']['calculation_rule']['global_filters']=[
        {'condition':'SUM(sales_order.quantity) > 0','filter_type':'include'}]
    sql=compiler.translate(json.dumps(ast),'81')
    assert 'JOIN sales_order ' in sql and 'HAVING (SUM(sales_order.quantity) > 0)' in sql
