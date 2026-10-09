"""Transport/model-format errors must never become business clarification."""
import json
from copy import deepcopy
from types import SimpleNamespace as Doc

import pytest

from binding_errors import BindingServiceError, dependency_failure, invoke_binding_object
from structured_binding import bind
from query_binding_review import review_bindings
from test_structured_binding import fixture
from test_query_binding_review import catalog, draft


@pytest.mark.parametrize('content', [
    '{"subject":"hospital"}', '```json\n{"subject":"hospital"}\n```',
    ' \ufeff ```JSON\n{"subject":"hospital"}\n``` ',
    [{'type':'text','text':'{"subject":'},{'type':'text','text':'"hospital"}'}],
])
def test_text_transport_variants_preserve_the_exact_json_object(content):
    assert invoke_binding_object(Doc(invoke=lambda _:Doc(content=content)), [],
                                 stage='catalog_binding') == {'subject':'hospital'}


def test_null_sections_preserve_existing_deterministic_catalog_recovery():
    e,k,plan=fixture();plan['filters']=None
    ast,_=bind(e,k,Doc(invoke=lambda _:Doc(content=json.dumps(plan))))
    assert not ast['ambiguity']
    assert ast['filters'] == [{'field':'hospital.province','operator':'=','value':'上海市'}]


@pytest.mark.parametrize('content', ['', None, [], ['x'], [{'type':'tool_use','text':'{}'}],
    '[]', 'null', '42', 'prefix {"subject":"hospital"}', '{broken', '{}',
    '{"message":"missing input"}', '{"filters":"wrong"}'])
def test_unusable_model_content_is_not_a_missing_user_parameter(content):
    e,k,_=fixture(); before=deepcopy(e)
    with pytest.raises(BindingServiceError) as caught:
        bind(e,k,Doc(invoke=lambda _:Doc(content=content)))
    assert caught.value.code == 'ASL_BINDING_RESPONSE_INVALID'
    assert not caught.value.retryable and e == before
    assert '请补充' not in json.dumps(caught.value.public_detail(),ensure_ascii=False)


@pytest.mark.parametrize('exception,code,status,retryable', [
    (TimeoutError('secret'), 'ASL_BINDING_TIMEOUT',504,True),
    (ConnectionError('secret'), 'ASL_BINDING_UNAVAILABLE',503,True),
    (RuntimeError('secret'), 'ASL_BINDING_FAILED',503,False),
])
@pytest.mark.parametrize('stage', ['catalog_binding','relationship_binding'])
def test_model_call_failures_are_typed_and_never_publish_exception_messages(exception,code,status,retryable,stage):
    def fail(_):raise exception
    with pytest.raises(BindingServiceError) as caught:
        if stage == 'catalog_binding':
            e,k,_=fixture();bind(e,k,Doc(invoke=fail))
        else:
            review_bindings(json.dumps(draft()),catalog(),'',None,Doc(invoke=fail),None,121,287)
    error=caught.value
    assert (error.code,error.status_code,error.retryable,error.stage) == (code,status,retryable,stage)
    assert 'secret' not in json.dumps(error.public_detail())


@pytest.mark.parametrize('status,expected,retryable', [(401,'ASL_BINDING_AUTH_FAILED',False),
    (403,'ASL_BINDING_AUTH_FAILED',False),(429,'ASL_BINDING_RATE_LIMITED',True),
    (503,'ASL_BINDING_UNAVAILABLE',True)])
def test_wrapped_http_failures_keep_a_bounded_classification(status,expected,retryable):
    inner=RuntimeError('private response');inner.status_code=status
    inner.response=Doc(status_code=status,headers={'Retry-After':'2'})
    outer=RuntimeError('private wrapper');outer.__cause__=inner
    error=dependency_failure(outer,'catalog_binding')
    assert error.code == expected and error.retryable == retryable
    if status == 429: assert error.response.headers == {'Retry-After':'2'}


