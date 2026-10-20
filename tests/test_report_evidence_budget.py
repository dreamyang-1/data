import asyncio
import copy
import json

import httpx
import pytest

from app.analysis.report_contract import build_report_contract, report_issues
from app.analysis.synthesis import QwenAnalysisSynthesizer, SynthesisValidationError
from app.config import Settings


REFERENCE = {'available': True, 'content': '''| 指标 | 单位 | 业务口径 |
| --- | --- | --- |
| 成交金额 | 元 | 金额合计 |
| 成交笔数 | 笔 | 成交次数 |
| 商品数量 | 件 | 商品数量合计 |
'''}


def material(right='成交笔数'):
    return {'task_id': 'calc', 'status': 'COMPLETED', 'facts': {
        'delivery_operation': {'type': 'pipeline', 'operations': [
            {'type': 'derive', 'operator': 'divide', 'left_field': '成交金额',
             'right_field': right, 'result_field': '平均成交金额'}]},
        'query_data': {'columns': ['平均成交金额'], 'rows': [{'平均成交金额': 20}]}}}


def contract():
    return build_report_contract({'tasks': [material()], 'semantic_reference': REFERENCE})


def report(text):
    return {'claims': [{'statement': text}], 'final_answer': {
        'overview': text, 'result_task_ids': ['calc'], 'result_titles': {'calc': '平均成交金额'}}}


@pytest.mark.parametrize('right,unit', [('成交笔数', '元/笔'), ('商品数量', '元/件')])
def test_executed_denominator_drives_unit_without_rewriting_inputs(right, unit):
    data = {'tasks': [material(right)], 'semantic_reference': REFERENCE}
    saved = copy.deepcopy(data)
    result = build_report_contract(data)
    assert result['executed_ratios'][0]['denominator'] == right
    assert result['executed_ratios'][0]['unit'] == unit
    assert data == saved


def test_planned_formula_and_cell_text_are_not_executed_evidence():
    data = {'tasks': [{'structured_parameters': {'计算绑定': {'operation': 'divide'}},
                      'facts': {'query_data': {'rows': [{'causality_established': True}]}}}]}
    assert build_report_contract(data)['executed_ratios'] == []
    assert build_report_contract(data)['causality_established'] is False


def test_sql_returned_metric_keeps_scoped_unit_without_inventing_a_formula():
    ref = {'content': REFERENCE['content'] + '| 平均成交金额 | 元/笔 | 金额除以笔数 |\n'}
    data = {'facts': {'query_data': {'columns': ['平均成交金额']}},
            'semantic_reference': ref}
    result = build_report_contract(data)
    assert result['returned_metric_units'] == {'平均成交金额': '元/笔'}
    assert not result['executed_ratios']
    assert report_issues('平均成交金额20元/单位。', result)
    assert not report_issues('平均成交金额20元/笔。', result)


def test_units_are_not_borrowed_from_unreturned_catalog_metrics():
    data = {'facts': {'query_data': {'columns': ['未登记均价']}},
            'semantic_reference': REFERENCE}
    assert build_report_contract(data)['returned_metric_units'] == {}


def test_business_explanations_in_tips_receive_review_too():
    from app.analysis.report_contract import review_candidates
    output = QwenAnalysisSynthesizer._parse_report(json.dumps({
        'claims': [{'statement': '平均金额20元/笔。'}],
        'final_answer': {'tips': [
            '平均金额为负通常关联退货或冲销，建议核查明细。',
            '本次计算未排除样品单、调拨单等非销售单据。',
            '结果不包含后续订单变更或跨年结算。']}}), set())
    assert {item['text'] for item in review_candidates(output)} == set(output.final_answer['tips'])


