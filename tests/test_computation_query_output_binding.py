"""Resolve returned aliases from validated query evidence, never row guessing."""
from datetime import datetime,timezone

import pytest

from app.domain.models import AtomicTask,DataQueryResult,Dataset,PlannerExtraction
from test_complex_execution_correctness import service,context,IDENTITY


@pytest.mark.parametrize('db_type',['mysql','mariadb','postgresql','oracle','dm','sqlserver'])
def test_output_roles_follow_the_actual_translator_dialect(db_type):
    import importlib.util
    from pathlib import Path
    from app.services.query_output_bindings import calculation_columns
    root=Path(__file__).resolve().parents[1]
    path=root/'sql-translator/sql_dialects.py'
    if not path.exists():
        path=root.parent/'sql-translator/sql_dialects.py'
    spec=importlib.util.spec_from_file_location('calculation_dialect_contract',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    dialect=module.get_dialect(db_type)
    _,request=context()
    request._planner_extraction=PlannerExtraction(structured={'维度':['经销商']})
    sql='SELECT '+', '.join([
        dialect.select_alias('dealer.name','经销商名称'),
        dialect.select_alias('dealer.code','经销商（编码）'),
        dialect.select_alias(dialect.time_bucket('sales.business_date','month'),'业务日期')])+' FROM sales'
    result=DataQueryResult(asl={'dimensions':[{'name':'sales.business_date','granularity':'month'}],
        'time_context':{'anchor':'sales.business_date'}},sql=sql,
        dataset=Dataset(columns=['经销商名称','经销商（编码）','业务日期'],rows=[],row_count=0,
            snapshot_id='fixture',quality_status='PASS',data_as_of=datetime.now(timezone.utc)))
    assert calculation_columns(request,result)=={'month':['业务日期'],'dimension_identity':{'经销商':'经销商（编码）'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate_time_alias',[False,True])
async def test_monthly_growth_binds_real_query_columns_and_preserves_same_named_identities(duplicate_time_alias):
    agent=service();chat,request=context()
    request.conversation_id='source'
    request._planner_extraction=PlannerExtraction(structured={'维度':['经销商'],
        '时间粒度':{'unit':'月','time_range':'2025年1月至3月'}})
    columns=['经销商名称','经销商（编码）','业务日期','销售总数量']
    rows=[{'经销商名称':'同名组织','经销商（编码）':code,'业务日期':f'2025-{month:02d}','销售总数量':value}
        for code,values in [('A',[100,200,100]),('B',[100,100,100])]
        for month,value in enumerate(values,1)]
    sql="SELECT dealer.name AS `经销商名称`, dealer.code AS `经销商（编码）`, DATE_FORMAT(sales.business_date, '%Y-%m') AS `业务日期`, SUM(sales.qty) AS `销售总数量` FROM sales JOIN dealer ON sales.dealer_code=dealer.code GROUP BY dealer.name,dealer.code,DATE_FORMAT(sales.business_date,'%Y-%m')"
    if duplicate_time_alias:
        columns.append('另一个月份')
        for row in rows:
            row['另一个月份']=row['业务日期']
        sql=sql.replace('SUM(sales.qty)',"DATE_FORMAT(sales.business_date, '%Y-%m') AS `另一个月份`, SUM(sales.qty)")
    result=DataQueryResult(asl={'dimensions':[{'name':'dealer'},{'name':'sales.business_date','granularity':'month'}],
        'time_context':{'anchor':'sales.business_date'}},sql=sql,dataset=Dataset(columns=columns,rows=rows,row_count=len(rows),
        total_row_count=len(rows),snapshot_id='fixture',quality_status='PASS',data_as_of=datetime.now(timezone.utc)))
    await agent._persist_query_dataset(request,result,chat)
    operation={'type':'monthly_growth','field':'销售总数量','period_field':'统计月份',
        'group_by':['经销商'],'method':'arithmetic','period_start':'2025-02','period_end':'2025-03',
        'result_field':'平均增速','format':'percent'}
    task=AtomicTask(task_id='growth',question='按经销商计算平均月度增长率',depends_on=['source'],
        extraction={'是否查询数据库':'否','结果操作':{'source_task_id':'source','operation':operation}})
    response=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    if duplicate_time_alias:
        assert response.status=='FAILED' and not response.dataset_id
    else:
        assert response.status=='COMPLETED'
        actual={row['经销商（编码）']:row['平均增速'] for row in response.evidence[0].payload['preview']}
        assert actual=={'A':'25.00%','B':'0.00%'}
        binding=response.evidence[0].payload['operation']['input_binding']
        assert binding['period_column']=={'declared':'统计月份','actual':'业务日期'}
