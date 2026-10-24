import pytest

from test_sql_translator_hardening import translator


def fixture():
    value = translator()
    value.loader.entities = {
        code: {'entity_code': code, 'physical_table_join': {'base_table': table},
               'attributes': [], 'relations': [], 'sub_table_mappings': []}
        for code, table in [('orders', 'sales_order'), ('dealer', 'dealer'),
                            ('hospital', 'hospital'), ('province', 'dim_province')]
    }
    value.loader.entities['orders']['relations'] = [
        {'target_entity': owner, 'join_key': f'sales_order.{owner}_id = {owner}.id'}
        for owner in ('dealer', 'hospital')]
    for owner in ('dealer', 'hospital'):
        value.loader.entities[owner]['relations'] = [
            {'target_entity': 'province', 'join_key': f'{owner}.province_id = dim_province.id'}]
    value.loader.dimensions = {'province': {
        'dim_code': 'province', 'field_mapping': {'dim_table_field': 'dim_province.id'},
        'bind_entities': [{'entity_code': 'province'}]}}
    return value


@pytest.mark.parametrize('owner', ['hospital', 'dealer'])
@pytest.mark.parametrize('dimensions', [[{'name': 'province'}], [{'name': 'dim_province.name'}]])
def test_shared_dictionary_follows_explicit_foreign_key(owner, dimensions):
    value = fixture()
    joins = ' '.join(value._detect_additional_joins('orders', dimensions,
        [{'field': f'{owner}.province_id', 'operator': '=', 'value': '310000'}], model_id='130'))
    assert f'{owner}.province_id = dim_province.id' in joins
    other = 'dealer' if owner == 'hospital' else 'hospital'
    assert f'{other}.province_id = dim_province.id' not in joins
    assert joins.index(f'JOIN {owner} ') < joins.index('JOIN dim_province ')


def test_unrelated_filter_does_not_reassign_dimension():
    value = fixture()
    dims = [{'name': 'province'}]
    baseline = value._detect_additional_joins('orders', dims, [], model_id='130')
    joins = value._detect_additional_joins('orders', dims,
        [{'field': 'hospital.level', 'operator': '=', 'value': '3'}], model_id='130')
    assert baseline[0] == joins[0]


def test_explicit_owner_field_is_not_rebound_to_filtered_owner():
    value = fixture()
    joins = ' '.join(value._detect_additional_joins('orders', [{'name': 'dealer.province_id'}],
        [{'field': 'hospital.province_id', 'operator': '=', 'value': '310000'}], model_id='130'))
    assert 'dim_province' not in joins


def test_conflicting_dictionary_owners_do_not_pick_arbitrary_route():
    value = fixture()
    with pytest.raises(ValueError, match='共享维度'):
        value._detect_additional_joins('orders', [{'name': 'province'}],
            [{'field': f'{owner}.province_id', 'operator': '=', 'value': '310000'}
             for owner in ('hospital', 'dealer')], model_id='130')


def test_hospital_count_remains_one_region_despite_dealers_in_other_regions():
    import sqlite3
    value = fixture()
    joins = ' '.join(value._detect_additional_joins('orders', [{'name': 'province'}],
        [{'field': 'hospital.province_id', 'operator': '=', 'value': '310000'}], model_id='130'))
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE sales_order(dealer_id INT, hospital_id INT);
            CREATE TABLE dealer(id INT, province_id TEXT);
            CREATE TABLE hospital(id INT, province_id TEXT);
            CREATE TABLE dim_province(id TEXT);
            INSERT INTO dealer VALUES(1,'110000'),(2,'320000');
            INSERT INTO hospital VALUES(10,'310000'),(11,'310000');
            INSERT INTO dim_province VALUES('110000'),('320000'),('310000');
            INSERT INTO sales_order VALUES(1,10),(2,10),(2,11);
        ''')
        result = db.execute('SELECT dim_province.id, COUNT(DISTINCT sales_order.hospital_id) '
            'FROM sales_order ' + joins + " WHERE hospital.province_id='310000' GROUP BY dim_province.id").fetchall()
        assert result == [('310000', 2)]
