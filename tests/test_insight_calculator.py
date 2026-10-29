import json
from copy import deepcopy

import httpx
import pytest

from app.analysis.insight_calculator import InsightCalculator
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.config import Settings


def data():
    return {"facts": {"query_data": {"rows": [
        {"brand": "A", "amount": "151980.0054"},
        {"brand": "A", "amount": "75919.0387"},
        {"brand": "A", "amount": "52000.001"},
        {"brand": "A", "amount": "19799.9956"},
        {"brand": "A", "amount": "18414.9998"},
        {"brand": "A", "amount": "12690.7136"},
        {"brand": "A", "amount": "6313.9993"},
        {"brand": "A", "amount": "805.9951"},
        {"brand": "A", "amount": "0"},
        {"brand": "B", "amount": "32598.0366"},
        {"brand": "B", "amount": "28212.2128"},
        {"brand": "B", "amount": "11599.9924"},
    ], "sample_only": False, "total_row_count_confirmed": True}}}


SUM = {"operation": "aggregate", "source": "data_0", "function": "sum", "field": "amount", "group_by": ["brand"]}


def test_group_sum_counts_and_chained_ratio_use_all_rows():
    payload = data(); original = deepcopy(payload)
    calc = InsightCalculator(payload)
    result = calc.execute(json.dumps(SUM))
    assert result["rows"] == [
        {"brand": "A", "value": 337924.7485, "row_count": 9},
        {"brand": "B", "value": 72410.2418, "row_count": 3},
    ]
    ratio = calc.execute(json.dumps({"operation": "arithmetic", "operator": "divide",
        "left": {"source": "calc_0", "row": 0, "field": "value"},
        "right": {"source": "calc_0", "row": 1, "field": "value"}}))
    assert ratio["rows"][0]["value"] == pytest.approx(337924.7485/72410.2418)
    assert payload == original
    assert InsightCalculator(data()).execute(json.dumps({**SUM,"source":"calc_0"}))["status"] == "FAILED"


@pytest.mark.parametrize("args", [
    {**SUM, "source": "outside"}, {**SUM,"field":"missing"},
    {**SUM,"group_by":["missing"]}, {**SUM,"function":"eval"},
    {"operation":"arithmetic","operator":"divide","left":{"value":1},"right":{"value":0}},
    {"operation":"arithmetic","operator":"add","left":{"source":"data_0","row":-1,"field":"amount"},"right":{"value":1}},
])
def test_bad_inputs_do_not_create_result_or_zero(args):
    calc = InsightCalculator(data())
    assert calc.execute(json.dumps(args))["status"] == "FAILED"
    assert not calc.results


def test_partial_data_stays_partial_and_review_can_reference_results():
    payload = data(); payload["facts"]["query_data"]["sample_only"] = True
    result = InsightCalculator(payload).execute(json.dumps(SUM))
    assert result["sample_only"] is True
    payload["insight_calculations"] = [result]
    assert "calc_0" in InsightCalculator(payload).sources


def test_failed_tasks_are_not_calculator_sources():
    assert not InsightCalculator({"tasks":[{"status":"FAILED", **data()}]}).sources


def test_provider_prose_before_report_json_preserves_final_answer():
    report={"claims":[{"statement":"Calculated 337924.7485"}],"final_answer":{"overview":"337924.7485"}}
    raw='Model explanation\n```json\n'+json.dumps(report)+'\n```'
    parsed=QwenAnalysisSynthesizer._parse_report(raw,set())
    assert len(parsed.claims)==1
    assert parsed.claims[0].statement=='Calculated 337924.7485'
    assert parsed.final_answer==report['final_answer']
    assert QwenAnalysisSynthesizer._parse_report('Normal plain text',set()).claims[0].statement=='Normal plain text'
    legacy='```json\n'+json.dumps({"claims":[{"statement":"Legacy fenced report"}]})+'\n```'
    assert QwenAnalysisSynthesizer._parse_report(legacy,set()).claims[0].statement=='Legacy fenced report'


