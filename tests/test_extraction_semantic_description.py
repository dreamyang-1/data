import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.planning import task_dag
from app.adapters.asl_notices import binding_notices


def test_user_prompt_contains_entire_maintained_description():
    source = Path(__file__).resolve().parents[1] / "语义描述文件.md"
    description = source.read_text(encoding="utf-8-sig").strip()
    prompt = task_dag.extraction_user_prompt("查询商品销售额")
    assert description in prompt
    assert prompt.endswith("【本次补全后的问题】\n查询商品销售额")


def test_missing_optional_description_preserves_question(monkeypatch, tmp_path):
    monkeypatch.setattr(task_dag, "_USER_SEMANTIC_DESCRIPTION_PATH", tmp_path / "absent.md")
    assert task_dag.extraction_user_prompt("查询销售额") == "查询销售额"


@pytest.mark.asyncio
async def test_real_model_request_uses_user_description_and_keeps_roles_distinct():
    captured = {}
    async def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "task_structure": "SINGLE_TASK", "tasks": [], "single_task_intent": "METRIC_QUERY",
            "single_task_extraction": {"意图": "统计查询", "实体": ["商品", "经销商"],
                "指标": [{"name": "含税销售总额"}], "过滤条件": [
                    {"field": "商品品牌", "op": "IN", "value": ["万益特", "贝朗"]},
                ]},
        }, ensure_ascii=False)}}]})
    planner = task_dag.MultiQuestionPlanner(Settings(
        _env_file=None, env="test", intent_model_api_key=SecretStr("fixture-key"),
        multi_question_model_enabled=True,
    ), transport=httpx.MockTransport(handler))
    outcome = await planner.plan("查询万益特或贝朗的商品销售额", semantic_context="# 业务语义规范\n调用方补充")
    system, user = captured["messages"]
    assert "调用方补充" in system["content"]
    assert "实体表示本次查询涉及" in system["content"]
    assert "不能拆成同时满足的多个等号" in system["content"]
    assert user["role"] == "user"
    assert "商品科室关联" in user["content"]
    assert "## 四、常见问题映射示例" in user["content"]  # no truncation
    mentions = task_dag.parse_parameter_mentions(list(outcome.single_parameters))
    assert {"text": "商品", "role_hint": "实体"} in mentions
    assert {"text": "含税销售总额", "role_hint": "指标"} in mentions
    assert {"text": "万益特", "role_hint": "商品品牌"} in mentions
    assert outcome.single_structured["过滤条件"][0]["value"] == ["万益特", "贝朗"]


def test_preserved_set_notice_discloses_actual_behavior():
    messages = binding_notices([{"type": "PRESERVE_SET_FILTER", "mention": "万益特",
        "reason": "CROSS_FIELD_MATCH", "source": "SURFACE_MENTION_RECALL"}])
    assert len(messages) == 1
    assert "保留原有 IN/NOT IN" in messages[0]
    assert "漏匹配" in messages[0]
    assert "本次未应用" not in messages[0]
