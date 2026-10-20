"""Regression for a completed price calculation followed by wrong LLM statistics."""
from types import SimpleNamespace
import pytest
from app.services.computation_delivery import computation_material,metric_distributions


def test_sample_mean_and_median_are_computed_before_narration():
    rows=[{'订单均价':value} for value in [10,*([100]*4),*([300]*8)]]
    stats=metric_distributions(rows,['订单均价'])['订单均价']
    assert stats['sample_count']==13 and stats['mean']==pytest.approx(2810/13)
    assert stats['median']==300 and stats['min']==10 and stats['max']==300
    assert stats['q1']==100 and stats['q3']==300
    assert [item['count'] for item in stats['histogram']]==[1,4,0,0,8]
    assert sum(item['percentage'] for item in stats['histogram'])==pytest.approx(100)
    assert stats['mean']<stats['median']


def test_constant_sample_has_one_bin_and_zero_deviation():
    stats=metric_distributions([{'均价':25}]*3,['均价'])['均价']
    assert stats['mean']==stats['median']==25 and stats['population_stddev']==0
    assert stats['histogram']==[{'lower':25,'upper':25,'upper_inclusive':True,'count':3,'percentage':100}]


def test_missing_values_are_disclosed_without_becoming_zero_samples():
    stats=metric_distributions([{'均价':10},{'均价':None},{'均价':30}],['均价'])['均价']
    assert stats['sample_count']==2 and stats['missing_or_non_numeric_count']==1
    assert stats['mean']==stats['median']==20


def test_ordinary_computation_keeps_existing_material_shape():
    material=computation_material('计算订单均价',None,[{'均价':10}],
        {'type':'derive','result_field':'均价'},lambda *args:'table',columns=['均价'])
    assert 'metric_distributions' not in material['facts']
    assert material['presentation']['table']=='table'


def test_distribution_material_contains_statistics_and_full_row_data():
    rendered=[]
    def render(columns,rows):rendered.append((columns,rows));return 'table'
    material=computation_material('分析订单均价的分布',None,[{'均价':10},{'均价':30}],
        {'type':'pipeline','operations':[{'type':'derive','result_field':'均价'}]},render,columns=['均价'])
    assert material['facts']['metric_distributions']['均价']['median']==20
    assert material['facts']['query_data']['rows']==[{'均价':10},{'均价':30}]
    assert rendered[-1][1][0]['算术平均值']==20


@pytest.mark.asyncio
async def test_complete_distribution_facts_reach_combined_analysis_model(monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from app.analysis import synthesis
    from test_analysis_synthesis import settings,transport_for
    monkeypatch.setattr(synthesis,'_semantic_reference',AsyncMock(return_value=''))
    material=computation_material('分析均价分布',None,[{'均价':10},{'均价':30}],
        {'type':'derive','result_field':'均价'},lambda *args:'table',columns=['均价'])
    calls=[]
    await synthesis.QwenAnalysisSynthesizer(settings(),transport_for({'claims':[{'statement':'中位数为20。'}]},calls)).synthesize_combined(
        '分析均价分布',[{'task_id':'calculation',**material}])
    payload=json.loads(calls[0]['messages'][1]['content'])
    facts=payload['tasks'][0]['facts']
    assert facts['metric_distributions']['均价']['mean']==20
    assert facts['metric_distributions']['均价']['median']==20
    assert len(facts['query_data']['rows'])==2