@pytest.mark.parametrize('text', [
    '平均成交金额为20万元/单位。',
    '高均价主要由大额专项订单驱动。',
    '【推测】高均价主要由大额专项订单驱动。',
    '负金额源于退货冲销。',
    '缺失对Top5排名影响极小。',
    '少量空值不影响最终排名。',
    '无法确认公司归属，但缺失对排名影响极小。',
    '多笔订单说明均价稳定。',
    '这一均价并非单笔极端订单拉高。',
    '若同一订单含正负行项目，均价分母可能偏小。',
    '退货或冲销已按净额计入，可能影响个别人员均价。',
    '高均价受单笔订单影响极大。',
    '高均价可能受单笔大额订单驱动。',
    '结果对异常值敏感度较高。',
    '订单量较多，均值代表性相对更强。',
    '订单量较多且保持高均价，表现较为稳健。',
    '低订单量人员的高均价稳定性较低。',
    '少量超大额订单可能拉高个别销售人员的均值。',
    '当前均价未排除负向订单，可能存在轻微偏差。',
    '平均金额为负通常关联退货或冲销。',
    '负值说明当前口径未排除退货或冲销订单。',
    '当前排名包含退货/冲销等负向订单。',
])
def test_reproduced_unsupported_claims_are_detected(text):
    assert report_issues(text, contract())


@pytest.mark.parametrize('text', [
    '平均成交金额为20元/笔，按成交金额除以成交笔数计算。',
    '单价为20元/件。',
    '华南贡献30抵消华东下降80的37.5%，剩余净下降50。',
    '订单笔数较少，不能据此判断均价稳定。',
    '公司字段缺失，尚不能判断缺失对排名的影响。',
    '负金额不能直接归因于退货。',
])
def test_observations_calculations_and_explicit_limits_remain_valid(text):
    assert not report_issues(text, contract())


@pytest.mark.asyncio
async def test_bad_unit_gets_one_correction_using_actual_denominator(monkeypatch):
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', reference)
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        data = json.loads(calls[-1]['messages'][1]['content'])
        if 'items' in data:
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
                'reviews': [{'id': item['id'], 'kind': 'FACT', 'supported': True} for item in data['items']]})}}]})
        output = report('平均成交金额为20万元/单位。' if len(calls) == 1 else '平均成交金额为20元/笔。')
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]})
    model = QwenAnalysisSynthesizer(Settings(env='test', intent_model_api_key='test'), httpx.MockTransport(handler))
    text, output = await model.synthesize_combined('平均成交金额是多少？', [material()])
    assert len(calls) == 3 and '元/笔' in text and '每单位' not in text
    data = json.loads(calls[1]['messages'][1]['content'])
    assert data['report_contract']['executed_ratios'][0]['unit'] == '元/笔'
    assert data['report_corrections'] and output.final_answer['result_task_ids'] == ['calc']


async def reference(*args, **kwargs):
    return REFERENCE


@pytest.mark.asyncio
async def test_repeated_bad_prose_is_removed_without_losing_selection_or_good_facts(monkeypatch):
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', reference)
    calls = []
    async def handler(request):
        calls.append(1)
        data = json.loads(json.loads(request.content)['messages'][1]['content'])
        if 'items' in data:
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
                'reviews': [{'id': item['id'], 'kind': 'FACT', 'supported': True} for item in data['items']]})}}]})
        output = report('公司字段缺失对Top5排名影响极小。')
        output['claims'].append({'statement': '平均成交金额为20元/笔。'})
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]})
    model = QwenAnalysisSynthesizer(Settings(env='test', intent_model_api_key='test'), httpx.MockTransport(handler))
    text, output = await model.synthesize_combined('平均成交金额是多少？', [material()])
    assert calls == [1, 1, 1] and text == '平均成交金额为20元/笔。'
    assert 'overview' not in output.final_answer
    assert output.final_answer['result_task_ids'] == ['calc']


@pytest.mark.asyncio
async def test_synthesis_total_deadline_degrades_report_not_query(monkeypatch):
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', reference)
    async def handler(request):
        await asyncio.sleep(.2)
        return httpx.Response(200)
    settings = Settings(env='test', intent_model_api_key='test', analysis_synthesis_timeout_seconds=.02)
    with pytest.raises(SynthesisValidationError, match='deadline'):
        await QwenAnalysisSynthesizer(settings, httpx.MockTransport(handler)).synthesize_combined('问题', [material()])


