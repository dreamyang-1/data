"""SQL projects declared fields only; catalog labels are not extra columns."""
from test_sql_translator_hardening import translator

def test_registered_dimension_does_not_add_undeclared_display():
    value=translator()
    value.loader.entities['city']={'entity_code':'city','physical_table_join':{'base_table':'city'},
        'attributes':[{'attr_code':'city_id','field_mapping':'city.id'},
                      {'attr_code':'city_name','field_mapping':'city.name','attr_name':'城市名称'}]}
    value.loader.dimensions['city']={'dim_code':'city','dim_name':'城市编码',
        'field_mapping':{'fact_table_field':'city.id'},'bind_entities':[{'entity_code':'city'}]}
    sql=value._build_select_clause([], [{'name':'city'}], 'city', '6')
    assert 'city.id' in sql and 'city.name' not in sql
    assert value._build_order_by_clause({'field':'city','field_type':'dimension'}, [], [{'name':'city'}], '6') == 'ORDER BY `城市编码` ASC'
