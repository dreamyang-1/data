"""语义建模推荐接口（Python 版）单测与路由契约测试。

覆盖：实体表推荐（建议实体名去重/关键词分组/limit/LLM 重排与降级）、
属性推荐（主键优先/排序/enumValues）、关联检测（计数/related/skip 零）、
关系推荐（已配置分组/反向交换/基数翻转/物理推导 PK-FK/LLM 语义增强）、
路由层 Resp 包装与鉴权。
"""
from contextlib import contextmanager
import json
import logging

import pytest
from fastapi.testclient import TestClient as RawTestClient

from app.config import Settings
from app.main import create_app
from app.services import semantic_recommend
from app.services.semantic_recommend import (
    SemanticRecommendError,
    SemanticRecommendService,
    _build_semantic_description,
    _derive_entity_name,
    _is_foreign_key_to,
    _normalize_cardinality,
    _parse_llm_json_array,
    _split_table_column,
)


# ====================== 测试替身 ======================


class FakeReader:
    """按表驱动返回元数据行（snake_case，与数据库列同名）。

    calls 记录每次读取的方法名，用于断言"固定查询次数"回归
    （detect_relations 不随实体数增长 N+1）。
    """

    def __init__(self, *, entities=None, tables=None,
                 fields=None, relations=None):
        self._entities = entities or []
        self._tables = tables or []
        self._fields = fields or {}
        self._relations = relations or []
        self.field_calls = []
        self.calls = []

    def entities(self, semantic_model_id):
        self.calls.append("entities")
        return [dict(row) for row in self._entities]

    def tables(self, semantic_model_id, data_source_id):
        self.calls.append("tables")
        rows = [dict(row) for row in self._tables]
        if data_source_id is None:
            return rows
        return [row for row in rows if row.get("data_source_id") == data_source_id]

    def field_counts(self, table_ids):
        self.calls.append("field_counts")
        self.field_calls.append(list(table_ids))
        return {tid: len(self._fields.get(tid, [])) for tid in table_ids}

    def fields(self, table_id):
        self.calls.append("fields")
        return [dict(row) for row in self._fields.get(table_id, [])]

    def fields_batch(self, table_ids):
        self.calls.append("fields_batch")
        return {tid: [dict(row) for row in self._fields.get(tid, [])]
                for tid in table_ids}

    def relations(self, semantic_model_id):
        self.calls.append("relations")
        return [dict(row) for row in self._relations]


class FakeGateway:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def complete(self, query, prompt):
        self.calls.append((query, prompt))
        return self.answer


def field(fid, name, comment=None, ftype="varchar", enum_values=None):
    return {"id": fid, "name": name, "comment": comment,
            "type": ftype, "enum_values": enum_values}


def base_reader(**overrides):
    data = dict(
        entities=[
            {"id": "et-1", "name": "客户", "main_table_name": "customer",
             "data_source_id": 58},
            {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
             "data_source_id": 58},
        ],
        tables=[
            {"id": 1, "name": "customer", "comment": "客户信息表",
             "data_source_id": 58},
            {"id": 2, "name": "sales_order", "comment": "订单记录表",
             "data_source_id": 58},
            {"id": 3, "name": "log_record", "comment": None,
             "data_source_id": 58},
            {"id": 4, "name": "config_item", "comment": "系统配置",
             "data_source_id": 59},
        ],
        fields={
            1: [
                field(11, "id", "主键", "bigint"),
                field(12, "customer_name", "客户名称"),
                field(13, "status", None, "varchar", "A,B"),
            ],
            2: [
                field(21, "id", "主键", "bigint"),
                field(22, "customer_id", "客户ID"),
            ],
        },
        relations=[
            {"id": 9, "name": "拥有", "category": "IDENTIFYING", "type": "1:N",
             "source_entity_type_id": "et-1", "target_entity_type_id": "et-2",
             "source_table_column_name": "customer-id",
             "target_table_column_name": "sales_order-customer_id"},
            {"id": 10, "name": "属于", "category": None, "type": "N:1",
             "source_entity_type_id": "et-2", "target_entity_type_id": "et-1",
             "source_table_column_name": "sales_order-order_no",
             "target_table_column_name": "customer-id"},
        ],
    )
    data.update(overrides)
    return FakeReader(**data)


# ====================== 纯函数 ======================


def test_is_foreign_key_to_rules():
    assert _is_foreign_key_to("customer_id", "customer", "id")
    assert _is_foreign_key_to("CUSTOMER_ID", "customer", "id")
    assert not _is_foreign_key_to("id", "customer", "id")
    assert not _is_foreign_key_to(None, "customer", "id")
    assert not _is_foreign_key_to("customer_name", "customer", "id")
    # 去下划线后仍不匹配对方表名，也没有同名主键
    assert not _is_foreign_key_to("cust_id", "customer", None)
    # 与对方主键同名
    assert _is_foreign_key_to("sales_order_id", None, "sales_order_id")
    # 短表名（长度<=2 去下划线）不参与前缀判断
    assert not _is_foreign_key_to("ab_id", "ab", None)


