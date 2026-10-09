# 用与线上一致的参数复现 main()，打印授权目录与绑定模型原始返回
import sys, json
sys.path.insert(0, "/root/yyy/Oagnet")
from dotenv import load_dotenv
load_dotenv("/root/yyy/Oagnet/.runtime.env", override=True)

import agent
import structured_binding as sb

# 1. 侦察 bind：打印本次召回得到的授权目录
orig_bind = sb.bind
def spy_bind(extraction, knowledge, model, **kw):
    catalog = sb.catalog_candidates(knowledge)
    print("=== 授权物理字段 catalog['fields'] ===")
    print(sorted(catalog["fields"]))
    print("=== 目录维度 ===", sorted(catalog["dimensions"]))
    print("=== 目录指标 ===", sorted(catalog["metrics"]))
    return orig_bind(extraction, knowledge, model, **kw)
sb.bind = spy_bind

# 2. 侦察绑定模型原始返回（pydantic模型不能改属性，用代理包装）
orig_get_model = agent._get_chat_model
class ModelSpy:
    def __init__(self, inner): self._inner = inner
    def invoke(self, msgs):
        r = self._inner.invoke(msgs)
        content = str(getattr(r, "content", ""))
        if "time" in content:
            print("=== 绑定模型原始返回(截取) ===")
            print(content[:1500])
        return r
    def __getattr__(self, name): return getattr(self._inner, name)
def spy_model():
    return ModelSpy(orig_get_model())
agent._get_chat_model = spy_model

extraction = {
    "意图": "统计查询",
    "业务域": ["医药销售域"],
    "实体": ["经销商"],
    "指标": [{"name": "已合作经销商数量"}],
    "维度": ["业务日期"],
    "展示字段": [],
    "过滤条件": [],
    "排序": [{"field": "业务日期", "order": "asc"}],
    "时间粒度": {"unit": "月", "time_range": "2026年"},
    "限制": None,
    "输出要求": "默认输出表格",
    "是否去重": None,
}

result = agent.main("查看2026年每个月已合作经销商数量",
                    semantic_model_id=81, business_domain_ids=[205],
                    structured_extraction=extraction)
print("\n=== 最终结果 ===")
print(result[:2500])
