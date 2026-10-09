# 苏云品牌召回复现：看值目录里有没有 parent_brand 候选、苏云排第几
import sys, json
sys.path.insert(0, "/root/yyy/Oagnet")
from dotenv import load_dotenv
load_dotenv("/root/yyy/Oagnet/.runtime.env", override=True)
import agent
import structured_binding as sb

orig_bind = sb.bind
def spy_bind(extraction, knowledge, model, **kw):
    catalog = sb.catalog_candidates(knowledge)
    pb = [(i, r.get('value'), r.get('label'), r.get('field')) for i, r in enumerate(catalog['values'])
          if 'parent_brand' in str(r.get('field'))]
    print("== filter_value_queries ==")
    print(sb.filter_value_queries(extraction))
    print("== 值目录 parent_brand 候选（序号, 值, 标签, 字段） ==")
    for item in pb:
        print("  ", item)
    # 全量池里搜苏云
    pool = (knowledge.get('_ambiguity_candidates') or {}).get('entity_attribute_value', [])
    meta = [getattr(p, 'metadata', {}) or {} for p in pool]
    hits = [(i, m.get('attr_value') or m.get('canonical_value'), m.get('source_field'))
            for i, m in enumerate(meta) if '苏云' in str(m.get('attr_value') or m.get('canonical_value') or '')]
    print(f"== _ambiguity_candidates 池大小 {len(pool)}，含苏云: {hits}")
    main = knowledge.get('entity_attribute_values', [])
    mmeta = [getattr(p, 'metadata', {}) or {} for p in main]
    mh = [(i, m.get('attr_value') or m.get('canonical_value')) for i, m in enumerate(mmeta)
          if '苏云' in str(m.get('attr_value') or m.get('canonical_value') or '')]
    print(f"== 主结果 {len(main)} 条，含苏云: {mh}")
    # 提取的过滤条件值
    print("== extraction 过滤条件 ==", json.dumps(extraction.get('过滤条件'), ensure_ascii=False))
    return orig_bind(extraction, knowledge, model, **kw)
sb.bind = spy_bind

extraction = {
    "意图": "明细查询", "业务域": ["医药销售域"], "实体": ["经销商"],
    "指标": [], "维度": [],
    "展示字段": [], "过滤条件": [{"entity": "生产厂家", "field": "母厂牌", "op": "=", "value": ["苏云"]}],
    "排序": [], "时间粒度": {"unit": None, "time_range": None},
    "限制": None, "输出要求": "输出经销商清单", "是否去重": "是",
}
result = agent.main("查询江苏省苏云品牌低值耗材的经销商清单", semantic_model_id=81,
                    business_domain_ids=[205], structured_extraction=extraction)
print("=== 结果 ==="); print(result[:500])