def test_split_table_column():
    assert _split_table_column("customer-id") == ("customer", "id")
    assert _split_table_column("customer") == ("customer", None)
    assert _split_table_column(None) == (None, None)
    assert _split_table_column("  ") == (None, None)
    assert _split_table_column("-id") == ("-id", None)  # idx=0 视为无分隔符


def test_normalize_cardinality_reversal():
    assert _normalize_cardinality("1:N", True) == "N:1"
    assert _normalize_cardinality("N:1", True) == "1:N"
    assert _normalize_cardinality("N:N", True) == "N:N"
    assert _normalize_cardinality("1:N", False) == "1:N"
    assert _normalize_cardinality(None, False) == "1:N"


def test_build_semantic_description_sentences():
    assert _build_semantic_description("客户", "订单", "1:1", "拥有") == (
        "1个【客户】拥有1个【订单】"
    )
    assert _build_semantic_description("客户", "订单", "N:1", "隶属") == (
        "多个【客户】隶属1个【订单】，1个【订单】被多个【客户】隶属"
    )
    assert _build_semantic_description("客户", "订单", "N:N", "关联") == (
        "多个【客户】关联多个【订单】"
    )
    assert _build_semantic_description("客户", "订单", "1:N", "开设") == (
        "1个【客户】开设多个【订单】，一个【订单】被多个【客户】开设"
    )


def test_derive_entity_name_suffixes():
    assert _derive_entity_name("customer", "客户信息表") == "客户"
    assert _derive_entity_name("sales_order", "订单记录表") == "订单"
    assert _derive_entity_name("t_user", "用户数据表") == "用户"
    assert _derive_entity_name("t_user", None) == "t_user"
    assert _derive_entity_name("t_user", "") == "t_user"
    assert _derive_entity_name("t_user", "配置") == "配置"


def test_parse_llm_json_array():
    assert _parse_llm_json_array(None) is None
    assert _parse_llm_json_array("") is None
    assert _parse_llm_json_array("说明文字") is None
    assert _parse_llm_json_array("结果：[{'a':1}]") is None  # 非法 JSON
    assert _parse_llm_json_array('前缀 [ {"a":1} ] 后缀') == [{"a": 1}]
    assert _parse_llm_json_array('{"a":1}') is None  # 不是数组


# ====================== 接口一：实体表推荐 ======================


def test_recommend_tables_entity_name_dedup_and_keyword_limit():
    # 2026-10-14 契约：实体唯一——建议实体名与已有实体重名的条目不返回；
    # 表本身是否被映射不影响（一张表可派生多个实体）。
    # fixture 中 et-1(客户)/et-2(订单) 使 customer/sales_order 的建议名撞车
    reader = base_reader()
    service = SemanticRecommendService(reader, None)

    # 无关键词：有注释在前（按表名），无注释在后；字段数经一次 GROUP BY 查询
    result = service.recommend_tables(81, None, None, None)
    assert [vo["tableName"] for vo in result] == ["config_item", "log_record"]
    assert reader.field_calls == [[1, 2, 3, 4]]
    by_name = {vo["tableName"]: vo for vo in result}
    assert by_name["config_item"]["suggestedEntityName"] == "系统配置"
    assert by_name["config_item"]["fieldCount"] == 0
    assert by_name["log_record"]["fieldCount"] == 0

    # 关键词命中：名称命中 > 注释命中，未命中的被过滤；
    # 命中表的建议名撞已有实体（sales_order→订单）同样不返回
    result = service.recommend_tables(81, None, "订单", None)
    assert result == []

    # 关键词命中注释
    result = service.recommend_tables(81, None, "系统", None)
    assert [vo["tableName"] for vo in result] == ["config_item"]

    # 关键词命中建议名未撞车的表
    result = service.recommend_tables(81, None, "log", None)
    assert [vo["tableName"] for vo in result] == ["log_record"]

    # limit 截断
    result = service.recommend_tables(81, None, None, 1)
    assert [vo["tableName"] for vo in result] == ["config_item"]

    # data_source 过滤
    result = service.recommend_tables(81, 59, None, None)
    assert [vo["tableName"] for vo in result] == ["config_item"]


def test_recommend_tables_table_not_mapped_but_name_taken():
    # 表未被任何实体映射，但建议实体名已存在 → 不返回（实体唯一）
    reader = base_reader(tables=[
        {"id": 1, "name": "customer", "comment": "客户信息表", "data_source_id": 58},
        {"id": 2, "name": "sales_order", "comment": "订单记录表", "data_source_id": 58},
        {"id": 5, "name": "merchant_order", "comment": "订单", "data_source_id": 58},
        {"id": 3, "name": "log_record", "comment": None, "data_source_id": 58},
        {"id": 4, "name": "config_item", "comment": "系统配置", "data_source_id": 59},
    ])
    service = SemanticRecommendService(reader, None)
    # merchant_order 未被映射，但建议名"订单"与已有实体 et-2 重名 → 剔除
    names = [vo["tableName"] for vo in service.recommend_tables(81, None, None, None)]
    assert "merchant_order" not in names
    assert names == ["config_item", "log_record"]