@pytest.mark.asyncio
async def test_invalid_tool_can_be_corrected_without_inventing_a_result():
    calls=[]
    async def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:
            return httpx.Response(200,json={"choices":[{"message":{"tool_calls":[{"id":"bad","type":"function","function":{"name":"insight_calculator","arguments":json.dumps({**SUM,"field":"absent"})}}]}}]})
        if len(calls)==2:
            assert json.loads(body["messages"][-1]["content"])["status"]=="FAILED"
            return httpx.Response(200,json={"choices":[{"message":{"tool_calls":[{"id":"good","type":"function","function":{"name":"insight_calculator","arguments":json.dumps(SUM)}}]}}]})
        assert json.loads(body["messages"][-1]["content"])["rows"][0]["value"]==337924.7485
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"claims":[{"statement":"done"}],"final_answer":{"overview":"done"}})}}]})
    payload=data()
    synth=QwenAnalysisSynthesizer(Settings(env="test",intent_model_api_key="test-key"),httpx.MockTransport(handler))
    assert 'done' in await synth._request_content("test",payload,name="analysis-synthesis")
    assert len(payload["insight_calculations"])==1


@pytest.mark.asyncio
async def test_model_tool_round_trip_and_computed_evidence():
    calls = []
    async def handler(request):
        body = json.loads(request.content); calls.append(body)
        if len(calls) == 1:
            return httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":None,
                "tool_calls":[{"id":"sum1","type":"function","function":{"name":"insight_calculator","arguments":json.dumps(SUM)}}]}}]})
        result = json.loads(body["messages"][-1]["content"])
        assert body["messages"][-1]["tool_call_id"] == "sum1"
        assert result["rows"][0]["value"] == 337924.7485
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"claims":[{"statement":"Calculated value: 337924.7485"}],"final_answer":{"overview":"337924.7485"}})}}]})
    payload = data()
    settings = Settings(env="test",intent_model_api_key="test-key")
    synth = QwenAnalysisSynthesizer(settings,httpx.MockTransport(handler))
    content = await synth._request_content("test",payload,name="analysis-synthesis")
    assert len(calls) == 2 and "337924.7485" in content
    assert payload["insight_calculations"][0]["rows"][0]["row_count"] == 9
    assert calls[0]["tools"][0]["function"]["name"] == "insight_calculator"


@pytest.mark.asyncio
async def test_tool_report_missing_final_answer_is_repaired():
    calls=[]
    async def handler(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:
            message={"tool_calls":[{"id":"sum","type":"function","function":{"name":"insight_calculator","arguments":json.dumps(SUM)}}]}
        elif len(calls)==2:
            message={"content":"The sum is 337924.7485."}
        else:
            assert any(m['role']=='tool' and '337924.7485' in m['content'] for m in body['messages'])
            assert 'final_answer' in body['messages'][-1]['content']
            message={"content":json.dumps({"claims":[{"statement":"337924.7485"}],"final_answer":{"overview":"337924.7485"}})}
        return httpx.Response(200,json={"choices":[{"message":message}]})
    synth=QwenAnalysisSynthesizer(Settings(env="test",intent_model_api_key="test-key"),httpx.MockTransport(handler))
    result=json.loads(await synth._request_content("test",data(),name="analysis-synthesis"))
    assert len(calls)==3 and result['final_answer']['overview']=='337924.7485'


def test_evidence_review_accepts_explanation_before_json():
    review = {"reviews": [{"id": "0", "kind": "FACT", "supported": True}]}
    raw = 'Checked the calculator result.\n```json\n' + json.dumps(review) + '\n```'
    assert QwenAnalysisSynthesizer._report_json(raw, review=True) == review
    with pytest.raises(ValueError):
        QwenAnalysisSynthesizer._report_json(raw)


@pytest.mark.asyncio
async def test_report_format_is_repaired_even_without_calculator_call():
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        content = '### final_answer.overview\nThe result.' if len(calls) == 1 else json.dumps({
            'claims': [{'statement': 'The result.'}], 'final_answer': {'overview': 'The result.'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    synth = QwenAnalysisSynthesizer(Settings(env='test', intent_model_api_key='test-key'), httpx.MockTransport(handler))
    result = json.loads(await synth._request_content('test', data(), name='analysis-synthesis'))
    assert len(calls) == 2 and result['final_answer']['overview'] == 'The result.'
