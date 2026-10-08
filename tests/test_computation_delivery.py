from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from minio_followup_store import DatasetScope
from app.config import Settings
from app.domain.models import AtomicTask, ChatRequest, TrustedIdentity
from app.services.computation_delivery import aligned_input
from app.services.dataset_followup import plan_dataset_followup
from app.services.orchestrator import DataAnalysisOrchestrator
from app.presentation.root_report import render_root_report
from test_conversation_result_followup import _ResultStore


ORIGINAL = '筛选出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'


@pytest.mark.asyncio
@pytest.mark.parametrize('question,operation_question,field,descending,count', [
    (ORIGINAL, '根据上述结果计算订单平均金额，含税销售总额除以订单笔数，降序取前五名，不再查询数据库', '订单平均金额', True, 5),
    ('列出2025年订单平均金额最低的前三位销售人员及所属公司', '计算订单平均金额，升序取前三名，不再查询数据库', '订单平均金额', False, 3),
    ('筛选2025年含税销售总额最高的前五位销售人员及所属公司', '根据上述结果按含税销售总额选前五名，不再查询数据库', '含税销售总额', True, 5),
    ('查询2025年订单笔数最高的前四位销售人员及所属公司', '根据上述结果按订单笔数最高取前四名，不再查询数据库', '订单笔数', True, 4),
])
async def test_original_and_three_similar_deliver_real_ranked_artifacts(question, operation_question, field, descending, count):
    store = _ResultStore()
    scope = DatasetScope(tenant_id='tenant', user_id='user', application_id='app', conversation_id='test')
    rows = [{'姓名':'同名', '人员（编码）':f'{i:03}', '所属公司':f'公司{i%3}',
             '含税销售总额':float(i*i*100), '订单笔数':i} for i in range(1,25)]
    reference = store.save_dataset(scope=scope, columns=list(rows[0]), rows=rows, snapshot_id='',
        data_as_of=datetime.now(timezone.utc), source_type='QUERY_RESULT', source_ref='sql', semantic_model_id=81)
    service = object.__new__(DataAnalysisOrchestrator)
    service.settings = Settings(_env_file=None, env='test')
    service.dataset_store = store
    service.sessions = SimpleNamespace(put_dataset_reference=AsyncMock())
    service._latest_task_dataset_reference = AsyncMock(return_value=reference.to_dict())
    service.chat_responder = SimpleNamespace(respond=AsyncMock(side_effect=AssertionError('不得用模型文字计算')))
    chat = ChatRequest(semantic_model_id=81, application_id='app', conversation_id='test', message_id=str(uuid4()), question=question)
    task = AtomicTask(task_id='final', question=operation_question, depends_on=['source'])
    result = await service._respond_pure_computation(chat, TrustedIdentity(tenant_id='tenant', user_id='user'), task, {})
    assert result.status == 'COMPLETED'
    final = store.items[result.dataset_id]
    assert len(final.rows) == count
    values = [row[field] for row in final.rows]
    assert values == sorted(values, reverse=descending)
    assert all(row['所属公司'] for row in final.rows)
    if field in ('订单平均金额', '客单价'):
        assert all(row[field] == row['含税销售总额']/row['订单笔数'] for row in final.rows)
    assert chat._dag_deferred_insight['facts']['query_data']['rows'] == list(final.rows)
    materials = [{'task_id':'source', 'status':'COMPLETED', 'question':'原始统计',
                  'presentation':{'table':'| 原始89行 |\n| --- |\n| 不应最终返回 |'}},
                 {**chat._dag_deferred_insight, 'task_id':'final', 'status':'COMPLETED', 'depends_on':['source']}]
    answer, selected = render_root_report(question, materials, {'result_task_ids':['source']})
    assert selected == ['final']
    assert '原始89行' not in answer and '所属公司' in answer
    service.chat_responder.respond.assert_not_called()