def test_recommend_tables_llm_rename_into_existing_entity_dropped():
    # 大模型把建议实体名改成已有实体名 → 返回前剔除
    reader = base_reader()
    gateway = FakeGateway('[{"tableName":"log_record","entityName":"客户"}]')
    service = SemanticRecommendService(reader, gateway)
    result = service.recommend_tables(81, None, None, None)
    names = [vo["tableName"] for vo in result]
    assert "log_record" not in names
    assert names == ["config_item"]


def test_recommend_tables_llm_reorder_and_entity_names():
    reader = base_reader()
    gateway = FakeGateway(
        '[{"tableName":"config_item","entityName":"配置项"}]'
    )
    service = SemanticRecommendService(reader, gateway)
    result = service.recommend_tables(81, None, None, None)
    # 已配置表（customer/sales_order）不返回；LLM 返回的排在前
    assert [vo["tableName"] for vo in result] == ["config_item", "log_record"]
    by_name = {vo["tableName"]: vo for vo in result}
    assert by_name["config_item"]["suggestedEntityName"] == "配置项"
    assert "实体映射表推荐排序" == gateway.calls[0][0]


def test_recommend_tables_llm_failure_falls_back_to_rules():
    reader = base_reader()
    service = SemanticRecommendService(reader, FakeGateway(None))
    result = service.recommend_tables(81, None, None, None)
    assert [vo["tableName"] for vo in result] == ["config_item", "log_record"]
    assert result[0]["suggestedEntityName"] == "系统配置"


def test_recommend_tables_llm_ignored_without_gateway():
    reader = base_reader()
    service = SemanticRecommendService(reader, None, llm_enabled=True)
    # gateway 为 None 时 _llm_enabled 自动为 False，不应报错
    assert service.recommend_tables(81, None, None, None)


# ====================== 接口二：属性推荐 ======================


def test_recommend_fields_primary_first_and_ordering():
    service = SemanticRecommendService(base_reader(), None)
    result = service.recommend_fields(1)
    assert [vo["name"] for vo in result] == ["id", "customer_name", "status"]
    assert result[0]["primaryKey"] is True
    assert result[0]["fieldId"] == 11
    assert result[1]["primaryKey"] is False
    # 无注释字段排在有注释字段之后
    status = result[2]
    assert status["comment"] is None
    assert status["enumValues"] == "A,B"
    assert status["suggestedAttrName"] == "status"
    assert result[1]["suggestedAttrName"] == "客户名称"


def test_recommend_fields_rejects_bad_table_id():
    service = SemanticRecommendService(base_reader(), None)
    with pytest.raises(SemanticRecommendError):
        service.recommend_fields(0)
    with pytest.raises(SemanticRecommendError):
        service.recommend_fields(-1)


def test_recommend_fields_llm_refines_names():
    # 2026-10-14 契约：注释含字母/数字的字段必须由模型提炼为纯中文属性名
    reader = base_reader(fields={
        1: [
            field(11, "id", "主键", "bigint"),
            field(12, "customer_name", "客户名称"),
            field(14, "level3_code", "customer_level3编码"),
            field(13, "status", None, "varchar", "A,B"),
        ],
    })
    answer = json.dumps([
        {"name": "id", "attrName": "主键"},
        {"name": "customer_name", "attrName": "客户名称"},
        {"name": "level3_code", "attrName": "客户等级编码"},
        {"name": "status", "attrName": "状态A"},  # 含字母，必须被拒绝
    ], ensure_ascii=False)
    service = SemanticRecommendService(reader, FakeGateway(answer))
    result = service.recommend_fields(1)
    by_name = {vo["name"]: vo for vo in result}
    assert by_name["level3_code"]["suggestedAttrName"] == "客户等级编码"
    assert by_name["customer_name"]["suggestedAttrName"] == "客户名称"
    # 模型输出含字母 → 拒绝，保留规则结果（原字段名）
    assert by_name["status"]["suggestedAttrName"] == "status"
    # 模型被调用过（属性名提炼入口）
    gateway = service._gateway
    assert gateway.calls and gateway.calls[0][0] == "实体属性名提炼"


def test_recommend_fields_llm_invalid_answer_keeps_rule_names():
    reader = base_reader(fields={
        1: [field(12, "customer_name", "客户名称")],
    })
    # 网关返回空答案（如模型名不受支持）→ 静默降级为规则结果
    service = SemanticRecommendService(reader, FakeGateway(None))
    result = service.recommend_fields(1)
    assert result[0]["suggestedAttrName"] == "客户名称"
    assert len(service._gateway.calls) == 1


