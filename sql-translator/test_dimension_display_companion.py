import pytest

from test_sql_translator_hardening import translator


def configured(code):
    value = translator()
    table = 'dim_' + code
    value.loader.entities[code] = {
        'entity_code': code, 'physical_table_join': {'base_table': table},
        'attributes': [
            {'attr_code': code + '_id', 'field_mapping': table + '.id'},
            {'attr_code': code + '_name', 'attr_name': 'Visible name',
             'field_mapping': table + '.label'},
        ],
    }
    value.loader.dimensions[code] = {
        'dim_code': code, 'dim_name': 'Object',
        'field_mapping': {'fact_table_field': table + '.id'},
        'bind_entities': [{'entity_code': code}],
    }
    return value, table


@pytest.mark.parametrize('code', ['city', 'department', 'product', 'hospital', 'dealer'])
def test_logical_entity_dimension_preserves_identity_and_adds_name(code):
    value, table = configured(code)
    dimensions = [{'name': code}]
    sql = value._build_select_clause([], dimensions, 'ent_order', '6')
    grouped = value._build_group_by_clause(dimensions, 'ent_order', '6')
    assert table + '.label AS `Visible name`' in sql
    assert table + '.id AS `Object（编码）`' in sql
    assert sql.index('.label') < sql.index('.id')
    assert grouped == f'GROUP BY {table}.id, {table}.label'


def test_explicit_physical_code_projection_is_unchanged():
    value, table = configured('city')
    assert value._dimension_display_companion({'name': table + '.id'}, 'ent_order', '6') is None


@pytest.mark.parametrize('mode', ['missing', 'ambiguous', 'different_table'])
def test_unproven_name_mapping_is_not_guessed(mode):
    value, table = configured('city')
    attrs = value.loader.entities['city']['attributes']
    if mode == 'missing':
        attrs.pop()
    elif mode == 'ambiguous':
        attrs.append({'is_main_attribute': True, 'attr_name': 'Other name',
                      'field_mapping': table + '.other_name'})
    else:
        attrs[-1]['field_mapping'] = 'unrelated.label'
    assert value._dimension_display_companion({'name': 'city'}, 'ent_order', '6') is None


def test_duplicate_metadata_does_not_create_ambiguity():
    value, _ = configured('city')
    value.loader.entities['city']['attributes'] *= 2
    assert value._dimension_display_companion({'name': 'city'}, 'ent_order', '6') is not None


def test_scoped_flat_mapping_without_entity_id_resolution():
    value, table = configured('city')
    value.loader.dimensions['city']['bind_entities'] = []
    # The physical binding and scoped entity attributes still prove ownership.
    assert value._dimension_display_companion({'name': 'city'}, 'ent_order', '6') == (table + '.label', 'Visible name')


def test_already_selected_name_is_not_duplicated():
    value, table = configured('city')
    sql = value._build_select_clause([], [{'name': 'city'}, {'name': table + '.label'}], 'ent_order', '6')
    assert sql.count(table + '.label AS') == 1


@pytest.mark.parametrize('kind', ['dimension', None])
def test_sort_uses_emitted_identity_alias(kind):
    value, _ = configured('city')
    sql = value._build_order_by_clause({'field': 'city', 'field_type': kind}, [], [{'name': 'city'}], '6')
    assert sql == 'ORDER BY `Object（编码）` ASC'
