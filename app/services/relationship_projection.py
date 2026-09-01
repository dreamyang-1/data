from __future__ import annotations

import re

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    PrimaryIntent,
)


def requires_distinct_relationship_projection(
    request: CanonicalAnalysisRequest,
) -> bool:
    """Return whether a detail query represents a set of relationships.

    Relationship-list requests describe unique projected objects or object
    combinations.  A join may contain several physical paths to the same
    projection, so duplicate elimination must happen before LIMIT.  Event and
    transaction detail remains row-shaped and must never be collapsed.
    """

    if (
        request.primary_intent != PrimaryIntent.DETAIL_QUERY
        or request.metrics
        or AnalysisOperator.AGGREGATE in request.operators
    ):
        return False

    if "SET_RELATIONSHIP_PROJECTION" in request.assumptions:
        return True

    semantic_text = " ".join(
        value
        for value in (
            request.original_question,
            request.rewritten_question or "",
            request.entity or "",
            *request.fields,
            *request.dimensions,
        )
        if value
    ).lower()
    fact_detail_markers = (
        "订单",
        "交易",
        "事件",
        "日志",
        "明细",
        "逐笔",
        "每一笔",
        "每条记录",
        "每一条记录",
        "原始数据",
        "流水",
        "event log",
        "order detail",
        "transaction detail",
    )
    if any(marker in semantic_text for marker in fact_detail_markers):
        return False

    projected_master_names = {
        "产品名称", "商品名称", "经销商名称", "供应商名称",
        "医院名称", "客户名称", "门店名称", "品牌名称",
        "产品", "商品", "经销商", "供应商", "医院", "客户", "门店", "品牌",
    }
    projected_fields = {
        value for value in (*request.fields, *request.dimensions) if value
    }
    if projected_fields and projected_fields.issubset(projected_master_names):
        return True

    # A downstream DAG constraint is a filter compiled from an upstream set.
    # Repeated join paths do not turn the requested downstream object list into
    # event detail, even when the generated ASL projects the matched relation.
    if request.dependency_constraints:
        return True

    relationship_markers = (
        "适用",
        "关联",
        "对应",
        "所属",
        "合作",
        "匹配",
        "覆盖",
        "related",
        "relationship",
        "belongs to",
        "associated",
    )
    set_markers = (
        "哪些",
        "名单",
        "清单",
        "列表",
        "列出",
        "提供",
        "筛选",
        "查找",
        "找出",
        "匹配",
        "推荐",
        "top",
        "which",
        "list",
    )
    asks_for_set = any(marker in semantic_text for marker in set_markers)
    explicit_relationship = any(
        marker in semantic_text for marker in relationship_markers
    )
    # “某产品的经销商/医院名单”省略了“合作/关联”二字，但语义仍是
    # 从产品到业务对象的关系集合。若按普通明细执行，关系表中的每条
    # 销售路径都会重复投影同一个名称，导致把 196 条事实行误报成 196
    # 个经销商。该判断仍位于交易明细排除逻辑之后，不会折叠订单事实。
    implicit_product_relationship = (
        any(marker in semantic_text for marker in ("产品", "商品", "品牌"))
        and any(
            marker in semantic_text
            for marker in ("经销商", "医院", "科室", "供应商", "客户", "门店")
        )
    )
    # The inverse wording usually names the dealer as a concrete company and
    # therefore does not contain the literal word “经销商”. It is still a
    # relationship set: one dealer -> unique products. Without DISTINCT the
    # sales fact table repeats the same product once per order line.
    implicit_partner_product_relationship = bool(
        re.search(
            r"(?:有限责任公司|股份有限公司|有限公司|公司)"
            r".{0,12}(?:销售过?|卖过?|经营)"
            r"(?:.{0,12}(?:哪些|什么))?.{0,4}(?:产品|商品)",
            semantic_text,
        )
    )
    return implicit_partner_product_relationship or asks_for_set and (
        explicit_relationship or implicit_product_relationship
    )
