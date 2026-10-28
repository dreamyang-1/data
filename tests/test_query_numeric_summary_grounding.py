import json
from decimal import Decimal

import httpx
import pytest

from app.analysis.query_insight import build_query_result_insight
from app.analysis.report_contract import build_report_contract
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent


def insight(values):
    request = CanonicalAnalysisRequest(conversation_id='numeric-summary', tenant_id='test', user_id='test',
        original_question='合作时长大于3个月的经销商有哪些', primary_intent=PrimaryIntent.DETAIL_QUERY,
        dimensions=['经销商编码'])
    return build_query_result_insight(request, ['经销商编码', '合作时长'],
        [{'经销商编码': str(i), '合作时长': value} for i, value in enumerate(values)],
        total_row_count=len(values), total_row_count_confirmed=True, truncated=False)


def distribution(output):
    return output.facts['data_profile']['columns'][1]['value_distribution']


def test_original_1000_row_distribution_counts_intermediate_values():
    output = insight([7680, 7680, 4028, 4028, 2201, 1836, 1471] + [375] * 993)
    result = distribution(output)
    assert result['complete'] is True
    assert {v['value']: v['count'] for v in result['values']} == {
        7680: 2, 4028: 2, 2201: 1, 1836: 1, 1471: 1, 375: 993}
    assert result['values'][0]['share_of_returned_rows'] == .993
    assert 'value_distribution' not in output.facts['data_profile']['columns'][0]
    assert '不能据此认定数据库中全部符合条件' in output.answer


def test_distribution_keeps_nulls_out_and_does_not_claim_top_values_are_all_values():
    output = insight([Decimal('1'), '1.0', None, ''] + list(range(2, 42)))
    result = distribution(output)
    assert result['valid_count'] == 42 and result['row_count'] == 44
    assert len(result['values']) == 20 and result['complete'] is False
    assert result['other_value_row_count'] == 21
    assert result['values'][0]['count'] == 2
    assert result['values'][0]['share_of_returned_rows'] == 2 / 44


def test_each_task_keeps_its_own_counts():
    data = {'tasks': [{'task_id': 'first', 'facts': insight([1, 1]).facts},
                      {'task_id': 'second', 'facts': insight([1, 2, 2]).facts}]}
    counts = build_report_contract(data)['returned_value_distributions']
    assert [(v['task_id'], v['row_count']) for v in counts] == [('first', 2), ('second', 3)]


@pytest.mark.asyncio
async def test_numeric_statement_is_reviewed_with_computed_counts(monkeypatch):
    async def reference(*args, **kwargs): return {}
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', reference)
    calls = []
    wrong = '其余996条记录的合作时长均为375天。'
    right = '本次返回1000条记录。'
    async def handler(request):
        data = json.loads(json.loads(request.content)['messages'][1]['content']);calls.append(data)
        if 'items' in data:
            stats = data['evidence_context']['report_contract']['returned_value_distributions'][0]
            assert stats['values'][0]['count'] == 993
            assert wrong in [item['text'] for item in data['items']]
            payload = {'reviews': [{'id': item['id'], 'kind': 'FACT', 'supported': item['text'] != wrong}
                                    for item in data['items']]}
        else:
            assert data['report_contract']['returned_value_distributions'][0]['values'][0]['count'] == 993
            payload = {'claims': [{'statement': right + wrong}],
                       'final_answer': {'overview': wrong, 'result_task_ids': ['details']}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(payload)}}]})
    result = insight([7680, 7680, 4028, 4028, 2201, 1836, 1471] + [375] * 993)
    model = QwenAnalysisSynthesizer(Settings(_env_file=None, intent_model_api_key='test'), httpx.MockTransport(handler))
    text, report = await model.synthesize_combined('合作时长大于3个月的经销商有哪些',
        [{'task_id': 'details', 'status': 'COMPLETED', 'facts': result.facts}])
    assert len(calls) == 2 and wrong not in text and right in text
    assert report.final_answer['result_task_ids'] == ['details']
    assert 'overview' not in report.final_answer
