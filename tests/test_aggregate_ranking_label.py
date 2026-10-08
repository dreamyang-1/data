import pytest
from app.analysis.engine import AnalysisError, AnalysisEngine
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, AnalysisOperator, KnowledgeContext, MetricRef
from app.planning.calculation_targets import declared_calculation_targets


@pytest.mark.parametrize('question',[ '计算订单平均金额，按其降序取前五名，不再查询数据库',
    '根据上述结果计算各销售人员的订单平均金额，降序取前五名，不再查询数据库',
    '根据上述任务计算订单平均金额（含税销售总额除以订单笔数），降序取前五名，不再查询数据库',
    '含税销售总额除以订单笔数得到订单平均金额，降序取前五名，不再查询数据库',
    '算出平均订单金额，取前五名，不再查询数据库'])
def test_declared_calculation_does_not_need_possessive_de(question):
    assert '订单平均金额' in declared_calculation_targets(question)


@pytest.mark.parametrize('rank_text,source_rank',[('降序取前五名','最高的前五位'),('升序取前三位','最低的前三位'),('从高到低取前5名','最高的前5位')])
def test_calculation_rank_retains_original_rank_goal(rank_text,source_rank):
    from app.planning.task_dag import MultiQuestionPlanner
    from app.domain.models import AtomicTask,TaskPlan
    plan=TaskPlan(planner='STRUCTURED_MODEL',tasks=[AtomicTask(task_id='q',question='统计2025年各销售人员的含税销售总额和订单笔数，并展示所属公司'),
        AtomicTask(task_id='c',question='计算订单平均金额，'+rank_text+'销售人员及所属公司，不再查询数据库',depends_on=['q'])])
    MultiQuestionPlanner.validate(plan,source_question='筛选2025年订单平均金额'+source_rank+'销售人员，以及所属公司')


@pytest.mark.parametrize('dimension',['业务员','销售员','销售人员','salesperson'])
def test_company_companion_does_not_make_ranked_person_ambiguous(dimension):
    request=CanonicalAnalysisRequest(original_question='按销售额排销售人员',
        conversation_id='rank',tenant_id='t',user_id='u',
        primary_intent=PrimaryIntent.METRIC_QUERY,dimensions=[dimension])
    assert AnalysisEngine._ranking_label_column(request,
        ['销售员姓名','业务员（编码）','国药公司名称','销售额'],'销售额')=='销售员姓名'


def test_two_unrelated_labels_remain_ambiguous():
    request=CanonicalAnalysisRequest(original_question='排名',
        conversation_id='rank',tenant_id='t',user_id='u',primary_intent=PrimaryIntent.METRIC_QUERY)
    with pytest.raises(AnalysisError):
        AnalysisEngine._ranking_label_column(request,['姓名','公司名称','金额'],'金额')


def test_ranked_table_retains_requested_company_and_identity():
    request=CanonicalAnalysisRequest(original_question='销售额最高的人员及所属公司',
        conversation_id='rank',tenant_id='t',user_id='u',primary_intent=PrimaryIntent.METRIC_QUERY,
        dimensions=['业务员'],operators=[AnalysisOperator.TOP_N],ranking_limit=2,
        metrics=[MetricRef(input='销售额',canonical_name='销售额')],
        asl_template={'display_fields':[{'name':'company.name','alias':'公司名称'}]})
    rows=[{'销售员姓名':'甲','业务员（编码）':'00110000','公司名称':'公司甲','销售额':30},
          {'销售员姓名':'乙','业务员（编码）':'2','公司名称':'公司乙','销售额':20}]
    result=AnalysisEngine().analyze_ranking(request,list(rows[0]),rows,KnowledgeContext(query=''))
    assert '公司名称' in result.answer and '业务员（编码）' in result.answer
    assert result.facts['rankings'][0]['profile']['公司名称']=='公司甲'
    assert '00110000' in result.answer and '110,000' not in result.answer
