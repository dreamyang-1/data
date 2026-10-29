# B类复现：三级医院绑定细节
import sys, json
sys.path.insert(0, "/root/yyy/Oagnet")
from dotenv import load_dotenv
load_dotenv("/root/yyy/Oagnet/.runtime.env", override=True)
import agent
import structured_binding as sb

orig_bind = sb.bind
def spy_bind(extraction, knowledge, model, **kw):
    catalog = sb.catalog_candidates(knowledge)
    lv = [(i, r.get('value'), r.get('label'), r.get('field')) for i, r in enumerate(catalog['values']) if 'hospital_level' in str(r.get('field'))]
    print("等级values:", lv)
    return orig_bind(extraction, knowledge, model, **kw)
sb.bind = spy_bind

class ModelSpy:
    def __init__(self, inner): self._inner = inner
    def invoke(self, msgs):
        r = self._inner.invoke(msgs)
        print("=== 模型返回 ==="); print(str(getattr(r, "content", ""))[:700])
        return r
    def __getattr__(self, name): return getattr(self._inner, name)
_og = agent._get_chat_model
agent._get_chat_model = lambda: ModelSpy(_og())

extraction = {
    "意图": "统计查询", "业务域": ["医药销售域"], "实体": ["医院"],
    "指标": [{"name": "含税销售金额"}], "维度": [],
    "展示字段": [], "过滤条件": [{"field": "医院等级", "op": "=", "value": ["三级医院"]}],
    "排序": [], "时间粒度": {"unit": None, "time_range": None},
    "限制": None, "输出要求": "默认输出表格", "是否去重": None,
}
result = agent.main("统计三级医院的含税销售金额", semantic_model_id=81,
                    business_domain_ids=[205], structured_extraction=extraction)
print("=== 结果 ==="); print(result[:600])
