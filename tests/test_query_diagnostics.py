import json
import logging
from types import SimpleNamespace

from app.observability.query_diagnostics import record


def events(caplog):
    return [json.loads(item.message.split('query_diagnostic ', 1)[1])
            for item in caplog.records if item.message.startswith('query_diagnostic ')]


def test_log_identifies_missing_group_and_having_without_private_values(caplog):
    caplog.set_level(logging.INFO, logger='app.observability.query_diagnostics')
    request = SimpleNamespace(request_id='request-1', semantic_model_id=130, business_domain_ids=[296])
    extraction = {'指标': [{'name': '含税销售总额'}], '维度': [],
                  '展示字段': [{'entity': '经销商', 'field': '经销商名称'}],
                  '过滤条件': [{'field': '经销商名称', 'op': '=', 'value': ['私人公司名称']}]}
    asl = {'subject': {'entity': 'sales_order'}, 'metrics': [{'name': 'sales_total_including_tax'}],
           'dimensions': [], 'display_fields': [{'name': 'dealer.dealer_name'}],
           'filters': [{'field': 'dealer.dealer_name', 'operator': '=', 'value': '私人公司名称'}],
           'having': ['SUM(sales_order.amount_with_tax) > 1000000']}
    sql = "SELECT dealer.dealer_name, SUM(sales_order.amount_with_tax) FROM dealer WHERE dealer.dealer_name='私人公司名称' HAVING SUM(sales_order.amount_with_tax)>1000000"
    record('STRUCTURED_EXTRACTION', request, extraction=extraction, status='READY')
    record('ASL_TO_SQL', request, asl=asl, status='STARTED')
    record('SQL_TRANSLATION', request, sql=sql, status='READY')
    logged = events(caplog)
    assert [event['stage'] for event in logged] == ['STRUCTURED_EXTRACTION', 'ASL_TO_SQL', 'SQL_TRANSLATION']
    assert logged[0]['extraction']['维度'] == []
    assert logged[1]['asl']['dimensions'] == []
    assert logged[1]['asl']['having'][0]['fields'] == ['sales_order.amount_with_tax']
    assert logged[1]['asl']['having'][0]['operators'] == ['>']
    assert logged[2]['sql']['has_group_by'] is False and logged[2]['sql']['has_having'] is True
    assert '私人公司名称' not in caplog.text and '1000000' not in caplog.text
    assert sql not in caplog.text


def test_blocker_log_keeps_cause_and_does_not_expose_values_or_credentials(caplog):
    caplog.set_level(logging.INFO, logger='app.observability.query_diagnostics')
    request = SimpleNamespace(request_id='request-2', semantic_model_id=130, business_domain_ids=[])
    extraction = {'指标': [], '过滤条件': [{'field': '销售总额', 'op': '>', 'value': [1000000]}]}
    ambiguity = {'type': 'filter_slot', 'field': '过滤条件[1]', 'phrase': '销售总额',
                 'question': '结构化参数【过滤条件[1]】{"value":"私人公司名称","password":"abc123"}：销售总额指标没有提取出来'}
    record('STRUCTURED_BLOCKER', request, extraction=extraction, asl={'ambiguity': [ambiguity]}, status='BLOCKED')
    event = events(caplog)[0]
    assert event['asl']['ambiguity'][0]['field'] == '过滤条件[1]'
    assert event['asl']['ambiguity'][0]['parameter_name'] == '销售总额'
    assert '销售总额指标没有提取出来' in event['asl']['ambiguity'][0]['reason']
    assert '私人公司名称' not in caplog.text and 'abc123' not in caplog.text and '1000000' not in caplog.text


def test_diagnostic_error_never_breaks_query(monkeypatch):
    request = SimpleNamespace(request_id='request-3', semantic_model_id=130, business_domain_ids=[])
    monkeypatch.setattr('app.observability.query_diagnostics.extraction_shape', lambda _: 1 / 0)
    record('STRUCTURED_EXTRACTION', request, extraction={}, status='READY')
