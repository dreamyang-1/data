"""Declared relationship type must survive MySQL -> DSL -> pinned generation."""
import pytest

from catalog_generation import build_catalog_records
from mysql_tool import _row_to_relation_dict
from test_catalog_publication import authority, reseal


@pytest.mark.parametrize('declared', ['1:1', '1:N', 'N:1', 'N:M'])
def test_relation_declaration_is_preserved_through_generation(declared):
    source = authority()
    relation = _row_to_relation_dict(dict(code='declared', name='关系', type=declared,
        target_entity_code='hospital', source_table_column_name='hospitals-id', target_table_column_name='hospitals.parent_id'))
    source['documents'][0]['entities'][0]['relations'] = [relation]
    rows, _ = build_catalog_records(reseal(source), lambda texts: [[0.1, 0.2] for _ in texts])
    row = next(r for r in rows if r.metadata['type'] == 'relation')
    assert row.metadata['relation_type'] == declared
    assert row.metadata['parent'] == row.metadata['target_entity'] == 'hospital'
    assert row.metadata['join_key'] == dict(source_field='hospitals.id', target_field='hospitals.parent_id')


def test_missing_relation_type_is_not_invented():
    assert _row_to_relation_dict(dict(code='unknown', name='未知'))['relation_type'] is None
