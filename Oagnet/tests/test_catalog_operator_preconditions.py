"""Operator candidate guards: no native services, embedding or write transport."""
from types import SimpleNamespace
import json
import sys

import pytest

from catalog_release import CatalogEvidenceError, catalog_scope, digest
from test_catalog_publication import authority, embed, system


@pytest.mark.parametrize('source_changed',[False,True])
def test_reviewed_source_must_match_the_capture_before_any_embedding_or_write(source_changed):
    service,store,registry,redis,captures,overrides=system()
    reviewed=authority()['catalog_version'] if source_changed else 'stale-review'
    if source_changed:overrides[(81,(205,))]=authority(formula='SUM(changed)')
    def forbidden(*args):raise AssertionError('must not embed a different reviewed source')
    with pytest.raises(CatalogEvidenceError,match='CATALOG_OPERATION_SOURCE_CHANGED'):
        service.publish(81,[205],embed_fn=forbidden,publication_id='approved',
            producer_revision='tested',embedding_contract='fixture',expected_catalog_version=reviewed)
    assert not store.records and not redis.values
    assert captures==[(81,[205])]


def test_guarded_publication_retains_full_native_double_readback_and_pin():
    service,store,registry,redis,captures,_=system()
    receipt=service.publish(81,[205],embed_fn=embed,publication_id='approved',
        producer_revision='tested',embedding_contract='fixture',expected_catalog_version=authority()['catalog_version'])
    assert receipt['records_verified']==8 and len(captures)==3
    assert receipt==service.pin(81,[205]).finish()
    assert registry.active(catalog_scope(81,[205])) is not None


def publish_args():
    return ['publish','--semantic-model-id','81','--business-domain-id','205',
        '--publication-id','approved','--producer-revision','tested',
        '--expected-target-identity-hash','target','--expected-catalog-version',authority()['catalog_version'],
        '--expected-embedding-contract','embedding']


@pytest.mark.parametrize('missing',['--expected-target-identity-hash','--expected-catalog-version','--expected-embedding-contract'])
def test_cli_publish_requires_all_reviewed_preconditions_before_opening_store(monkeypatch,capsys,missing):
    from scripts import manage_catalog_publication as cli
    def forbidden(**kwargs):raise AssertionError('must not open store')
    monkeypatch.setattr(cli,'open_catalog_store',forbidden)
    args=publish_args();index=args.index(missing);del args[index:index+2]
    assert cli.main(args)==2
    assert json.loads(capsys.readouterr().out)['reason_code']=='CATALOG_OPERATION_PRECONDITIONS_REQUIRED'


@pytest.mark.parametrize('action',['initialize','reactivate'])
def test_other_operator_writes_require_the_reviewed_target(monkeypatch,capsys,action):
    from scripts import manage_catalog_publication as cli
    def forbidden(**kwargs):raise AssertionError('must not open store')
    monkeypatch.setattr(cli,'open_catalog_store',forbidden)
    args=[action,'--semantic-model-id','81','--business-domain-id','205']
    if action=='reactivate':args+=['--vector-index-version','previous']
    assert cli.main(args)==2
    assert json.loads(capsys.readouterr().out)['reason_code']=='CATALOG_OPERATION_PRECONDITIONS_REQUIRED'


def test_cli_embedding_change_is_rejected_before_any_store_operation(monkeypatch,capsys):
    from scripts import manage_catalog_publication as cli
    monkeypatch.setattr(cli,'configured_embedding_contract',lambda:'changed')
    def forbidden(**kwargs):raise AssertionError('must not open store')
    monkeypatch.setattr(cli,'open_catalog_store',forbidden)
    assert cli.main(publish_args())==2
    assert json.loads(capsys.readouterr().out)['reason_code']=='CATALOG_OPERATION_EMBEDDING_CHANGED'


