"""Render actionable dependency failures without hiding their real subject.

The dependency boundary only exposes bounded, structured diagnostics.  This
module turns those diagnostics into user guidance and administrator guidance;
it never guesses a missing business name or semantic field.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.adapters.base import AdapterError


def _clean_text(value: object, *, limit: int = 240) -> str:
    text = " ".join(str("" if value is None else value).split())
    return text[:limit]


def _details(exc: AdapterError) -> dict[str, Any]:
    if not isinstance(exc.details, dict):
        return {}
    result = dict(exc.details)
    nested = result.get("upstream_details")
    if isinstance(nested, dict):
        for key, value in nested.items():
            result.setdefault(str(key), value)
    return result


def _texts(value: object, *, limit: int = 8) -> list[str]:
    values: Iterable[object]
    if isinstance(value, (list, tuple, set)):
        values = value
    elif value in (None, ""):
        values = []
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = (
                item.get("label")
                or item.get("name")
                or item.get("field")
                or item.get("value")
            )
        text = _clean_text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _quote_join(values: Iterable[str]) -> str:
    return "、".join(f"“{value}”" for value in values if value)


def _field_label(value: object) -> str:
    field = _clean_text(value)
    if not field:
        return ""
    suffix = field.rsplit(".", 1)[-1].casefold()
    labels = {
        "manufacturer_name": "厂家名称",
        "parent_brand": "母厂牌",
        "brand_name": "厂牌名称",
        "product_name": "产品名称",
        "product_code": "产品编码",
        "hospital_name": "医院名称",
        "dealer_name": "经销商名称",
        "city_name": "城市名称",
        "province_name": "省份名称",
        "department_name": "科室名称",
        "transaction_date": "交易日期",
    }
    label = labels.get(suffix)
    return f"{label}（{field}）" if label else field


def _filter_descriptions(value: object) -> list[str]:
    entries = value if isinstance(value, list) else [value]
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            text = _clean_text(entry)
            if text:
                result.append(text)
            continue
        field = _field_label(entry.get("field") or entry.get("semantic_field"))
        raw_value = entry.get("value")
        values = _texts(raw_value)
        operator = _clean_text(entry.get("operator") or entry.get("op") or entry.get("expected_operator"))
        display_operator = {
            "EQ": "=", "NE": "!=", "NEQ": "!=", "GT": ">", "GTE": ">=",
            "GE": ">=", "LT": "<", "LTE": "<=", "LE": "<=",
        }.get(operator.upper(), operator)
        description = field
        if values:
            rendered_operator = display_operator or "="
            if rendered_operator.isalpha():
                rendered_operator = f" {rendered_operator} "
            description = f"{description or '筛选值'}{rendered_operator}{','.join(values)}"
        elif operator:
            description = f"{description}（要求操作符：{operator}）"
        if description:
            result.append(description)
    return list(dict.fromkeys(result))[:8]


def _diagnostic_subject(details: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        values = _texts(details.get(key))
        if values:
            return values
    return []


def render_dependency_error(exc: AdapterError) -> str | None:
    """Return a concrete message for a classified dependency failure.

    ``None`` means the caller may use its existing status/category fallback.
    """

    code = exc.upstream_code or exc.code
    details = _details(exc)

    if code == "ASL_ENTITY_MENTION_UNRESOLVED":
        mentions = _diagnostic_subject(details, "unresolved_mentions", "mention")
        if mentions:
            subject = _quote_join(mentions)
            return (
                f"无法确认业务名称{subject}应绑定到当前语义层中的哪个实体字段；"
                "已发布的数据目录没有给出唯一匹配，因此本次未继续生成 SQL。\n"
                f"用户可补充：在问题中直接写明{subject}的业务角色和完整名称，"
                f"例如“厂牌为{mentions[0]}”或“厂家名称为{mentions[0]}”。"
                "如果该词只是描述语而不是业务名称，请把实际筛选对象写完整。\n"
                f"语义层需配置：为{subject}对应的实体属性发布规范名称、别名和源值映射；"
                "若同一名称命中多个字段，应返回具体候选字段供用户选择。"
            )
        return (
            "语义查询服务报告存在未能唯一绑定的业务名称，但上游响应没有返回该名称。"
            "这属于诊断信息缺失，不能要求用户盲目重述问题。\n"
            "语义层需配置：检查实体属性、别名和源值目录；服务维护人员还需确认"
            "ASL_ENTITY_MENTION_UNRESOLVED 响应包含 mention 或 unresolved_mentions。"
        )

    if code in {"ASL_FILTER_INVALID", "ASL_REQUIRED_FILTER_MISSING"}:
        failed_filter = details.get("failed_filter")
        if isinstance(failed_filter, dict):
            failed = _filter_descriptions(failed_filter)
            if failed:
                reason = (
                    "该字段未通过本次向量目录范围校验"
                    if details.get("validation_stage") == "vector_grounding"
                    else "该条件未能绑定到可执行的语义字段"
                )
                return (
                    f"未通过校验的筛选条件：{_quote_join(failed)}。{reason}，"
                    "因此本次未生成 SQL，也未执行数据查询。"
                    "这里列出的是首先失败的条件，不代表其他条件也存在问题。"
                    "请数据部门核对该字段的目录映射和召回配置；"
                    "已提供的字段和值无需重复输入。"
                )
        raw_filters = (
            details.get("expected_filter")
            or details.get("missing_filters")
            or details.get("filters")
        )
        filters = _filter_descriptions(raw_filters)
        semantic_field = _clean_text(details.get("semantic_field"))
        if semantic_field and semantic_field not in filters:
            filters.insert(0, semantic_field)
        if not filters:
            return (
                "筛选条件校验失败，本次未生成 SQL。"
                "上游未返回具体失败字段和值，暂时无法判断是哪一项条件，"
                "不能据此认定所有筛选条件都有问题。请服务维护人员检查该次请求的诊断信息；"
                "无需重复输入已经提供的条件。"
            )
        target = _quote_join(filters)
        # Upstream does not currently produce candidate evidence carrying a
        # business semantic identity, and matching generic roles/types does
        # not prove relevance.  Never display unconfirmed candidates here;
        # the raw details stay untouched for operator inspection.
        return (
            f"筛选条件{target}没有绑定到唯一且可执行的语义字段，"
            "因此本次未继续生成 SQL。"
            " 当前无法确认可用的候选字段：诊断信息中没有能证明候选与"
            "该失败项相关的业务语义身份证据，系统不会用邻近字段充当候选。"
            "\n用户可补充：如原问题中该字段的业务含义不明确，可进一步说明其含义；"
            "已经给出的字段和值无需重复提供。"
            f"\n语义层需检查：请管理员核对{target}的目录映射与召回信息，"
            "确认是否存在对应业务语义角色的已发布属性、别名和关系路径；"
            "仅当存在明确的缺失证据时才能判定为未配置。"
        )

    if code == "INTENT_ASL_CONTRACT_INCOMPLETE":
        errors = _diagnostic_subject(details, "errors", "missing_constraints")
        suffix = f" 具体缺失：{_quote_join(errors)}。" if errors else ""
        return (
            "意图识别结果在转换为 ASL 执行合同时没有完整保留用户已经明确给出的条件。"
            + suffix
            + " 系统已停止执行，避免扩大或改变查询范围。\n"
            "用户可补充：仅当上面列出的条件在原问题中确实不明确时，补充对应对象、字段或范围；"
            "已明确的条件无需重复输入。\n"
            "语义层/合同需配置：检查结构化提取字段到 Intent-ASL contract 的映射，"
            "确保查询对象、筛选条件、分组和返回字段逐项保留。"
        )

    if code in {
        "ASL_DIMENSION_INVALID",
        "ASL_REQUIRED_DIMENSION_MISSING",
        "ASL_GROUPING_DIMENSION_MISSING",
    }:
        raw_dimensions = _diagnostic_subject(
            details, "missing_dimensions", "grouping", "required_dimensions"
        )
        target = _quote_join(raw_dimensions) if raw_dimensions else "本次要求的分组维度"
        return (
            f"语义查询没有保留或无法唯一绑定{target}，因此未执行不完整查询。"
            " 当前无法确认可用的候选字段：诊断信息中没有能证明候选与"
            "该维度相关的业务语义身份证据，系统不会用邻近字段充当候选。"
            "\n用户可补充：仅当原问题确实没有写明分组字段时才需要补充；"
            "已经给出的分组无需重复提供。"
            f"\n语义层需检查：请管理员核对{target}的目录映射与召回信息，"
            "确认是否存在对应业务语义角色的可分组属性及其实体关系路径；"
            "仅当存在明确的缺失证据时才能判定为未配置。"
        )

    if code == "ASL_UNREQUESTED_DIMENSION":
        unexpected = _diagnostic_subject(details, "unexpected_dimensions")
        required = _diagnostic_subject(details, "required_grouped_dimensions")
        message = "ASL 加入了用户没有要求的分组维度，可能改变结果粒度。"
        if unexpected:
            message += f" 多出的维度：{_quote_join(unexpected)}。"
        if required:
            message += f" 用户要求的维度：{_quote_join(required)}。"
        return (
            message
            + "\n用户无需补充已写明的分组；如原问题确实省略，请明确写成“按某字段分组”。"
            + "\n语义层需配置：限制 ASL dimensions 只保留 Intent-ASL contract 要求的规范维度。"
        )

    if code in {"ASL_DETAIL_FIELDS_INCOMPLETE", "ASL_DETAIL_PROJECTION_MISSING"}:
        fields = _diagnostic_subject(
            details, "missing_fields", "projection", "requested_fields", "query_object"
        )
        target = _quote_join(fields) if fields else "本次要求返回的明细字段"
        return (
            f"语义查询无法完整投影{target}，因此未执行可能漏列的查询。"
            " 当前无法确认可用的候选字段：诊断信息中没有能证明候选与"
            "该字段相关的业务语义身份证据，系统不会用邻近字段充当候选。"
            "\n用户可补充：仅当原问题确实没有写明需要返回的字段时才需要补充；"
            "已经给出的字段无需重复提供。"
            f"\n语义层需检查：请管理员核对{target}的目录映射与召回信息，"
            "确认是否存在对应业务语义角色的可查询属性和正确关系路径；"
            "仅当存在明确的缺失证据时才能判定为未配置。"
        )

    if code == "ASL_RELATIONSHIP_ANCHOR_INVALID":
        anchor = _diagnostic_subject(details, "relationship_anchor")
        target = _quote_join(anchor) if anchor else "本次关系查询的主体"
        return (
            f"关系查询主体{target}无法唯一绑定到语义实体。"
            " 当前无法确认可用的候选实体：诊断信息中没有能证明候选与"
            "该关系主体相关的业务语义身份证据，系统不会拿邻近实体凑数。"
            "\n用户可补充：仅当原问题确实没有写明关系两端的业务对象时才需要补充；"
            "已经给出的内容无需重复提供。"
            f"\n语义层需检查：请管理员核对{target}的目录映射与召回信息，"
            "确认是否存在唯一的关系主体、目标实体和连接路径；"
            "仅当存在明确的缺失证据时才能判定为未配置。"
        )

    if code == "ASL_METRIC_SELECTION_INVALID":
        expected = _diagnostic_subject(
            details, "expected_metric_codes", "required_metrics", "metric"
        )
        selected = _diagnostic_subject(details, "selected_metric_codes")
        message = "指标口径没有通过语义合同校验，为避免返回错误数据，本次未执行查询。"
        if expected:
            message += f" 本次要求的指标为：{_quote_join(expected)}。"
        return (
            message
            + "\n用户可补充：在问题中使用指标的完整业务名称重试；已经给出的内容无需重复提供。"
            + "\n若反复失败，需系统维护人员核对该问题对应指标的绑定配置后重新发布。"
        )

    if code in {
        "SQL_QUERY_ENTITY_ALIGNMENT_FAILED",
        "SQL_QUERY_FILTER_OPERATOR_FAILED",
        "SQL_QUERY_FILTER_POLARITY_FAILED",
    }:
        filters = _filter_descriptions(
            details.get("missing_entities") or details.get("filters")
        )
        target = _quote_join(filters) if filters else "当前问题中的筛选条件"
        reasons = {
            "SQL_QUERY_ENTITY_ALIGNMENT_FAILED": "生成的 SQL 没有保留",
            "SQL_QUERY_FILTER_OPERATOR_FAILED": "生成的 SQL 改变了精确匹配方式",
            "SQL_QUERY_FILTER_POLARITY_FAILED": "生成的 SQL 改变或遗漏了排除条件",
        }
        return (
            f"{reasons[code]}{target}，系统已拒绝执行可能查错范围的 SQL。\n"
            "用户无需反复改写同一问题；如果原问题中的字段角色不明确，请补充字段名称和完整值。\n"
            f"语义层/SQL 转换需配置：检查{target}的规范字段、操作符映射和关系路径，"
            "保证 ASL 到 SQL 后字段、值、正反向和精确匹配方式保持一致。"
        )

    if code == "SQL_RELATIONSHIP_GRAPH_INCOMPLETE":
        tables = _diagnostic_subject(details, "missing_tables")
        suffix = f" 缺少的关系表：{_quote_join(tables)}。" if tables else ""
        return (
            "SQL 引用了未出现在 FROM/JOIN 关系图中的表，系统已拒绝执行。"
            + suffix
            + "\n用户无需改写问题。语义层/SQL 转换需配置：补全实体关系、连接键和授权关系路径，"
            "使所有被引用实体都有可验证的 JOIN 路径。"
        )

    if code == "ASL_SYNTHETIC_PRODUCT_FILTER":
        filters = _filter_descriptions(details.get("unexpected_filters"))
        suffix = f" 多出的条件：{_quote_join(filters)}。" if filters else ""
        return (
            "ASL 在已明确的厂牌/品类范围之外擅自增加了产品筛选条件。"
            + suffix
            + " 系统已拒绝执行，避免缩小查询范围。\n"
            "用户无需补充未要求的产品名称。语义规划需配置：严格保留当前筛选合同，"
            "不得从厂牌或品类推测具体产品。"
        )

    if code in {"ASL_TIME_ANCHOR_INVALID", "ASL_TIME_ANCHOR_MISSING", "ASL_TIME_DIMENSION_MISSING"}:
        field = _field_label(details.get("field") or details.get("time_anchor"))
        suffix = f"（当前返回：{field}）" if field else ""
        return (
            f"本次时间字段绑定失败：查询范围无法绑定到已发布的业务时间字段{suffix}，"
            "因此未执行查询。\n"
            "用户可补充：说明要按交易日期、创建日期、维修日期等哪一种业务时间查询。\n"
            "语义层需配置：为当前实体发布时间属性、时间角色和默认时间锚点；"
            "趋势查询还需发布可分组的时间粒度。"
        )

    return None