def test_alignment_uses_codes_not_names_and_preserves_companions():
    a = SimpleNamespace(columns=('姓名','人员（编码）','含税销售总额'))
    b = SimpleNamespace(columns=('姓名','人员（编码）','所属公司','订单笔数'))
    cols, rows = aligned_input([(a,[{'姓名':'同名','人员（编码）':'001','含税销售总额':60},
                                   {'姓名':'同名','人员（编码）':'002','含税销售总额':30}]),
                               (b,[{'姓名':'同名','人员（编码）':'002','所属公司':'乙','订单笔数':1},
                                   {'姓名':'同名','人员（编码）':'001','所属公司':'甲','订单笔数':2}])])
    assert rows[0]['所属公司'] == '甲' and rows[1]['所属公司'] == '乙'
    assert '订单笔数' in cols


@pytest.mark.parametrize('right', [
    [{'编号':'1','销量':3},{'编号':'1','销量':4}],
    [{'编号':'2','销量':3}],
    [{'编号':None,'销量':3}],
])
def test_alignment_rejects_nonunique_missing_or_different_scope(right):
    with pytest.raises(ValueError):
        aligned_input([(SimpleNamespace(columns=('编号','金额')), [{'编号':'1','金额':3}]),
                       (SimpleNamespace(columns=('编号','销量')),right)])


def test_missing_numeric_inputs_do_not_invent_an_average():
    assert plan_dataset_followup('计算订单平均金额前五名', ['姓名','所属公司'], [{'姓名':'甲','所属公司':'乙'}]) is None


@pytest.mark.asyncio
async def test_unavailable_input_is_not_completed_by_prose():
    service = object.__new__(DataAnalysisOrchestrator)
    service.dataset_store = None
    service._latest_task_dataset_reference = AsyncMock(return_value=None)
    chat = ChatRequest(semantic_model_id=81, application_id='app', conversation_id='test', message_id='missing',question='计算订单平均金额')
    result = await service._respond_pure_computation(chat, TrustedIdentity(tenant_id='tenant',user_id='user'),
        AtomicTask(task_id='final',question='计算订单平均金额，不再查询数据库',depends_on=['source']), {})
    assert result.status == 'FAILED' and result.dataset_id is None


@pytest.mark.asyncio
async def test_dag_routes_declared_calculation_before_entity_alias_enrichment():
    from test_task_dag import _StubOrchestrator, _Classifier
    from app.adapters import build_mock_adapters
    from app.stores import InMemorySessionStore
    from app.planning import MultiQuestionPlanner
    from app.domain.models import AgentResponse, PrimaryIntent, TaskPlan
    settings=Settings(_env_file=None,env='test',analysis_synthesis_enabled=False,multi_question_model_enabled=False)
    service=_StubOrchestrator(settings=settings,task_planner=MultiQuestionPlanner(settings),
        classifier=_Classifier(),adapters=build_mock_adapters(),sessions=InMemorySessionStore(7200,7200))
    async def query(child,identity):
        return AgentResponse(request_id=uuid4(),conversation_id=child.conversation_id,status='COMPLETED',
            intent=PrimaryIntent.METRIC_QUERY,answer='销售员姓名、国药公司名称、订单笔数、含税销售总额')
    service._handle=query
    service._compile_dependency_constraints=AsyncMock(side_effect=AssertionError('纯计算不得转为数据库关联查询'))
    service._respond_pure_computation=AsyncMock(return_value=AgentResponse(request_id=uuid4(),
        conversation_id='test',status='COMPLETED',intent=PrimaryIntent.COMPARISON_ANALYSIS,answer='已完成实际计算'))
    response=await service._handle_task_plan(ChatRequest(semantic_model_id=81,application_id='app',
        conversation_id='test',message_id='routing',question=ORIGINAL),TrustedIdentity(tenant_id='tenant',user_id='user'),
        TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='q',question='统计2025年各业务员的销售额和订单笔数'),
            AtomicTask(task_id='c',question='根据上述任务的查询结果计算订单平均金额，降序取前五名销售人员及其销售公司，不再查询数据库',depends_on=['q'])]))
    service._respond_pure_computation.assert_awaited_once()
    service._compile_dependency_constraints.assert_not_called()
    assert response.task_results[-1].status=='COMPLETED'
