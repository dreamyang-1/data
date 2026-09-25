from copy import deepcopy
from types import SimpleNamespace
import sqlite3
import pytest
from related_scope_sql import compile_related_filters


def fixture():
    edges=['sales.product_id = product.id','product.id = bridge.product_id','bridge.tag_id = tag.id']
    entities={name:{'physical_table_join':{'base_table':name},'relations':[{'join_key':e} for e in edges]} for name in ['sales','product','bridge']}
    allowed={'sales.product_id','product.id','product.brand','bridge.product_id','bridge.tag_id'}
    translator=SimpleNamespace(_get_entity=lambda name,model:entities.get(name),
        catalog=SimpleNamespace(attribute_metadata=lambda model,fields:{f:[{}] for f in fields if f in allowed}),
        _build_from_clause=lambda *a:'FROM product',_detect_additional_joins=lambda *a:[],
        _extract_tables_from_join=lambda *a:set(),_build_filter_clause=lambda *a:"WHERE product.brand = 'Target'")
    relation=dict(kind='shared_attribute',outer_entity='sales',outer_field='sales.product_id',
                  target_entity='product',target_field='product.id',bridge_entity='bridge',
                  bridge_key='bridge.product_id',shared_field='bridge.tag_id',
                  target_filters=[{'field':'product.brand','operator':'=','value':'Target'}])
    return translator,{'subject':{'entity':'sales'},'related_filters':[relation]}


def test_exists_counts_each_sale_once_even_with_duplicate_multiple_tags():
    t,ast=fixture(); condition=compile_related_filters(t,ast,'106')[0]
    c=sqlite3.connect(':memory:')
    c.executescript('CREATE TABLE sales(product_id TEXT, amount INT); CREATE TABLE product(id TEXT,brand TEXT); CREATE TABLE bridge(product_id TEXT,tag_id TEXT);')
    c.executemany('INSERT INTO product VALUES(?,?)',[('target','Target'),('candidate','Other'),('unrelated','Other')])
    c.executemany('INSERT INTO bridge VALUES(?,?)',[('target','x'),('target','y'),('candidate','x'),('candidate','x'),('candidate','y'),('unrelated','z')])
    c.executemany('INSERT INTO sales VALUES(?,?)',[('candidate',100),('candidate',200),('unrelated',500)])
    assert c.execute('SELECT SUM(amount) FROM sales WHERE '+condition).fetchone()[0]==300
    assert 'LIMIT' not in condition
    c.close()


@pytest.mark.parametrize('change',[{'shared_field':'bridge.no_such_field'}, {'outer_field':'sales.product_id;DROP'}, {'bridge_entity':'unknown'}, {'target_field':'product.unregistered'}, {'outer_entity':'product'}])
def test_scope_and_identifiers_remain_enforced(change):
    t,ast=fixture(); ast['related_filters'][0].update(change)
    with pytest.raises(ValueError): compile_related_filters(t,ast,'106')


def test_plain_queries_unchanged():
    t,ast=fixture(); ast.pop('related_filters')
    assert compile_related_filters(t,ast,'106')==[]


def test_reverse_only_publication_from_shared_entity_is_supported():
    t,ast=fixture()
    old=t._get_entity
    def entity(name,model):
        row=deepcopy(old(name,model))
        if row:
            row['relations']=[r for r in row['relations'] if 'tag.id' not in r['join_key']]
        return row
    t._get_entity=entity
    t.catalog.entity_relationship_metadata=lambda model:{'tag':{'relations':[{'join_key':'tag.id = bridge.tag_id'}]}}
    assert 'EXISTS' in compile_related_filters(t,ast,'106')[0]