def test_recommend_fields_llm_clean_name_kept_without_gateway_call_change():
    # gateway 为 None 时 _llm_enabled 自动为 False，不应报错也不调模型
    reader = base_reader(fields={1: [field(12, "customer_name", "客户名称")]})
    service = SemanticRecommendService(reader, None)
    result = service.recommend_fields(1)
    assert result[0]["suggestedAttrName"] == "客户名称"


# ====================== 接口三：关联检测 ======================


def test_detect_relations_filters_by_business_domain():
    # 2026-10-14 契约：实体在业务域内配置，传 businessDomainId 时只检测同域实体
    reader = base_reader(
        entities=[
            {"id": "et-1", "name": "客户", "main_table_name": "customer",
             "data_source_id": 58, "business_domain_id": 10},
            {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
             "data_source_id": 58, "business_domain_id": 10},
            {"id": "et-3", "name": "库存", "main_table_name": "stock",
             "data_source_id": 58, "business_domain_id": 20},
        ],
        relations=[
            {"id": 9, "name": "拥有", "category": "IDENTIFYING", "type": "1:N",
             "source_entity_type_id": "et-1", "target_entity_type_id": "et-2",
             "source_table_column_name": "customer-id",
             "target_table_column_name": "sales_order-customer_id"},
            {"id": 11, "name": "占用", "category": None, "type": "1:N",
             "source_entity_type_id": "et-1", "target_entity_type_id": "et-3",
             "source_table_column_name": "customer-stock_id",
             "target_table_column_name": "stock-id"},
        ],
    )
    service = SemanticRecommendService(reader, None)
    # 不传参：自动按当前实体 et-1 所属域 10 过滤（STALE_TEST：2026-10-14 契约
    # 从"不传=全模型"改为"不传=自动识别业务域"）
    auto = service.detect_relations(81, "et-1")
    assert [row["otherEntityTypeId"] for row in auto] == ["et-2"]
    # 显式传业务域 10：与自动识别结果一致
    scoped = service.detect_relations(81, "et-1", business_domain_id=10)
    assert [row["otherEntityTypeId"] for row in scoped] == ["et-2"]
    assert scoped[0]["related"] is True
    # 传其他域 20：只返回该域内的 et-3（它与当前实体有已配置关系）
    other_domain = service.detect_relations(81, "et-1", business_domain_id=20)
    assert [row["otherEntityTypeId"] for row in other_domain] == ["et-3"]
    assert other_domain[0]["relationCount"] == 1


def test_detect_relations_auto_derives_domain_from_current_entity():
    # 2026-10-14 契约：进入业务域才能选实体，服务端按当前实体所属业务域自动过滤
    reader = base_reader(
        entities=[
            {"id": "et-1", "name": "客户", "main_table_name": "customer",
             "data_source_id": 58, "business_domain_id": 10},
            {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
             "data_source_id": 58, "business_domain_id": 10},
            {"id": "et-3", "name": "库存", "main_table_name": "stock",
             "data_source_id": 58, "business_domain_id": 20},
        ],
        relations=[
            {"id": 9, "name": "拥有", "category": "IDENTIFYING", "type": "1:N",
             "source_entity_type_id": "et-1", "target_entity_type_id": "et-2",
             "source_table_column_name": "customer-id",
             "target_table_column_name": "sales_order-customer_id"},
            {"id": 11, "name": "占用", "category": None, "type": "1:N",
             "source_entity_type_id": "et-1", "target_entity_type_id": "et-3",
             "source_table_column_name": "customer-stock_id",
             "target_table_column_name": "stock-id"},
        ],
    )
    service = SemanticRecommendService(reader, None)
    # 不传参：自动按 et-1 所属域 10 过滤，跨域 et-3 被剔除
    scoped = service.detect_relations(81, "et-1")
    assert [row["otherEntityTypeId"] for row in scoped] == ["et-2"]
    # 当前实体无业务域：不过滤（全模型行为）
    reader._entities[0]["business_domain_id"] = None
    service2 = SemanticRecommendService(reader, None)
    all_ids = {row["otherEntityTypeId"]
               for row in service2.detect_relations(81, "et-1")}
    assert all_ids == {"et-2", "et-3"}


def test_detect_relations_counts_configured_and_inferred():
    service = SemanticRecommendService(base_reader(), None)
    result = service.detect_relations(81, "et-1")
    assert len(result) == 1
    row = result[0]
    assert row["otherEntityTypeId"] == "et-2"
    assert row["otherEntityName"] == "订单"
    assert row["related"] is True
    assert row["relationCount"] == 2  # 两条已配置关系（含反向）
    assert row["inferred"] is True
    assert row["inferredCount"] == 1  # sales_order.customer_id -> customer.id


def test_detect_relations_skips_entities_without_evidence():
    reader = base_reader(entities=[
        {"id": "et-1", "name": "客户", "main_table_name": "customer",
         "data_source_id": 58},
        {"id": "et-3", "name": "孤立实体", "main_table_name": "no_such_table",
         "data_source_id": 58},
    ])
    service = SemanticRecommendService(reader, None)
    assert service.detect_relations(81, "et-1") == []


