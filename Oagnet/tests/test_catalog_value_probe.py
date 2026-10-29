from copy import deepcopy

import pytest

import catalog_value_sources as values
from catalog_release import CatalogEvidenceError
from test_catalog_value_sources import catalog, business, attribute_id, SCOPE


def empty_then_probe(monkeypatch, catalog, business):
    pin = catalog[0].pin(81, [205]); attribute = attribute_id(pin)
    business[0].clear()
    pin.lookup_entity_values(attribute, '甲')
    business[0][:] = ['甲市', '乙市']
    return pin, attribute


def test_bounded_probe_is_readonly_one_field_and_does_not_claim_exact_match(monkeypatch, catalog, business):
    pin, attribute = empty_then_probe(monkeypatch, catalog, business)
    observed = pin.probe_entity_values(attribute, '甲')
    assert observed['values'] == ['乙市', '甲市'] and observed['complete'] is True
    assert observed['source'] == 'VERIFIED_SOURCE_BOUNDED_PROBE'
    connection, options = business[1][-1]
    assert connection.closed == connection.rolled_back == 1
    assert connection.sql[0] == ('START TRANSACTION READ ONLY', None)
    sql, args = connection.sql[-1]
    assert 'MAX_EXECUTION_TIME(15000)' in sql and 'FROM `hospitals`' in sql
    assert '`city`' in sql and 'JOIN' not in sql and args == (65,)
    assert options['connect_timeout'] <= 8 and options['read_timeout'] <= 20
    # Revalidation replays both discovery and original exact lookup separately.
    original = values.query_values
    monkeypatch.setattr(values, 'query_values', lambda *args: [] if args[2]=='甲' else original(*args))
    pin.finish()


def test_probe_requires_current_empty_lookup_and_implicit_governance(catalog, business):
    pin = catalog[0].pin(81, [205]); attribute = attribute_id(pin)
    with pytest.raises(CatalogEvidenceError, match='REQUIRES_EMPTY'):
        pin.probe_entity_values(attribute, '甲')
    pin.lookup_entity_values(attribute, '甲城')
    with pytest.raises(CatalogEvidenceError, match='REQUIRES_EMPTY'):
        pin.probe_entity_values(attribute, '甲城')
    business[0].clear(); pin.lookup_entity_values(attribute, '甲')
    with pytest.raises(CatalogEvidenceError, match='IMPLICIT_SEARCH_NOT_GOVERNED'):
        pin.probe_entity_values(attribute, '甲', require_implicit_policy=True)


def test_high_cardinality_never_returns_a_partial_candidate_set(monkeypatch, catalog, business):
    pin, attribute = empty_then_probe(monkeypatch, catalog, business)
    business[0][:] = [str(i) for i in range(65)]
    result = pin.probe_entity_values(attribute, '甲')
    assert result['complete'] is False and result['values'] == []


@pytest.mark.parametrize('fault', ['scope','mapping','policy','value_drift','budget'])
def test_probe_cannot_bypass_scope_mapping_freshness_or_budget(monkeypatch, catalog, business, fault):
    pin, attribute = empty_then_probe(monkeypatch, catalog, business)
    if fault in {'scope','mapping','policy'}:
        key = {'scope':'business_domain_id','mapping':'mapping_column','policy':'vectorization'}[fault]
        catalog[1][0][key] = {'scope':206,'mapping':'other','policy':1}[fault]
        with pytest.raises(CatalogEvidenceError): pin.probe_entity_values(attribute, '甲')
    elif fault == 'value_drift':
        pin.probe_entity_values(attribute, '甲'); business[0].append('甲县')
        with pytest.raises(CatalogEvidenceError, match='CHANGED_DURING_READ'): pin.finish()
    else:
        for _ in range(3): pin.probe_entity_values(attribute, '甲')
        reads = len(business[1])
        with pytest.raises(CatalogEvidenceError, match='BUDGET'): pin.probe_entity_values(attribute, '甲')
        assert len(business[1]) == reads


@pytest.mark.parametrize('rows', [['duplicate','duplicate'], ['x'*257], [''], [None], [1]])
def test_untrusted_probe_results_are_rejected(monkeypatch, catalog, business, rows):
    pin, attribute = empty_then_probe(monkeypatch, catalog, business)
    business[0][:] = rows
    with pytest.raises(CatalogEvidenceError, match='PROBE_RESULT_INVALID'): pin.probe_entity_values(attribute, '甲')


def test_source_failure_rolls_back_closes_and_masks_private_error(monkeypatch, catalog, business):
    pin, attribute = empty_then_probe(monkeypatch, catalog, business)
    from test_catalog_value_sources import Connection
    connection = Connection([]); connection.error = 'credential-or-private-host'
    import mysql_tool
    monkeypatch.setattr(mysql_tool.pymysql, 'connect', lambda **kwargs: connection)
    with pytest.raises(CatalogEvidenceError, match='^CATALOG_VALUE_PROBE_FAILED$'):
        pin.probe_entity_values(attribute, '甲')
    assert connection.rolled_back == connection.closed == 1
