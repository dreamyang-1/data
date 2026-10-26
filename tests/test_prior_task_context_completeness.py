"""任务规划历史上下文：可证明的完整小名单可复用，预览及截断不可冒充完整。"""
from copy import deepcopy

import pytest

from app.planning.compat import render_prior_task_table_context


def table(summary):
    return {
        "question": "2025年销售额最高的前三家公司是哪几家？",
        "tasks": [{"名称": "查询销售额前三家公司", "是否查询数据库": "是", "结果概要": summary}],
    }


def complete_summary():
    return {
        "columns": ["公司名称", "销售额", "省份", "城市", "公司编码"],
        "row_count": 3,
        "sample_is_complete": True,
        "sample": [
            {"公司名称": f"公司{name}", "销售额": 100 - n, "省份": "测试省", "城市": "测试市", "公司编码": f"00{n}"}
            for n, name in enumerate("甲乙丙", 1)
        ],
    }


def test_proven_complete_top_three_keeps_every_object_and_join_key():
    source = table(complete_summary())
    before = deepcopy(source)
    text = render_prior_task_table_context(source)
    assert "（结果列：" in text
    assert "，全部结果已完整提供，公司名称=公司甲" in text
    for code in ("001", "002", "003"):
        assert f"公司编码={code}" in text
    assert "只有一个新增查询目标时可只规划该查询" in text
    assert source == before


@pytest.mark.parametrize("proof,count", [(None, 3), (False, 3), (True, 30), (True, True)])
def test_sample_needs_existing_proof_and_matching_count(proof, count):
    summary = complete_summary()
    summary["sample_is_complete"] = proof
    summary["row_count"] = count
    text = render_prior_task_table_context(table(summary))
    task_line = next(line for line in text.splitlines() if line.startswith("- "))
    assert "以下仅为采样，不能作为完整对象名单" in task_line
    assert "全部结果已完整提供" not in task_line
    assert "把采样里的参数值逐个写进" not in text
    assert "优先于通用拆分规则" not in text


def test_renderer_ten_row_cap_does_not_keep_complete_marker():
    summary = complete_summary()
    summary["sample"] = [{"公司编码": f"{n:03d}"} for n in range(11)]
    summary["row_count"] = 11
    text = render_prior_task_table_context(table(summary))
    task_line = next(line for line in text.splitlines() if line.startswith("- "))
    assert "公司编码=009" in task_line and "公司编码=010" not in task_line
    assert "以下仅为采样" in task_line
    assert "全部结果已完整提供" not in task_line


def test_oversized_complete_task_is_omitted_atomically_with_notice():
    summary = complete_summary()
    summary["sample"][0]["公司名称"] = "大公司" * 2000
    text = render_prior_task_table_context(table(summary), budget=1000)
    assert len(text) <= 1000
    assert "部分历史上下文因长度限制已省略" in text
    assert "公司编码=001" not in text
    assert not any(line.startswith("- ") for line in text.splitlines())
    assert text.endswith("不得编造未展示值或沿用其他模型、业务域的结果。")


@pytest.mark.parametrize("budget", [0, 20, 100, 500, 3500])
def test_budget_never_truncates_a_complete_claim_or_rule(budget):
    text = render_prior_task_table_context(table(complete_summary()), budget=budget)
    assert len(text) <= budget
    if "公司编码=001" in text:
        assert "公司编码=003" in text
    elif text:
        assert "不得据此复用具体名单" in text or "部分历史上下文因长度限制已省略" in text


@pytest.mark.parametrize("sample", ["invalid", [None], [{"公司名称": "公司甲"}, "invalid"]])
def test_legacy_malformed_sample_does_not_crash_or_claim_complete(sample):
    summary = complete_summary()
    summary["sample"] = sample
    text = render_prior_task_table_context(table(summary))
    task_line = next(line for line in text.splitlines() if line.startswith("- "))
    assert "以下仅为采样" in task_line
    assert "全部结果已完整提供" not in task_line


@pytest.mark.parametrize("value", [None, {}, {"tasks": []}])
def test_absent_history_does_not_invent_context(value):
    assert render_prior_task_table_context(value) == ""