def test_detect_relations_validates_input():
    service = SemanticRecommendService(base_reader(), None)
    with pytest.raises(SemanticRecommendError):
        service.detect_relations(81, "")
    with pytest.raises(SemanticRecommendError, match="实体类型不存在"):
        service.detect_relations(81, "et-404")


def test_detect_relations_fixed_reader_calls():
    """回归：读取次数固定为 4（entities/relations/tables/fields_batch）。

    原实现逐实体查表清单和字段（85 实体模型约 173 条 SQL，实测 2.6s）。
    """
    entities = [
        {"id": "et-1", "name": "客户", "main_table_name": "customer",
         "data_source_id": 58},
        {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
         "data_source_id": 58},
        {"id": "et-3", "name": "无主表实体", "main_table_name": None,
         "data_source_id": 58},
        {"id": "et-4", "name": "主表缺失实体", "main_table_name": "missing",
         "data_source_id": 58},
        {"id": "et-5", "name": "日志", "main_table_name": "log_record",
         "data_source_id": 58},
    ]
    reader = base_reader(entities=entities)
    service = SemanticRecommendService(reader, None)
    service.detect_relations(81, "et-1")
    assert reader.calls == ["entities", "relations", "tables", "fields_batch"]


# ---------- 接口三：大模型复核（2026-10-15） ----------


def test_detect_relations_llm_reviews_rule_pairs_and_adds_suggestions():
    """模型复核：确认规则对并给出字段对；对规则未命中的对补充建议（llmOnly）。"""
    reader = base_reader(entities=[
        {"id": "et-1", "name": "客户", "main_table_name": "customer",
         "data_source_id": 58},
        {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
         "data_source_id": 58},
        {"id": "et-5", "name": "日志", "main_table_name": "log_record",
         "data_source_id": 58},
    ])
    answer = json.dumps([
        {"otherEntityTypeId": "et-2", "confirmed": True,
         "cardinality": "N:1", "confidence": "high",
         "reason": "订单表客户ID指向客户主键",
         "fieldPairs": [
             {"sourceTableName": "sales_order", "sourceColumnName": "customer_id",
              "targetTableName": "customer", "targetColumnName": "id"},
         ]},
        {"otherEntityTypeId": "et-5", "confirmed": True,
         "cardinality": "1:N", "confidence": "low",
         "reason": "操作日志按客户维度记录",
         "fieldPairs": [
             {"sourceTableName": "customer", "sourceColumnName": "id",
              "targetTableName": "log_record", "targetColumnName": "customer_id"},
         ]},
    ], ensure_ascii=False)
    gateway = FakeGateway(answer)
    service = SemanticRecommendService(reader, gateway)
    result = service.detect_relations(81, "et-1")

    # 单次批量调用
    assert len(gateway.calls) == 1
    assert len(result) == 2
    confirmed = result[0]
    assert confirmed["otherEntityTypeId"] == "et-2"
    assert confirmed["related"] is True
    assert confirmed["inferred"] is True
    assert confirmed["llmReviewed"] is True
    assert confirmed["llmConfirmed"] is True
    assert confirmed["llmConfidence"] == "high"
    assert confirmed["llmCardinality"] == "N:1"
    assert confirmed["llmFieldPairs"][0]["sourceColumnName"] == "customer_id"
    assert confirmed["llmReason"] == "订单表客户ID指向客户主键"
    assert confirmed["llmOnly"] is False
    suggestion = result[1]
    assert suggestion["otherEntityTypeId"] == "et-5"
    assert suggestion["llmOnly"] is True
    assert suggestion["related"] is False
    assert suggestion["inferred"] is False
    assert suggestion["llmConfirmed"] is True
    assert suggestion["llmFieldPairs"][0]["targetColumnName"] == "customer_id"


def test_detect_relations_llm_denial_moves_rule_pair_last():
    """模型否决规则命中的对：该对排到模型确认的对之后（tier3）。"""
    reader = base_reader(
        entities=[
            {"id": "et-1", "name": "客户", "main_table_name": "customer",
             "data_source_id": 58},
            {"id": "et-3", "name": "库存", "main_table_name": "stock",
             "data_source_id": 58},
            {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
             "data_source_id": 58},
        ],
        fields={
            1: [field(11, "id", "主键", "bigint")],
            2: [field(21, "id", "主键", "bigint"),
                field(22, "customer_id", "客户ID")],
            5: [field(51, "id", "主键", "bigint"),
                field(52, "customer_id", "客户ID")],
        },
        tables=[
            {"id": 1, "name": "customer", "comment": "客户信息表",
             "data_source_id": 58},
            {"id": 2, "name": "sales_order", "comment": "订单记录表",
             "data_source_id": 58},
            {"id": 5, "name": "stock", "comment": "库存表",
             "data_source_id": 58},
        ],
        relations=[],
    )
    answer = json.dumps([
        {"otherEntityTypeId": "et-3", "confirmed": False,
         "cardinality": None, "confidence": "low", "reason": "客户与库存无直接业务关联",
         "fieldPairs": []},
        {"otherEntityTypeId": "et-2", "confirmed": True,
         "cardinality": "N:1", "confidence": "high", "reason": "外键指向客户",
         "fieldPairs": [
             {"sourceTableName": "sales_order", "sourceColumnName": "customer_id",
              "targetTableName": "customer", "targetColumnName": "id"},
         ]},
    ], ensure_ascii=False)
    service = SemanticRecommendService(reader, FakeGateway(answer))
    result = service.detect_relations(81, "et-1")

    # 规则顺序是 [et-3, et-2]（entities 顺序），模型确认 et-2、否决 et-3 后重排
    assert [row["otherEntityTypeId"] for row in result] == ["et-2", "et-3"]
    assert result[0]["llmConfirmed"] is True
    assert result[1]["llmConfirmed"] is False
    assert result[1]["llmFieldPairs"] == []