def test_answer_budget_is_five_minutes_and_asl_keeps_report_reserve():
    settings = Settings(_env_file=None)
    assert settings.request_timeout_seconds == 300
    assert settings.asl_generation_timeout_seconds == 180
    assert settings.asl_generation_timeout_seconds + settings.analysis_synthesis_timeout_seconds < settings.request_timeout_seconds


@pytest.mark.asyncio
async def test_semantic_review_removes_paraphrased_inference_but_cannot_edit_data(monkeypatch):
    monkeypatch.setattr('app.analysis.synthesis._semantic_reference', reference)
    calls = []
    unsupported = '这种格局提示客户采购方式存在差别。'
    async def handler(request):
        body = json.loads(request.content);data = json.loads(body['messages'][1]['content']);calls.append(data)
        if 'items' in data:
            assert data['evidence_context']['tasks'][0]['facts']['query_data']['rows'] == [{'平均成交金额': 20}]
            output = {'reviews': [{'id': item['id'], 'kind': 'INFERENCE' if item['text'] == unsupported else 'FACT', 'supported': True} for item in data['items']],
                      'result_task_ids': ['invented'], 'replacement': '平均金额为999元。'}
        else:
            output = report('平均成交金额为20元/笔。'+unsupported)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output, ensure_ascii=False)}}]})
    text, output = await QwenAnalysisSynthesizer(Settings(env='test', intent_model_api_key='test'), httpx.MockTransport(handler)).synthesize_combined('平均成交金额', [material()])
    assert len(calls) == 2 and text == '平均成交金额为20元/笔。'
    assert output.final_answer['result_task_ids'] == ['calc'] and '999' not in text


@pytest.mark.parametrize('review', [None, {'reviews': []}, {'reviews': [{'id': '0', 'supported': 'true'}]},
    {'reviews': [{'id': '0', 'supported': True}, {'id': '0', 'supported': True}]}])
def test_unavailable_or_malformed_review_preserves_facts_and_result_selection(review):
    from app.analysis.report_contract import apply_evidence_review, review_candidates
    from app.analysis.synthesis import SynthesisOutput
    output = SynthesisOutput.model_validate(report('平均成交金额为20元/笔。排名逆转的可能性较低。'))
    clean = apply_evidence_review(output, review_candidates(output), review)
    assert clean.claims[0].statement == '平均成交金额为20元/笔。'
    assert clean.final_answer['result_task_ids'] == ['calc']


def test_valid_causal_limitation_and_paragraph_format_are_preserved():
    from app.analysis.report_contract import apply_evidence_review, review_candidates
    from app.analysis.synthesis import SynthesisOutput
    output = SynthesisOutput.model_validate(report('### 数据范围\n最高值为20。\n\n不能据此确定业务原因。'))
    candidates = review_candidates(output)
    reviews = {'reviews': [{'id': item['id'], 'kind': 'FACT', 'supported': True} for item in candidates]}
    assert apply_evidence_review(output, candidates, reviews) == output
    assert not review_candidates(SynthesisOutput.model_validate(report('华南抵消部分下降，净变化为-50，但这不能证明下降的业务原因。')))


def test_deterministic_rejection_keeps_factual_sentence_in_same_paragraph():
    from app.analysis.report_contract import remove_unsupported_text
    from app.analysis.synthesis import SynthesisOutput
    output = SynthesisOutput.model_validate(report('平均成交金额20元/笔。表现较为稳健。'))
    clean = remove_unsupported_text(output, contract())
    assert clean.claims[0].statement == '平均成交金额20元/笔。'
    assert clean.final_answer['overview'] == '平均成交金额20元/笔。'
    assert clean.final_answer['result_task_ids'] == ['calc']
