import copy
from types import SimpleNamespace

import pytest

from app.analysis import units
from app.presentation.root_report import render_root_report
from app.services.orchestrator import DataAnalysisOrchestrator


REFERENCE = {'available': True, 'content': '''| 指标名称 | 别名 | 业务口径 | 单位 |
| --- | --- | --- | --- |
| 含税销售总额 | 销售额,销售总金额 | 金额合计 | 元 |
| 订单笔数 | 订单数 | 去重订单数 | 笔 |
| 平均每单销售金额 | 订单平均金额 | 金额除以订单数 | 元 |
| 无量纲 | 无 | 无单位 | 无 |
'''}


def test_units_are_read_from_current_file_including_aliases_without_guessing():
    result = units.metric_units_from_reference(REFERENCE)
    assert units.unit_for_column(result, '含税销售总额') == '元'
    assert units.unit_for_column(result, '销售额') == '元'
    assert units.unit_for_column(result, '订单数') == '笔'
    assert units.unit_for_column(result, '平均每单销售金额') == '元'
    assert units.unit_for_column(result, '无量纲') == ''
    assert units.unit_for_column(result, '未登记金额') == ''


@pytest.mark.parametrize('available', [False, True])
def test_unavailable_or_non_metric_table_does_not_supply_units(available):
    assert units.metric_units_from_reference({'available': available,
        'content': '| 实体 | 单位 |\n| --- | --- |\n| 公司 | 元 |'}) == {}


@pytest.mark.asyncio
async def test_orchestrator_uses_shared_document_loader_and_current_model(monkeypatch):
    calls = []
    async def load(settings, model_id):
        calls.append(model_id)
        return REFERENCE if model_id == 130 else {'available': True,
            'content': REFERENCE['content'].replace('| 元 |', '| 万元 |')}
    monkeypatch.setattr('app.domain.semantic_description.load_semantic_description', load)
    owner = SimpleNamespace(settings=object())
    first = await DataAnalysisOrchestrator._metric_units(owner, 130)
    second = await DataAnalysisOrchestrator._metric_units(owner, 131)
    assert first['含税销售总额'] == '元'
    assert second['含税销售总额'] == '万元'
    assert calls == [130, 131]


@pytest.mark.parametrize('status', ['COMPLETED', 'PARTIAL_SUCCESS'])
def test_root_report_updates_pre_rendered_computed_table_without_changing_values(status):
    table = '| 业务员 | 含税销售总额 | 订单笔数 | 平均每单销售金额 |\n| --- | --- | --- | --- |\n| A | 1,200 | 3 | 400 |'
    material = {'task_id': 'task-3', 'question': '最高平均金额', 'status': status,
        'depends_on': ['task-1', 'task-2'], 'presentation': {'table': table},
        'facts': {'metric_units': {'含税销售总额': '元', '订单笔数': '笔', '平均每单销售金额': '元'}}}
    saved = copy.deepcopy(material)
    answer, selected = render_root_report('最高平均金额', [material], {})
    assert '含税销售总额（元）' in answer
    assert '订单笔数（笔）' in answer
    assert '平均每单销售金额（元）' in answer
    assert '| A | 1,200 | 3 | 400 |' in answer
    assert material == saved and selected == ['task-3']


def test_multiple_tables_and_existing_units_preserve_body_and_prose():
    original = '说明\n\n| 含税销售总额（元） |\n| --- |\n| 10 |\n\n| 订单笔数 |\n| :---: |\n| 2 |'
    mapping = units.metric_units_from_reference(REFERENCE)
    result = units.table_with_units(original, mapping)
    assert result.count('含税销售总额（元）') == 1
    assert '订单笔数（笔）' in result
    assert result.startswith('说明\n\n') and '| 10 |' in result and '| 2 |' in result
    assert units.table_with_units(result, mapping) == result
    assert units.table_with_units(original, {}) == original