def test_detect_relations_llm_failure_keeps_rule_result():
    """网关失败/输出不可解析：静默降级，结果与纯规则完全一致且无 llm 字段。"""
    for answer in (None, "不是JSON", "[]"):
        service = SemanticRecommendService(base_reader(), FakeGateway(answer))
        result = service.detect_relations(81, "et-1")
        assert len(result) == 1
        row = result[0]
        assert row["otherEntityTypeId"] == "et-2"
        assert row["inferredCount"] == 1
        assert not any(k.startswith("llm") for k in row)


def test_detect_relations_llm_invalid_suggestions_ignored():
    """未确认/无字段对的建议、未知实体ID、字段对缺失键均被忽略。"""
    reader = base_reader(entities=[
        {"id": "et-1", "name": "客户", "main_table_name": "customer",
         "data_source_id": 58},
        {"id": "et-2", "name": "订单", "main_table_name": "sales_order",
         "data_source_id": 58},
        {"id": "et-5", "name": "日志", "main_table_name": "log_record",
         "data_source_id": 58},
    ])
    answer = json.dumps([
        # 未确认且无字段对：忽略
        {"otherEntityTypeId": "et-5", "confirmed": False,
         "confidence": "low", "fieldPairs": []},
        # 未知实体ID：忽略
        {"otherEntityTypeId": "et-999", "confirmed": True,
         "confidence": "high",
         "fieldPairs": [{"sourceTableName": "a", "sourceColumnName": "id",
                         "targetTableName": "b", "targetColumnName": "a_id"}]},
        # 字段对缺键：过滤为空
        {"otherEntityTypeId": "et-2", "confirmed": True, "confidence": "high",
         "fieldPairs": [{"sourceTableName": "sales_order"}]},
    ], ensure_ascii=False)
    service = SemanticRecommendService(reader, FakeGateway(answer))
    result = service.detect_relations(81, "et-1")

    assert [row["otherEntityTypeId"] for row in result] == ["et-2"]
    row = result[0]
    assert row["llmReviewed"] is True
    assert row["llmConfirmed"] is True
    assert row["llmFieldPairs"] == []


# ====================== 接口四：关系推荐 ======================


def test_recommend_relations_existing_grouping_and_reversal():
    service = SemanticRecommendService(base_reader(), None)
    result = service.recommend_relations(81, "et-1", "et-2")

    existing = [vo for vo in result if vo["fromExisting"]]
    assert len(existing) == 2
    by_id = {vo["relationId"]: vo for vo in existing}

    forward = by_id[9]  # et-1 -> et-2 原方向
    assert forward["relationName"] == "拥有"
    assert forward["cardinality"] == "1:N"
    assert forward["fieldPairs"] == [{
        "sourceTableName": "customer", "sourceColumnName": "id",
        "sourceColumnComment": None,
        "targetTableName": "sales_order", "targetColumnName": "customer_id",
        "targetColumnComment": None,
    }]
    assert forward["semanticDescription"] == (
        "1个【客户】拥有多个【订单】，一个【订单】被多个【客户】拥有"
    )

    reversed_vo = by_id[10]  # et-2 -> et-1 存储方向，需翻转为请求视角
    assert reversed_vo["cardinality"] == "1:N"  # N:1 反向翻转
    # 字段对按请求方向交换：存储的 sales_order.order_no -> customer.id
    # 展示为 customer.id -> sales_order.order_no
    assert reversed_vo["fieldPairs"][0]["sourceTableName"] == "customer"
    assert reversed_vo["fieldPairs"][0]["sourceColumnName"] == "id"
    assert reversed_vo["fieldPairs"][0]["targetTableName"] == "sales_order"
    assert reversed_vo["fieldPairs"][0]["targetColumnName"] == "order_no"


