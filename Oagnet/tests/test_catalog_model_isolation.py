"""Execute loader SQL on synthetic metadata with IDs reused across models."""
from contextlib import contextmanager
import sqlite3

import pytest

import mysql_tool as mysql

ENTITY = '12345678-1234-4567-89ab-123456789abc'


@pytest.fixture
def metadata_db(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    schemas = {
        'semantic_model': 'id name code description is_deleted',
        'semantic_model_business_domain': 'id semantic_model_id is_deleted',
        'semantic_model_entity_type': 'id semantic_model_id business_domain_id code name description alias update_frequency main_table_name update_time create_time is_deleted status data_source_id',
        'semantic_model_attribute_config': 'id semantic_model_id entity_type_id code attr_name description mapping_table mapping_column data_type is_main_attribute is_primary_key is_unique is_required prefix suffix update_time create_time is_deleted vectorization',
        'semantic_model_relation_config': 'id semantic_model_id name description source_entity_type_id target_entity_type_id source_field target_field source_field_name target_field_name source_table_column_name target_table_column_name code type update_time create_time is_deleted',
        'semantic_model_entity_bind_indicator': 'semantic_model_id entity_code indicator_code indicator_name indicator_logic update_time create_time is_deleted',
        'semantic_model_indicator': 'semantic_model_id business_domain_id indicator_code indicator_name is_deleted',
        'semantic_model_data_source': 'id semantic_model_id is_deleted status db_type host port username password db_name',
        'semantic_model_table': 'id semantic_model_id data_source_id name is_deleted',
        'semantic_model_field': 'id semantic_model_id data_source_id table_id name is_deleted type',
    }
    for table, fields in schemas.items():
        db.execute(f'CREATE TABLE {table} ({",".join(fields.split())})')

    def insert(table, **row):
        db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))

    for model, domain in [(81,205), (82,206)]:
        insert('semantic_model', id=model, is_deleted=0)
        insert('semantic_model_business_domain', id=domain, semantic_model_id=model, is_deleted=0)
        insert('semantic_model_entity_type', id=ENTITY, semantic_model_id=model, business_domain_id=domain,
               code='hospital', name='Hospital', is_deleted=0, status=1, data_source_id=7,
               update_time=str(model), create_time=str(model))
    for model, attr, column in [(81,'abcdef0123456789abcdef0123456789','name'),
                                (82,'bcdefa0123456789abcdef0123456789','secret')]:
        insert('semantic_model_attribute_config', id=attr, semantic_model_id=model, entity_type_id=ENTITY,
               code='name', attr_name='Name', mapping_table='hospitals', mapping_column=column,
               is_deleted=0, is_main_attribute=1, vectorization=1, update_time=str(model), create_time=str(model))
        insert('semantic_model_relation_config', id=str(model), semantic_model_id=model, code='same_relation',
               name='Relation', source_entity_type_id=ENTITY, target_entity_type_id=ENTITY,
               source_field='hospitals.'+column, is_deleted=0, update_time=str(model), create_time=str(model))
    insert('semantic_model_data_source', id=7, semantic_model_id=81, is_deleted=0, status=1,
           db_type='mysql', host='fixture.invalid', port=3306, username='fixture', password='fixture', db_name='fixture')
    insert('semantic_model_table', id=8, semantic_model_id=81, data_source_id=7, name='hospitals', is_deleted=0)
    # Both fields exist physically in model 81. Physical membership alone must
    # not authorize another model's semantic attribute or suppress its main key.
    for index, column in enumerate(['name','secret']):
        insert('semantic_model_field', id=9+index, semantic_model_id=81, data_source_id=7,
               table_id=8, name=column, is_deleted=0, type='varchar')

    def query(sql, args=None):
        # SQLite lacks MySQL's numeric/string equality coercion for COALESCE.
        return [dict(r) for r in db.execute(sql.replace('%s','?').replace("'0'",'0'), args or ())]

    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,sql,args=None):self.rows=query(sql,args)
        def fetchall(self):return self.rows
    class Connection:
        def cursor(self,*_):return Cursor()
    @contextmanager
    def connection():yield Connection()
    monkeypatch.setattr(mysql,'_query',query)
    monkeypatch.setattr(mysql,'_catalog_read_connection',connection)
    yield insert
    db.close()


def test_catalog_does_not_choose_foreign_newer_attribute_or_relation(metadata_db):
    entity = mysql.get_entity(205)[0]
    assert entity['attributes'][0]['attribute_id'] == 'abcdef0123456789abcdef0123456789'
    assert entity['attributes'][0]['field_mapping'] == 'hospitals.name'
    assert entity['relations'][0]['join_key']['source_field'] == 'hospitals.name'
    # Explicit other-domain capture remains possible under its own model.
    other = mysql.get_entity(206)[0]
    assert other['attributes'][0]['field_mapping'] == 'hospitals.secret'


@pytest.mark.parametrize('domains', [None, 205])
def test_attribute_discovery_uses_semantic_model_as_well_as_entity_id(metadata_db, domains):
    registered = mysql.get_registered_entity_attributes(81, domains)
    assert [r['field_mapping'] for r in registered] == ['hospitals.name']
    published = mysql.load_published_entity_attribute_candidates(81, domains)
    assert [r['field'] for r in published] == ['hospitals.name']


@pytest.mark.parametrize('unique_main', [False, True])
def test_foreign_attribute_neither_authorizes_field_nor_blocks_current_main(metadata_db, unique_main):
    candidates = [{'entity_code':'hospital','business_domain_id':205,'field':'hospitals.'+column}
                  for column in ['name','secret']]
    _, rows = mysql._authorized_entity_field_rows(81, 205, candidates,
        require_unique_main=unique_main, max_candidates=32)
    assert [r['mapping_column'] for r in rows] == ['name']


def test_vector_source_definitions_exclude_foreign_model_attributes(metadata_db):
    rows = mysql._entity_attribute_vector_definitions(81,205)
    assert [r['mapping_column'] for r in rows] == ['name']


@pytest.mark.parametrize('attribute_model', [None, 82])
def test_missing_or_foreign_model_is_not_repaired_from_parent(metadata_db, attribute_model):
    metadata_db('semantic_model_attribute_config', id='new', semantic_model_id=attribute_model,
        entity_type_id=ENTITY, code='injected', attr_name='Injected', mapping_table='hospitals',
        mapping_column='secret', is_deleted=0)
    assert len(mysql.get_entity(205)[0]['attributes']) == 1
    assert len(mysql.get_registered_entity_attributes(81,205)) == 1


def test_foreign_entity_model_cannot_override_domain_ownership(metadata_db):
    metadata_db('semantic_model_entity_type', id=ENTITY, semantic_model_id=82,
        business_domain_id=205, code='injected', name='Injected', is_deleted=0,
        status=1, update_time='999', create_time='999', data_source_id=7)
    assert [e['entity_code'] for e in mysql.get_entity(205)] == ['hospital']
    assert {e['entity_code'] for e in mysql.get_registered_entity_attributes(81,205)} == {'hospital'}
