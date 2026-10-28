"""Exercise the real main dispatch and binder without vector/LLM clients."""
import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace, ModuleType

import pytest
import structured_binding


class Bound(Exception):
    def __init__(self, value):
        self.value = value


def main_dispatch(monkeypatch, *, generated):
    tree = ast.parse((Path(__file__).parents[1] / 'agent.py').read_text('utf8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    namespace = {'json': json, '_normalize_business_domain_scope': lambda one, many: many or [],
        'canonicalize_structured_mentions': lambda *args: [],
        '_recover_question_asl': generated, 'embed_query': lambda _: [],
        '_get_chat_model': lambda: SimpleNamespace(),
        '_known_physical_fields': lambda _: {'sales.created_date', 'sales.amount'},
        '_detail_projection_subject_candidate': lambda *args: None}
    class Builder:
        def __init__(self, *args, **kwargs):
            assert kwargs['semantic_model_id'] == 81 and kwargs['business_domain_ids'] == [205]
        def retrieve(self, text):
            return {}
    namespace['PromptBuilder'] = Builder
    mysql = ModuleType('mysql_tool')
    mysql.resolve_scoped_dictionary_keys = lambda *args: None
    monkeypatch.setitem(sys.modules, 'mysql_tool', mysql)
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'agent.py', 'exec'), namespace)
    return namespace['main']


def test_structured_dispatch_never_reextracts_lossy_question(monkeypatch):
    extraction = {'意图': '对比分析', '指标': [{'name': '销售额'}], '维度': ['业务日期'],
        '排序': [{'field': '业务日期', 'order': 'asc'}],
        '时间粒度': {'unit': '月', 'time_range': '2025-11-01至2025-12-31'}}
    def binding(actual, *args, **kwargs):
        assert actual == extraction
        raise Bound(actual)
    monkeypatch.setattr(structured_binding, 'bind', binding)
    main = main_dispatch(monkeypatch, generated=lambda *args, **kwargs: pytest.fail('must not re-extract'))
    with pytest.raises(Bound):
        main('仅查询11月，按销售额排序', completed_question='错误的一月范围', store=object(),
            semantic_model_id=81, business_domain_ids=[205], structured_extraction=extraction)


def test_unstructured_dispatch_preserves_question_generation(monkeypatch):
    def generate(question, **kwargs):
        assert question == '完整问题' and kwargs['domain_ids'] == [205]
        raise Bound(question)
    main = main_dispatch(monkeypatch, generated=generate)
    with pytest.raises(Bound):
        main('原问题', completed_question='完整问题', store=object(), semantic_model_id=81, business_domain_ids=[205])


@pytest.mark.parametrize('period,start,end', [
    ('2025-11-01至2025-12-31', '2025-11-01', '2025-12-31'),
    ('2025-09-01至2025-12-31', '2025-09-01', '2025-12-31'),
])
def test_real_binder_retains_full_time_window_month_grain_and_time_sort(period, start, end):
    obj = SimpleNamespace
    attrs = [{'attr_name': '业务日期', 'field_mapping': 'sales.created_date', 'data_type': 'date'}]
    knowledge = {'entities': [obj(metadata={'entity_code': 'sales', 'entity_name': '销售订单', 'attributes': attrs})],
        'metrics': [obj(metadata={'metric_code': 'amount', 'metric_name': '销售额', 'entity_code': 'sales',
            'time_anchor': 'sales.created_date'})], 'dimensions': [], 'entity_attribute_values': [],
        '_vector_authorized_fields': ['sales.created_date']}
    extraction = {'展示字段': [], '实体': ['销售订单'], '指标': [{'name': '销售额'}], '维度': ['业务日期'],
        '过滤条件': [], '排序': [{'field': '业务日期', 'order': 'asc'}],
        '时间粒度': {'unit': '月', 'time_range': period, 'anchor': '业务日期'}}
    selected = {'subject': 'sales', 'metrics': [{'index': 0, 'key': 'amount'}],
        'dimensions': [{'index': 0, 'key': 'sales.created_date'}],
        'sort': [{'index': 0, 'key': 'sales.created_date'}], 'time': {'anchor': 'sales.created_date'}}
    model = obj(invoke=lambda messages: obj(content=json.dumps(selected)))
    result, _ = structured_binding.bind(extraction, knowledge, model)
    assert not result['ambiguity']
    assert result['time_context']['start'] == start and result['time_context']['end'] == end
    assert result['dimensions'][0]['granularity'] == 'month'
    assert result['sort']['field'] == 'sales.created_date'
    assert result['sort']['direction'] == 'ASC'