def test_recommend_relations_inferred_candidates_and_llm_names():
    gateway = FakeGateway(
        '[{"candidate":1,"relationName":"开设","semanticDescription":"自定义"},'
        '{"candidate":99,"relationName":"越界"}]'
    )
    service = SemanticRecommendService(base_reader(), gateway)
    result = service.recommend_relations(81, "et-1", "et-2")

    inferred = [vo for vo in result if not vo["fromExisting"]]
    # 1:N（PK-FK）一条；无反向 FK、无同名外键
    assert len(inferred) == 1
    candidate = inferred[0]
    assert candidate["relationName"] == "开设"
    assert candidate["semanticDescription"] == "自定义"
    assert candidate["cardinality"] == "1:N"
    assert candidate["fieldPairs"] == [{
        "sourceTableName": "customer", "sourceColumnName": "id",
        "sourceColumnComment": "主键",
        "targetTableName": "sales_order", "targetColumnName": "customer_id",
        "targetColumnComment": "客户ID",
    }]


def test_recommend_relations_same_name_fk_and_reverse_fk():
    reader = base_reader(fields={
        1: [
            field(11, "id", "主键", "bigint"),
            field(12, "dept_id", "部门ID"),
            field(13, "region_id", "区域ID"),
        ],
        2: [
            field(21, "id", "主键", "bigint"),
            field(22, "customer_id", "客户ID"),
            field(23, "dept_id", "部门ID"),
        ],
    })
    service = SemanticRecommendService(reader, None)
    result = service.recommend_relations(81, "et-1", "et-2")
    inferred = [vo for vo in result if not vo["fromExisting"]]
    # 1:N（customer.id -> sales_order.customer_id）
    # + N:1（sales_order.id 无对应 customer 主键外键 → 无）
    # + N:N（同名 dept_id）
    cardinalities = sorted(vo["cardinality"] for vo in inferred)
    assert cardinalities == ["1:N", "N:N"]
    nn = next(vo for vo in inferred if vo["cardinality"] == "N:N")
    assert nn["relationName"] == "关联"  # 无网关时保留模板
    assert nn["semanticDescription"] == (
        "多个【客户】关联多个【订单】"
    )
    assert nn["fieldPairs"] == [{
        "sourceTableName": "customer", "sourceColumnName": "dept_id",
        "sourceColumnComment": "部门ID",
        "targetTableName": "sales_order", "targetColumnName": "dept_id",
        "targetColumnComment": "部门ID",
    }]


def test_recommend_relations_without_llm_keeps_template():
    service = SemanticRecommendService(base_reader(), None)
    result = service.recommend_relations(81, "et-1", "et-2")
    inferred = [vo for vo in result if not vo["fromExisting"]]
    assert inferred[0]["relationName"] == "关联"


def test_recommend_relations_validates_input():
    service = SemanticRecommendService(base_reader(), None)
    with pytest.raises(SemanticRecommendError):
        service.recommend_relations(81, "", "et-2")
    with pytest.raises(SemanticRecommendError, match="实体类型不存在"):
        service.recommend_relations(81, "et-1", "et-404")


def test_recommend_relations_requires_both_entities():
    reader = base_reader(entities=[])
    service = SemanticRecommendService(reader, None)
    with pytest.raises(SemanticRecommendError, match="实体类型不存在"):
        service.recommend_relations(81, "et-1", "et-2")


# ====================== 路由层契约 ======================


HEADERS = {
    "Authorization": "Bearer phase0c-fixture-token",
    "X-Tenant-Id": "t1",
    "X-User-Id": "u1",
}


@contextmanager
def route_client(service):
    """注入 fake 推荐服务并启动应用 lifespan（container 注入 trusted_backend）。"""
    app = create_app(Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        business_question_collection_enabled=False,
        trusted_backend_token="phase0c-fixture-token",
    ))
    original = semantic_recommend.get_service
    semantic_recommend.get_service = lambda: service
    try:
        with RawTestClient(app) as client:
            yield client
    finally:
        semantic_recommend.get_service = original


