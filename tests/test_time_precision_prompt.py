from app.planning.task_dag import _SINGLE_SYSTEM_PROMPT, extraction_parser_prompt


def test_real_extraction_prompt_preserves_all_grains_and_explicit_anchor():
    prompt = extraction_parser_prompt()
    for grain in ('秒', '分钟', '小时', '日', '周', '月', '季度', '半年', '年'):
        assert grain in prompt
    assert 'anchor' in prompt and '发货日期' in prompt
    assert '2025年8月份' in prompt
    assert '财年' in prompt and '工作日' in prompt
    assert prompt in _SINGLE_SYSTEM_PROMPT