@pytest.mark.parametrize('number,code', [(1045,'ASL_BINDING_AUTH_FAILED'),
    (2003,'ASL_BINDING_UNAVAILABLE'),(2013,'ASL_BINDING_UNAVAILABLE'),(1146,'ASL_BINDING_FAILED')])
def test_dictionary_database_failure_classification(number,code):
    from pymysql.err import OperationalError
    assert dependency_failure(OperationalError(number,'secret'),'dictionary_lookup').code == code


@pytest.mark.parametrize('stage', ['catalog_binding','relationship_binding','dictionary_lookup'])
def test_api_returns_service_error_and_safe_scope_diagnostics(monkeypatch,stage):
    import api
    from fastapi import HTTPException
    error=BindingServiceError('ASL_BINDING_TIMEOUT',stage=stage,error_type='TimeoutError',status_code=504,retryable=True)
    def fail(*a,**kw):raise error
    monkeypatch.setattr(api,'main',fail)
    logs=[]
    monkeypatch.setattr(api.logger,'warning',lambda *args,**kwargs:logs.append(args))
    with pytest.raises(HTTPException) as caught:
        api.agent_query(api.QueryRequest(query='测试',semantic_model_id=121,business_domain_ids=[287]))
    assert caught.value.status_code == 504
    assert caught.value.detail == error.public_detail()
    assert 'ambiguity' not in caught.value.detail
    assert logs and logs[-1][1:] == (121,[287],stage,'ASL_BINDING_TIMEOUT','TimeoutError',True)


def test_http_error_envelope_never_reports_success_for_malformed_model_output(monkeypatch):
    import api
    from fastapi.testclient import TestClient
    def fail(*args,**kwargs):
        raise BindingServiceError('ASL_BINDING_RESPONSE_INVALID',stage='catalog_binding',
                                  error_type='JSONDecodeError',status_code=502)
    monkeypatch.setattr(api,'main',fail)
    response=TestClient(api.app).post('/agent/query',json={'query':'统计年度销售总额',
        'semantic_model_id':121,'business_domain_ids':[287]})
    assert response.status_code == 502
    assert response.json()['success'] is False
    assert response.json()['detail']['code'] == 'ASL_BINDING_RESPONSE_INVALID'
    assert not response.json()['detail']['retryable']


def test_dictionary_timeout_is_not_reported_as_an_unknown_standard_value():
    def fail(*_):raise TimeoutError('private host')
    ast=draft();before=deepcopy(ast)
    e={'过滤条件':[{'entity':'医院','field':'省份','op':'=','value':['上海市']}]}
    with pytest.raises(BindingServiceError) as caught:
        review_bindings(json.dumps(ast),catalog(),'',e,Doc(),fail,121,287,
                        structured_only=True,relationship_required=False)
    assert caught.value.stage == 'dictionary_lookup' and ast == before


def test_multi_value_dictionary_partial_miss_never_widens_the_filter():
    ast=draft();ast['filters'][0].update(operator='IN',value=['上海市','北京市'])
    e={'过滤条件':[{'entity':'医院','field':'省份','op':'IN','value':['上海市','北京市']}]}
    result,repairs=review_bindings(json.dumps(ast),catalog(),'',e,Doc(),
        lambda *args:['001'] if args[-1]=='上海市' else [],121,287,
        structured_only=True,relationship_required=False)
    result=json.loads(result)
    assert result['filters'] == ast['filters'] and result['ambiguity'] and not repairs


def test_rate_limit_failure_still_uses_the_existing_bounded_capacity_retry():
    from capacity_control import AslCapacityController, AslUpstreamRateLimited
    from test_capacity_control import _policy,_RateLimitError
    calls=[]
    def fail(_):calls.append(1);raise _RateLimitError('0')
    controller=AslCapacityController(_policy(),sleeper=lambda _:None)
    with pytest.raises(AslUpstreamRateLimited):
        controller.run(lambda:invoke_binding_object(Doc(invoke=fail),[],stage='catalog_binding'))
    assert len(calls) == 2
