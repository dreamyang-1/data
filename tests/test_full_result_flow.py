"""Full SQL/derived data is separate from a twenty-row presentation."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import json

import pytest

from app.domain.models import CanonicalAnalysisRequest, ChatRequest, DataQueryResult, Dataset, PrimaryIntent, TrustedIdentity
from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.presentation.root_report import preview_result_tables
from app.services.progress import progress_scope
from test_analysis_orchestration import service, SmallDatasetStore, ReportExporterStub
from test_analysis_synthesis import settings, transport_for


@pytest.mark.asyncio
@pytest.mark.parametrize('deferred', [False, True])
@pytest.mark.parametrize('delivery', ['file', 'export_error', 'local'])
async def test_full_rows_reach_analysis_storage_and_only_twenty_reach_table(deferred, delivery, monkeypatch):
    # Semantic descriptions are an independent remote dependency. Exercise real
    # model serialization without reaching the business configuration service.
    import inspect
    import app.analysis.synthesis as synthesis
    reference = AsyncMock(return_value={}) if inspect.iscoroutinefunction(synthesis._semantic_reference) else lambda *a, **kw: {}
    monkeypatch.setattr(synthesis, '_semantic_reference', reference)
    rows = [{'经销商名称': f'DEALER-{n:04d}', '合作时长': n % 12} for n in range(2154)]
    request = CanonicalAnalysisRequest(
        conversation_id='full-flow', application_id='app', tenant_id='tenant', user_id='user',
        original_question='列出各经销商合作时长', primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity='dealer', fields=['经销商名称', '合作时长'], semantic_model_id=1,
    )
    chat = ChatRequest(application_id='app', conversation_id=request.conversation_id,
                       message_id='m1', question=request.original_question, semantic_model_id=1)
    chat._dag_defer_insight = deferred
    result = DataQueryResult(
        asl={'metrics': [], 'dimensions': [{'name':'dealer.name'}, {'name':'dealer.months'}]},
        sql='SELECT name, months FROM dealer',
        dataset=Dataset(columns=list(rows[0]), rows=rows, row_count=len(rows),
                        snapshot_id='full', data_as_of=datetime.now(timezone.utc), quality_status='PASS'),
        result_file_url='https://files.example/full.xlsx' if delivery=='file' else None,
        result_export_error='完整附件导出失败：文件生成或上传失败。' if delivery=='export_error' else None,
    )
    calls=[]
    model=QwenAnalysisSynthesizer(settings(), transport_for({'claims':[{'statement':'完整结果已整理。'}]}, calls))
    store=SmallDatasetStore(); exporter=ReportExporterStub()
    agent=service(synthesizer=model, dataset_store=store, report_exporter=exporter)
    agent._import_query_result_file=AsyncMock(side_effect=AssertionError('full rows must not reimport raw export'))
    events=[]
    with progress_scope(events.append):
        response=await agent._complete_query_result(chat, TrustedIdentity(tenant_id='tenant', user_id='user'), request, result)
    assert len(store.rows)==2154 and store.rows[-1]==rows[-1]
    facts=(chat._dag_deferred_insight['facts'] if deferred else json.loads(calls[0]['messages'][1]['content'])['facts'])
    assert facts['query_data']['rows']==rows
    assert facts['query_data']['sample_only'] is False
    if deferred:
        await model.synthesize_combined(request.original_question, [chat._dag_deferred_insight])
        combined=json.loads(calls[0]['messages'][1]['content'])
        assert combined['tasks'][0]['facts']['query_data']['rows']==rows
    query=next(item.payload for item in response.evidence if item.kind=='QUERY_RESULT')
    assert query['returned_row_count']==2154 and not query['truncated']
    assert 'DEALER-0019' in response.answer and 'DEALER-0020' not in response.answer
    assert response.answer.count('| DEALER-')==20
    assert '2154' in response.answer
    assert '不能代替' not in ' '.join(response.reliability.warnings)
    assert len(result.dataset.rows)==2154
    if delivery=='file': assert response.result_file_url.endswith('/full.xlsx')
    if delivery=='export_error':
        assert '附件导出失败' in response.answer and not response.files
        assert any(item.kind=='ANALYSIS_RESULT' for item in response.evidence)
    if delivery=='local' and not deferred:
        assert exporter.exported_dataset_id==response.dataset_id and response.files
    stages=[event['stage'] for event in events]
    if deferred:
        # Root-goal validation is emitted by the parent after gathering all
        # child outputs. The data/internal-check contracts stay unchanged.
        assert 'RELIABILITY_CHECK' not in stages and 'INSIGHT_ANALYSIS' not in stages
        assert response.reliability is not None
    else:
        assert stages.index('DATA_RETRIEVAL') < stages.index('RELIABILITY_CHECK') < stages.index('INSIGHT_ANALYSIS')


def test_rendered_table_cap_leaves_non_table_prose_and_separate_tables_intact():
    table='| name |\n| --- |\n'+'\n'.join(f'| R-{i} |' for i in range(31))
    output=preview_result_tables('summary\n\n'+table+'\n\nnext\n\n'+table)
    assert output.count('| R-19 |')==2 and '| R-20 |' not in output
    assert 'summary' in output and 'next' in output and output.count('共 31 行')==2


@pytest.mark.asyncio
async def test_dependent_filter_reads_and_returns_all_matching_rows(monkeypatch):
    from test_dataset_lineage_contract import setup, save, remember, request as followup_request
    from app.services.dataset_followup import scope_for_request
    agent=setup();agent.settings.data_query_max_rows=20
    rows=[{'经销商名称':f'D-{i}', '合作时长':i % 12} for i in range(2154)]
    reference=save(agent, rows)
    await remember(agent,reference)
    request=followup_request('筛选合作时长大于3个月的经销商名单')
    request.source_dataset_id=reference.dataset_id
    monkeypatch.setattr('app.services.orchestrator.plan_dataset_followup',lambda *a,**kw:
                        {'type':'filter','field':'合作时长','operator':'gt','value':3})
    result,dataset_id=await agent._try_dataset_followup(request)
    expected=[row for row in rows if row['合作时长']>3]
    assert result.dataset.rows==expected and len(expected)>1000
    assert result.dataset.total_row_count==len(expected) and not result.dataset.truncated
    refs=await agent.sessions.get_recent_dataset_references(request.tenant_id,request.user_id,
        request.application_id,request.conversation_id,limit=20)
    from app.services.dataset_followup import restore_reference
    saved=next(restore_reference(item) for item in refs if item['dataset_id']==dataset_id)
    assert list(agent.dataset_store.load_dataset(saved,current_scope=scope_for_request(request)).rows)==expected


@pytest.mark.asyncio
async def test_complete_cleaned_list_exports_cleaned_not_raw_rows():
    store=SmallDatasetStore();exporter=ReportExporterStub()
    agent=service(dataset_store=store,report_exporter=exporter)
    rows=[{'医院名称':f'H-{i}'} for i in range(25)]
    request=CanonicalAnalysisRequest(conversation_id='cleanup',tenant_id='t',user_id='u',
        application_id='app',original_question='医院名单',primary_intent=PrimaryIntent.DETAIL_QUERY,entity='hospital')
    chat=ChatRequest(application_id='app',conversation_id='cleanup',message_id='m',question='医院名单',semantic_model_id=1)
    result=DataQueryResult(asl={'metrics':[]},sql='SELECT name FROM hospital',result_file_url='https://files.example/raw.xlsx',
        dataset=Dataset(columns=['医院名称'],rows=[*rows,*rows,{'医院名称':None}],row_count=51,
                        snapshot_id='raw',data_as_of=datetime.now(timezone.utc)))
    response=await agent._complete_query_result(chat,TrustedIdentity(tenant_id='t',user_id='u'),request,result)
    assert list(store.rows)==rows and response.result_file_url is None
    assert exporter.exported_dataset_id==response.dataset_id and response.files
    assert all(file.download_url!='https://files.example/raw.xlsx' for file in response.files)
    assert response.answer.count('| H-')==20 and '25' in response.answer


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [['甲医院', '甲医院', '—'], [None, '—']])
async def test_cleaned_partial_data_does_not_depend_on_display_message(monkeypatch, values):
    from test_result_cleanup import test_partial_preview_cleanup_never_imports_raw_file_or_claims_empty_database
    monkeypatch.setattr('app.services.orchestrator.cleanup_message', lambda result: '')
    await test_partial_preview_cleanup_never_imports_raw_file_or_claims_empty_database(values)


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_scope', [False, True])
async def test_cached_root_material_loads_full_rows_only_with_current_scope(wrong_scope):
    from test_dataset_lineage_contract import setup, save, remember, IDENTITY
    from app.domain.models import AgentResponse
    agent=setup()
    rows=[{'地区':f'R-{i}','销售额':i} for i in range(2154)]
    reference=save(agent,rows);await remember(agent,reference)
    response=AgentResponse(request_id='00000000-0000-0000-0000-000000000001',
        conversation_id='lineage-conversation',status='COMPLETED',intent=PrimaryIntent.METRIC_QUERY,
        intent_source='test',answer='页面摘要',dataset_id=reference.dataset_id)
    chat=ChatRequest(application_id='lineage-app',conversation_id='root',message_id='m',
        question='统计销售额',semantic_model_id=82 if wrong_scope else 81)
    materials=[{'task_id':'t1','status':'COMPLETED','facts':{},'warnings':[]}]
    await agent._hydrate_root_query_data(materials,{'t1':response},{'t1':'lineage-conversation'},chat,IDENTITY)
    if wrong_scope:
        assert 'query_data' not in materials[0]['facts'] and materials[0]['warnings']
    else:
        assert materials[0]['facts']['query_data']['rows']==rows
        assert not materials[0]['facts']['query_data']['sample_only']
