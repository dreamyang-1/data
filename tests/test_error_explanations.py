import asyncio
from uuid import uuid4

import httpx
import pytest

from app.adapters.base import AdapterError
from app.adapters.http import PlatformHttpClient
from app.config import Settings
from app.services.error_explanations import explain_binding_reason, explain_dependency_failure
from app.services.orchestrator import DataAnalysisOrchestrator
from test_api import TestClient, build_test_app


@pytest.mark.parametrize('error,expected', [
    (httpx.ConnectError('secret-host'), '未能建立服务连接'),
    (httpx.ReadTimeout('secret-host'), '等待服务回复超时'),
    (ValueError('token=secret'), '服务返回的内容无法读取'),
    (httpx.RemoteProtocolError('secret-host'), '服务连接中途断开'),
])
@pytest.mark.asyncio
async def test_transport_failure_keeps_category(monkeypatch,error,expected):
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,*args,**kwargs):raise error
    monkeypatch.setattr('app.adapters.http.httpx.AsyncClient',Client)
    with pytest.raises(AdapterError) as caught:
        await PlatformHttpClient(Settings(_env_file=None,http_max_retries=0)).post(
            'http://test','/agent/query',{},timeout=0)
    message=DataAnalysisOrchestrator._dependency_message(caught.value)
    assert '把问题转换成查询条件时' in message
    assert expected in message
    assert 'secret' not in message


@pytest.mark.parametrize('path',['/agent_chat','/agent_chat/stream'])
@pytest.mark.parametrize('error,expected',[(KeyError('token=secret'),'缺少程序需要的信息'),
                                         (TypeError('secret'),'数据格式或类型'),
                                         (RuntimeError('password=secret'),'不足以确定原因')])
def test_normal_and_stream_errors_are_specific_and_safe(path,error,expected):
    class Workflow:
        async def ainvoke(self,state):raise error
    app=build_test_app()
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post(path,json={'semantic_model_id':81,'application_id':'app1',
            'conversation_id':str(uuid4()),'message_id':str(uuid4()),'question':'查询销售额'})
    assert response.status_code==(200 if path.endswith('stream') else 500)
    assert expected in response.text
    assert 'secret' not in response.text


@pytest.mark.parametrize('reason,expected',[
    ('存在多个同名字段，不能唯一确定','多个可能的对应项'),
    ('未匹配到可执行的标准字段','没有找到可用于查询的对应字段'),
    ('字段已匹配，但条件值尚未匹配到该字段的向量标准值','可选值中匹配成功'),
])
def test_same_business_field_keeps_different_actual_reasons(reason,expected):
    raw='结构化参数【过滤条件[1]】{"field":"经销商名称","value":"示例公司"}：'+reason
    message=explain_binding_reason(raw)
    assert expected in message and '经销商名称' in message
    assert '换个说法' not in message


def test_unknown_reason_does_not_invent_missing_configuration():
    message=explain_dependency_failure(AdapterError('UNKNOWN','secret',status_code=500))
    assert '暂不能确定根因' in message
    assert '未配置' not in message


def test_structured_business_diagnostic_is_not_discarded():
    message=explain_dependency_failure(AdapterError('UNKNOWN','secret',details={'reason':'查询结果缺少经销商名称列','path':'/execute'}))
    assert '读取数据库数据时' in message and '查询结果缺少经销商名称列' in message


@pytest.mark.parametrize('raw,expected', [
    ("Unknown column 'private_field' in field list", '不存在的字段'),
    ('Too many connections', '可用连接数已满'),
    ('You have an error in your SQL syntax', '查询语句格式错误'),
])
def test_database_diagnostic_survives_http_boundary(raw,expected):
    response=httpx.Response(500,json={'error':raw})
    details=PlatformHttpClient._upstream_error_details(response)
    error=AdapterError('SQL_EXECUTION_FAILED','sanitized',details={'path':'/execute',**details})
    text=DataAnalysisOrchestrator._dependency_message(error)
    assert expected in text and 'private_field' not in text


def test_observed_dealer_failure_explains_relationship_in_plain_language():
    raw='结构化参数【展示字段[1]】{"field":"经销商名称"}：该展示属性与分组对象之间没有可确认的唯一关联，无法在不改变统计粒度的情况下返回。'
    message=explain_binding_reason(raw)
    assert '每条查询结果应该对应哪个“经销商名称”' in message
    assert '配错' in message and '维护人员' in message
    assert all(word not in message for word in ['统计粒度','展示属性','唯一关联','换个说法'])