@pytest.mark.parametrize('initialize',[False,True])
def test_actual_target_guard_runs_before_milvus_client_construction(monkeypatch,initialize):
    import pymilvus
    from catalog_store import open_catalog_store
    def forbidden(**kwargs):raise AssertionError('must not construct a client')
    monkeypatch.setattr(pymilvus,'MilvusClient',forbidden)
    with pytest.raises(CatalogEvidenceError,match='CATALOG_OPERATION_TARGET_CHANGED'):
        open_catalog_store(initialize=initialize,expected_target_identity_hash='wrong-target')


@pytest.mark.parametrize('initialize',[False,True])
def test_matching_target_reaches_only_requested_store_mode(monkeypatch,initialize):
    import config,pymilvus,catalog_store
    target=dict(backend='milvus',source=dict(uri=f'http://{config.MILVUS_HOST}:{config.MILVUS_PORT}',database=config.MILVUS_DATABASE),
        collections=dict(semantic=config.MILVUS_SEMANTIC_COLLECTION+'_catalog',physical=config.MILVUS_PHYSICAL_COLLECTION+'_catalog'))
    calls=[];client=object()
    monkeypatch.setattr(pymilvus,'MilvusClient',lambda **kwargs:client)
    def store(current,**kwargs):calls.append((current,kwargs));return 'opened'
    monkeypatch.setattr(catalog_store,'MilvusCatalogStore',store)
    assert catalog_store.open_catalog_store(initialize=initialize,expected_target_identity_hash=digest(target))=='opened'
    assert len(calls)==1 and calls[0][0] is client and calls[0][1]['initialize'] is initialize


@pytest.mark.parametrize('source_changed',[False,True])
def test_cli_binds_review_to_actual_publisher_capture(monkeypatch,capsys,source_changed):
    from scripts import manage_catalog_publication as cli
    service,store,registry,redis,_,overrides=system();opened=[];embedding=[]
    if source_changed:overrides[(81,(205,))]=authority(formula='SUM(changed)')
    def open_store(**kwargs):opened.append(kwargs);return store
    def embed_documents(texts):embedding.extend(texts);return embed(texts)
    monkeypatch.setattr(cli,'open_catalog_store',open_store)
    monkeypatch.setattr(cli,'RedisCatalogReleaseRegistry',lambda *a:registry)
    monkeypatch.setattr(cli,'CatalogPublication',lambda *a:service)
    monkeypatch.setattr(cli,'configured_embedding_contract',lambda:'embedding')
    monkeypatch.setitem(sys.modules,'embedding',SimpleNamespace(embed_documents=embed_documents))
    assert cli.main(publish_args())==(2 if source_changed else 0)
    receipt=json.loads(capsys.readouterr().out)
    assert opened==[dict(initialize=False,expected_target_identity_hash='target')]
    if source_changed:
        assert receipt['reason_code']=='CATALOG_OPERATION_SOURCE_CHANGED'
        assert not embedding and not store.records and not redis.values
    else:
        assert receipt['records_verified']==8 and embedding
        assert receipt['scope']['business_domain_ids']==[205]


def test_initialize_passes_target_guard_without_embedding(monkeypatch,capsys):
    from scripts import manage_catalog_publication as cli
    service,store,registry,*_=system();opened=[]
    def open_store(**kwargs):opened.append(kwargs);return store
    monkeypatch.setattr(cli,'open_catalog_store',open_store)
    monkeypatch.setattr(cli,'RedisCatalogReleaseRegistry',lambda *a:registry)
    monkeypatch.setattr(cli,'CatalogPublication',lambda *a:service)
    def forbidden():raise AssertionError('initialize must not request an embedding contract')
    monkeypatch.setattr(cli,'configured_embedding_contract',forbidden)
    assert cli.main(['initialize','--semantic-model-id','81','--business-domain-id','205',
        '--expected-target-identity-hash','target'])==0
    assert opened==[dict(initialize=True,expected_target_identity_hash='target')]
    assert json.loads(capsys.readouterr().out)['action']=='initialize'