def test_route_recommend_tables_resp_contract():
    with route_client(SemanticRecommendService(base_reader(), None)) as client:
        resp = client.get(
            "/semanticModelEntityType/recommendTables",
            params={"semanticModelId": 81, "keyword": "log"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 200
        assert body["msg"] == "成功"
        assert isinstance(body["timestamp"], int)
        assert [row["tableName"] for row in body["data"]] == ["log_record"]
        assert all("configured" not in row for row in body["data"])


def test_route_recommend_fields():
    with route_client(SemanticRecommendService(base_reader(), None)) as client:
        resp = client.get(
            "/semanticModelEntityType/recommendFields",
            params={"tableId": 1},
            headers=HEADERS,
        )
        body = resp.json()
        assert body["code"] == 200
        assert [row["name"] for row in body["data"]] == [
            "id", "customer_name", "status",
        ]


def test_route_detect_relations_and_recommend_relations():
    with route_client(SemanticRecommendService(base_reader(), None)) as client:
        resp = client.get(
            "/semanticModelRelationConfig/detectRelations",
            params={"semanticModelId": 81, "entityTypeId": "et-1"},
            headers=HEADERS,
        )
        body = resp.json()
        assert body["code"] == 200
        assert body["data"][0]["otherEntityTypeId"] == "et-2"

        resp = client.get(
            "/semanticModelRelationConfig/recommendRelations",
            params={
                "semanticModelId": 81,
                "sourceEntityTypeId": "et-1",
                "targetEntityTypeId": "et-2",
            },
            headers=HEADERS,
        )
        body = resp.json()
        assert body["code"] == 200
        assert any(vo["fromExisting"] for vo in body["data"])


def test_route_business_error_maps_to_resp_500():
    class FailingService:
        def recommend_tables(self, *args):
            raise SemanticRecommendError("物理表ID不能为空")

    with route_client(FailingService()) as client:
        resp = client.get(
            "/semanticModelEntityType/recommendTables",
            params={"semanticModelId": 81},
            headers=HEADERS,
        )
        body = resp.json()
        assert resp.status_code == 200
        assert body["code"] == 500
        assert body["msg"] == "物理表ID不能为空"
        assert body["data"] is None


def test_route_unexpected_error_maps_to_internal_message():
    class FailingService:
        def recommend_tables(self, *args):
            raise RuntimeError("db down")

    with route_client(FailingService()) as client:
        resp = client.get(
            "/semanticModelEntityType/recommendTables",
            params={"semanticModelId": 81},
            headers=HEADERS,
        )
        body = resp.json()
        assert body["code"] == 500
        assert body["msg"] == "推荐服务内部错误"


def test_route_allows_anonymous_access():
    # STALE_TEST: 2026-10-14 用户决定 4 个推荐接口去鉴权（只读元数据，内网供 Java 后端调用），
    # 旧断言“无鉴权返回 401”按新契约改为可直接 200。
    with route_client(SemanticRecommendService(base_reader(), None)) as client:
        resp = client.get(
            "/semanticModelEntityType/recommendTables",
            params={"semanticModelId": 81},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 200


def test_route_query_validation():
    with route_client(SemanticRecommendService(base_reader(), None)) as client:
        # 缺少必填 semanticModelId
        resp = client.get(
            "/semanticModelEntityType/recommendTables", headers=HEADERS
        )
        assert resp.status_code == 422
        # limit 超上限
        resp = client.get(
            "/semanticModelEntityType/recommendTables",
            params={"semanticModelId": 81, "limit": 201},
            headers=HEADERS,
        )
        assert resp.status_code == 422


# ====================== 软删除过滤与网关告警回归 ======================


class _CaptureCursor:
    def __init__(self, captured):
        self._captured = captured

    def execute(self, sql, params):
        self._captured.append(sql)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _CaptureConn:
    def __init__(self, captured, **kwargs):
        self._captured = captured

    def cursor(self, cursorclass=None):
        return _CaptureCursor(self._captured)

    def close(self):
        pass


def test_pymysql_reader_filters_soft_deleted(monkeypatch):
    """回归：所有元数据 SQL 必须过滤 is_deleted = 0。

    实测 223 元数据库 37% 表 / 52% 实体 / 37% 字段 / 53% 关系配置
    处于已删除状态（模型 84 全部 793 张表已删除），不过滤会大量泄漏。
    """
    captured: list[str] = []
    monkeypatch.setattr(
        "pymysql.connect", lambda **kwargs: _CaptureConn(captured)
    )
    reader = semantic_recommend.PymysqlMetadataReader(
        host="h", port=3306, user="u", password="p", database="d")
    reader.entities(1)
    reader.tables(1, None)
    reader.tables(1, 58)
    reader.field_counts([1, 2])
    reader.fields(1)
    reader.fields_batch([1, 2])
    reader.relations(1)
    # field_counts([]) / fields_batch([]) 不应发 SQL
    assert reader.field_counts([]) == {}
    assert reader.fields_batch([]) == {}
    assert captured, "应发出 SQL"
    assert len(captured) == 7
    for sql in captured:
        assert "is_deleted = 0" in sql, f"缺少软删除过滤: {sql}"


def test_gateway_empty_answer_returns_none_and_logs(monkeypatch, caplog):
    """回归：网关空答案必须返回 None 并留 WARNING（qwen3-max 事故根因）。"""
    class _Resp:
        status_code = 200
        text = 'data: {"answer": ""}'

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: _Resp())
    gateway = semantic_recommend.WorkflowChatGateway(
        base_url="http://gw", api_key="k", model_name="qwen3-max")
    with caplog.at_level(
        logging.WARNING, logger="app.services.semantic_recommend"
    ):
        assert gateway.complete("query", "prompt") is None
    assert any("空答案" in record.getMessage() for record in caplog.records)


def test_gateway_non_200_returns_none_and_logs(monkeypatch, caplog):
    class _Resp:
        status_code = 502
        text = "bad gateway"

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: _Resp())
    gateway = semantic_recommend.WorkflowChatGateway(
        base_url="http://gw", api_key=None, model_name="qwen3.7-max")
    with caplog.at_level(
        logging.WARNING, logger="app.services.semantic_recommend"
    ):
        assert gateway.complete("query", "prompt") is None
    assert any("HTTP 502" in record.getMessage() for record in caplog.records)
