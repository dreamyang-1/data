# A类修复验证：聚合查询展示列归一（指标移入/提升维度）
import sys, json
sys.path.insert(0, "/root/yyy/Oagnet")
from dotenv import load_dotenv
load_dotenv("/root/yyy/Oagnet/.runtime.env", override=True)

import agent
import structured_binding as sb

orig_bind = sb.bind
def spy_bind(extraction, knowledge, model, **kw):
    catalog = sb.catalog_candidates(knowledge)
    print("fields含公司/等级:", [f for f in catalog["fields"] if "company" in f or "level" in f or "grade" in f])
    print("metrics:", sorted(catalog["metrics"]))
    ast, repairs = orig_bind(extraction, knowledge, model, **kw)
    print("=== bind返回 ast ===")
    print(json.dumps({k: ast[k] for k in ('metrics','dimensions','ambiguity')}, ensure_ascii=False, default=str)[:800])
    return ast, repairs
sb.bind = spy_bind

class ModelSpy:
    def __init__(self, inner): self._inner = inner
    def invoke(self, msgs):
        r = self._inner.invoke(msgs)
        content = str(getattr(r, "content", ""))
        print("=== 绑定模型返回 ===")
        print(content[:900])
        return r
    def __getattr__(self, name): return getattr(self._inner, name)
_orig_gcm = agent._get_chat_model
agent._get_chat_model = lambda: ModelSpy(_orig_gcm())

extraction = {
    "意图": "统计查询",
    "业务域": ["医药销售域"],
    "实体": ["销售公司"],
    "指标": [{"name": "含税销售总额"}],
    "维度": [],
    "展示字段": [{"entity": "销售公司", "field": "国药公司名称"}],
    "过滤条件": [],
    "排序": [],
    "时间粒度": {"unit": None, "time_range": None},
    "限制": None,
    "输出要求": "默认输出表格",
    "是否去重": None,
}
result = agent.main("查询每个国药公司的含税销售总额，并显示公司名称",
                    semantic_model_id=81, business_domain_ids=[205],
                    structured_extraction=extraction)
print("\n=== 最终结果 ===")
print(result[:1200])
