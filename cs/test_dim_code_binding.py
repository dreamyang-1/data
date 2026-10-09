# 离线验证：模型返回维度编码时，时间锚点/过滤/展示槽位能换绑到物理字段
import sys
sys.path.insert(0, "/root/yyy/Oagnet")
from structured_binding import bind


class StubModel:
    """返回固定的绑定计划，模拟模型把编码填到物理字段槽位。"""

    def __init__(self, plan):
        self.plan = plan

    def invoke(self, msgs):
        import json

        class R:
            content = json.dumps(self.plan, ensure_ascii=False)
        return R()


def make_knowledge():
    from types import SimpleNamespace

    def rec(meta):
        return SimpleNamespace(metadata=meta)

    attrs = [
        {"attr_code": "created_date", "attr_name": "订单日期",
         "field_mapping": "sales_order.created_date", "data_type": "date"},
        {"attr_code": "dealer_name", "attr_name": "经销商名称",
         "field_mapping": "dealer.dealer_name", "data_type": "varchar"},
        {"attr_code": "city_name", "attr_name": "城市名称",
         "field_mapping": "dim_city.city_name", "data_type": "varchar"},
    ]
    dims = [
        {"dim_code": "business_date", "dim_name": "业务日期", "field_mapping": "",
         "bind_entities": [{"mappingTable": "sales_order", "mappingColumn": "created_date"}]},
        {"dim_code": "city", "dim_name": "城市", "field_mapping": "",
         "bind_entities": [{"mappingTable": "dim_city", "mappingColumn": "city_name"}]},
    ]
    return {
        "_vector_authorized_fields": ["sales_order.created_date", "dealer.dealer_name", "dim_city.city_name"],
        "attributes": [rec(a) for a in attrs],
        "dimensions": [rec(d) for d in dims],
        "entities": [rec({"entity_code": "sales_order", "entity_name": "销售订单", "attributes": []})],
        "metrics": [],
        "entity_attribute_values": [],
    }


base = {
    "意图": "统计查询", "业务域": [], "实体": ["经销商"], "指标": [], "维度": [],
    "展示字段": [], "过滤条件": [], "排序": [], "时间粒度": None, "限制": None,
    "输出要求": None, "是否去重": None,
}

# 1. 时间锚点返回维度编码
plan = {"subject": "sales_order", "metrics": [],
        "dimensions": [{"index": 0, "key": "business_date", "granularity": "month"}],
        "display_fields": [],
        "filters": [], "sort": [], "relationship_required": False,
        "time": {"mode": "calendar", "anchor": "business_date", "amount": 1,
                 "unit": "year", "value": 2026, "type": "year"}}
extraction = dict(base, 时间粒度={"unit": "月", "time_range": "2026年"}, 维度=["业务日期"])
ast, repairs = bind(extraction, make_knowledge(), StubModel(plan), today=__import__("datetime").date(2026, 9, 30))
assert ast["ambiguity"] == [], ast["ambiguity"]
assert ast["time_context"]["anchor"] == "sales_order.created_date", ast["time_context"]
print("1 时间锚点维度编码换绑: 通过 ->", ast["time_context"])

# 2. 时间锚点返回未知编码：仍然报错
plan2 = dict(plan, time={"mode": "calendar", "anchor": "no_such_dim", "value": 2026, "type": "year"})
ast2, _ = bind(extraction, make_knowledge(), StubModel(plan2), today=__import__("datetime").date(2026, 9, 30))
assert any("未匹配到授权的时间字段" in str(a.get("question")) for a in ast2["ambiguity"])
print("2 未知锚点仍报错: 通过")

# 3. 过滤条件返回维度编码（数值条件，跳过标准值绑定）
plan3 = {"subject": None, "metrics": [], "dimensions": [], "display_fields": [],
         "filters": [{"index": 0, "key": "business_date", "op": ">=", "value": "2026-01-01"}],
         "sort": [], "relationship_required": False, "time": None}
extraction3 = dict(base, 过滤条件=[{"field": "业务日期", "op": ">=", "value": "2026-01-01"}])
ast3, _ = bind(extraction3, make_knowledge(), StubModel(plan3), today=__import__("datetime").date(2026, 9, 30))
assert ast3["filters"] == [{"field": "sales_order.created_date", "operator": ">=", "value": "2026-01-01"}], ast3["filters"]
print("3 过滤条件维度编码换绑: 通过 ->", ast3["filters"])


# 5. 中文日期字面量过滤值：等值换算成区间，不要求标准值
def filter_case(op, value, expect):
    plan_f = {"subject": None, "metrics": [], "dimensions": [], "display_fields": [],
              "filters": [{"index": 0, "key": "business_date", "op": op, "value": value}],
              "sort": [], "relationship_required": False, "time": None}
    ex = dict(base, 过滤条件=[{"field": "业务日期", "op": op, "value": value}])
    ast_f, _ = bind(ex, make_knowledge(), StubModel(plan_f), today=__import__("datetime").date(2026, 9, 30))
    assert ast_f["filters"] == expect, (op, value, ast_f["filters"], ast_f["ambiguity"])


filter_case("=", "2025年7月", [{"field": "sales_order.created_date", "operator": "BETWEEN",
                               "value": ["2025-07-01", "2025-07-31"]}])
print("5 中文月份等值换算: 通过")
filter_case("=", "2025年", [{"field": "sales_order.created_date", "operator": "BETWEEN",
                            "value": ["2025-01-01", "2025-12-31"]}])
print("6 中文年份等值换算: 通过")
filter_case("=", "2025年7月3日", [{"field": "sales_order.created_date", "operator": "BETWEEN",
                                 "value": ["2025-07-03", "2025-07-03"]}])
print("7 中文日期等值换算: 通过")
filter_case(">=", "2025-07", [{"field": "sales_order.created_date", "operator": ">=", "value": "2025-07-01"}])
print("8 ISO月份下界: 通过")
filter_case("BETWEEN", ["2025年7月", "2025年9月"], [{"field": "sales_order.created_date", "operator": "BETWEEN",
                                                   "value": ["2025-07-01", "2025-09-30"]}])
print("9 中文月份区间: 通过")

# 10. 非日期中文值仍要求标准值（兜底不放松）
plan10 = {"subject": None, "metrics": [], "dimensions": [], "display_fields": [],
          "filters": [{"index": 0, "key": "city", "op": "=", "value": "上海市"}],
          "sort": [], "relationship_required": False, "time": None}
ex10 = dict(base, 过滤条件=[{"field": "城市", "op": "=", "value": "上海市"}])
ast10, _ = bind(ex10, make_knowledge(), StubModel(plan10), today=__import__("datetime").date(2026, 9, 30))
assert any("标准值" in str(a.get("question")) or "标准值" in str(a) for a in ast10["ambiguity"]), ast10["ambiguity"]
print("10 非日期中文值仍走标准值校验: 通过")

# 4. 展示字段返回维度编码（明细查询）
plan4 = {"subject": None, "metrics": [], "dimensions": [], "display_fields": [{"index": 0, "key": "business_date"}],
         "filters": [], "sort": [], "relationship_required": False, "time": None}
extraction4 = dict(base, 展示字段=["业务日期"])
ast4, _ = bind(extraction4, make_knowledge(), StubModel(plan4), today=__import__("datetime").date(2026, 9, 30))
assert ast4["ambiguity"] == [] or all("展示" not in str(a) for a in ast4["ambiguity"])
assert ast4["dimensions"][0]["name"] == "sales_order.created_date", ast4["dimensions"]
print("4 展示字段维度编码换绑: 通过 ->", ast4["dimensions"])

print("\n全部通过")
