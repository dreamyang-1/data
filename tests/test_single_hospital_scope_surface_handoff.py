import asyncio
from copy import deepcopy
import json

import pytest

from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, TrustedIdentity
from app.domain.semantic_scope import AuthorizedSemanticScope


@pytest.mark.parametrize('model_id,department_owner,department_field,location,completed,expected', [
    (81,'商品科室关联','科室编码','城市',False,True),
    (81,'商品科室关联','科室编码','省份',False,True),
    (81,'商品科室关联','科室编码',None,False,True),
    (82,'商品科室关联','科室编码','城市',False,False),
    (81,'销售订单','业务科室','城市',False,False),
    (81,'医院','行政科室','城市',False,False),
    (81,None,None,'城市',False,False),
    (81,'商品科室关联','科室编码','城市',True,False),
])
def test_single_question_keeps_original_wording_and_confirmed_business_scope(
        model_id,department_owner,department_field,location,completed,expected):
    scope=AuthorizedSemanticScope(semantic_model_id=model_id,scope_mode='MODEL_WIDE')
    original='上海地区哪个医院消化内科2025年9月份的订单数量最多?'
    if completed:original+='，科室按商品适用科室筛选订单'
    request=CanonicalAnalysisRequest(conversation_id='single-hospital-scope',tenant_id='t',user_id='u',
        original_question=original,primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=model_id,authorized_semantic_scope=scope)
    conditions=[]
    if department_owner:conditions.append({'entity':department_owner,'field':department_field,'op':'=','value':['消化内科']})
    if location:conditions.append({'entity':'医院','field':location,'op':'=','value':['上海']})
    extraction={'过滤条件':conditions,'维度':['医院'],'时间粒度':{'time_range':'2025年9月'}}
    before=deepcopy(extraction)
    asl={'version':'2.0','intent':'query','subject':{'entity':'hospital'},'metrics':[],
         'dimensions':[{'name':'hospital.hospital_name'}],'filters':[],'time_context':None,'ambiguity':[]}
    class Client:
        def __init__(self):
            self.calls=[]
            self.responses=iter([
                {'success':True,'semantic_model_id':model_id,'business_domain_ids':[],
                 'asl_validation':'PASS','result':asl,'semantic_evidence':{'semantic_model_id':model_id,
                 'requested_business_domain_ids':[],'resolved_business_domain_ids':[],'selected_metrics':[]}},
                {'success':True,'sql':'SELECT hospital_name FROM hospital'},
                {'success':True,'columns':['hospital_name'],'data':[],'row_count':0}])
        async def post(self,base,path,payload,**kwargs):
            self.calls.append(deepcopy(payload));return next(self.responses)
    client=Client();adapter=HttpDataRetrievalAdapter(Settings(_env_file=None,env='test'),client)
    result=asyncio.run(adapter.query_surface(request,TrustedIdentity(tenant_id='t',user_id='u'),
        mentions=[],structured_extraction=extraction))
    sent=client.calls[0]
    assert sent['query'].startswith(original)
    assert sent['completed_question'].count('科室按商品适用科室筛选订单')==int(expected or completed)
    assert ('地区按医院所在地筛选' in sent['completed_question'])==(expected and bool(location))
    if not expected:assert sent['query']==original
    assert sent['structured_extraction']==before==extraction
    assert request.original_question==original and not request.rewritten_question
    assert json.loads(client.calls[1]['asl'])==asl==result.asl
