"""Prompt guidance must reach the model without changing transport or gating prose."""
from copy import deepcopy
import json

import httpx
import pytest

from app.analysis.engine import AnalysisOutput
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent


@pytest.mark.asyncio
@pytest.mark.parametrize("question,rows,metrics,sample_only,report", [
    (
        "查询商品适用科室及联系方式",
        [{"科室": "测试科室", "联系电话": "10000000000", "商品编码": "12345"}],
        [], False,
        "本次返回测试科室及联系电话，商品编码用于识别对象，不是统计指标。",
    ),
    (
        "分析各经销商合作医院数",
        [{"经销商": "甲", "合作医院数": 10}, {"经销商": "乙", "合作医院数": 8}],
        [{"name": "cooperating_hospital_count"}], False,
        "甲比乙多2家合作医院，但可能存在重叠，不能据此确定区域去重医院总数。",
    ),
    (
        "分析经销商销售数据",
        [{"经销商": "甲", "销售额": 100}, {"经销商": "乙", "销售额": 50}],
        [{"name": "sales_total"}], True,
        "两条预览记录金额合计150，仅代表当前预览，不是全量销售额。",
    ),
    (
        "比较覆盖率",
        [{"区域": "甲", "覆盖率": 0.5}, {"区域": "乙", "覆盖率": 0.3}],
        [{"name": "coverage"}], False,
        "甲比乙高20个百分点；缺少分子分母，不能确定合并后的总体覆盖率。",
    ),
    (
        "解释科室领用量差异",
        [{"科室": "ICU", "领用量": 120}, {"科室": "测试科室", "领用量": 30}],
        [{"name": "usage"}], False,
        "ICU领用量较高，但这不能证明具体治疗场景或采购原因。",
    ),
])
async def test_guidance_keeps_task_data_interface_and_report_unchanged(
    question, rows, metrics, sample_only, report
):
    calls = []

    async def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "claims": [{"statement": report}]
            }, ensure_ascii=False)}}]
        })

    request = CanonicalAnalysisRequest(
        conversation_id="synthetic-insight-boundaries", tenant_id="test", user_id="test",
        original_question=question, rewritten_question=question,
        primary_intent=PrimaryIntent.METRIC_QUERY,
    )
    facts = {
        "query_data": {"rows": rows, "sample_only": sample_only},
        "executed_query": {"asl": {"metrics": metrics, "time_context": None}},
    }
    original = deepcopy(facts)
    source = AnalysisOutput(answer="合成测试数据", method="test", facts=facts, warnings=[])
    settings = Settings(env="test", intent_model_api_key="test-key", analysis_synthesis_max_retries=0)
    text, output = await QwenAnalysisSynthesizer(
        settings, httpx.MockTransport(handler)
    ).synthesize(request, source, [])

    assert len(calls) == 1  # No extra validation/rewrite/model calls.
    assert text == report
    assert output.claims[0].statement == report
    assert facts == original
    body = calls[0]
    payload = json.loads(body["messages"][1]["content"])
    assert payload["facts"] == original
    assert payload["completed_question"] == question
    assert body["response_format"] == {"type": "json_object"}
    assert "tools" not in body
    prompt = body["messages"][0]["content"]
    assert "不仅凭意图标签或某个词判断" in prompt
    assert "即使全是数字也不是可计算指标" in prompt
    assert "覆盖率、平均值不能直接相加" in prompt
    assert "不能相加当成区域去重医院总数" in prompt
    assert "分母为零或未知时不计算" in prompt
    assert "只能代表该部分，不能称为总体" in prompt
    assert "标注【推测】" in prompt and "不是编造外部背景的许可" in prompt
    assert "不生成图表" in prompt and "不要结论先行" in prompt
    assert "八百至一千五百字" in prompt
    assert "need_chart" not in prompt
    assert "【概况总结】" not in prompt
