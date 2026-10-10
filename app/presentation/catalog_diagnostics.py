"""Actionable, bounded catalog errors without exposing raw SQL or connection data."""
from __future__ import annotations

import re


_SAFE_LABEL = re.compile(r"[\w\u3400-\u9fff .,:()（）/\-]{1,160}\Z")
_REASONS = {"MISSING": "为空或缺失", "INVALID": "格式或取值不合法",
            "AMBIGUOUS": "存在重复配置，无法唯一确定", "OWNER_MISMATCH": "归属与当前授权范围不一致"}
# Only these reviewed metadata slots can become operator-facing instructions.
_FIELDS = {
    "entity_code": ("实体编码", "semantic_model_entity_type.code", "在实体配置中补齐稳定且唯一的实体编码"),
    "attr_code": ("属性编码", "semantic_model_attribute_config.code", "在实体的属性配置中补齐稳定且唯一的属性编码"),
    "mapping_table": ("映射表", "semantic_model_attribute_config.mapping_table", "在属性配置中选择正确的物理表"),
    "mapping_column": ("映射字段", "semantic_model_attribute_config.mapping_column", "在属性配置中选择该物理表下的字段"),
    "field_mapping": ("字段映射", "实体属性 → 物理表/字段", "检查属性映射及物理表、字段注册是否唯一"),
    "relation_code": ("关系编码", "semantic_model_relation_config.code", "在实体关系配置中补齐关系编码，并检查两端实体和连接字段"),
    "metric_code": ("指标编码", "semantic_model_indicator.indicator_code", "在指标配置中补齐或纠正指标编码"),
    "indicator_code": ("指标编码", "semantic_model_indicator.indicator_code", "检查同一业务域的指标编码是否为空或重复"),
    "dim_code": ("维度编码", "semantic_model_dimension.code", "在维度配置中补齐维度编码"),
    "main_table_name": ("实体主表", "semantic_model_entity_type.main_table_name", "检查实体主表配置及物理表注册"),
    "sub_table_name": ("实体子表", "semantic_model_entity_sub_table_mapping.sub_table_name", "检查子表配置是否缺失或重复"),
    "main_join_column": ("主表连接字段", "semantic_model_entity_sub_table_mapping.main_join_column", "在主子表关系配置中补齐主表连接字段"),
    "sub_join_column": ("子表连接字段", "semantic_model_entity_sub_table_mapping.sub_join_column", "在主子表关系配置中补齐子表连接字段"),
    "dependence_atomic_indicator": ("依赖指标", "semantic_model_indicator.dependence_atomic_indicator", "检查依赖原子指标编码列表的格式及绑定"),
    "scope": ("目录归属", "语义模型/业务域归属", "核对对象所属模型和业务域；不要扩大授权范围"),
}
for _key in ("entity_id", "attribute_id", "table_id", "field_id", "data_source_id", "semantic_model_id", "business_domain_id"):
    _FIELDS[_key] = (f"对象标识 {_key}", "语义目录对象标识/归属", "核对该对象标识及其所属模型、业务域和数据源")
for _key in ("db_type", "host", "db_name", "port"):
    _FIELDS[_key] = (f"数据源配置 {_key}", "数据源配置", "检查该数据源的类型、地址、端口及数据库配置（连接信息不在回答中展示）")
for _key in ("table_name", "field_name", "code"):
    _FIELDS[_key] = (f"目录编码 {_key}", "物理表/字段或枚举配置", "补齐对应对象的目录名称或编码")


def _label(value):
    value = str(value) if type(value) is int else value
    return value if isinstance(value, str) and _SAFE_LABEL.fullmatch(value) else ""


def catalog_configuration_answer(exc: Exception, *, semantic_model_id: int, business_domain_ids) -> str:
    code = str(exc).strip()
    code = code if re.fullmatch(r"(?:CURRENT_)?CATALOG_[A-Z0-9_]{1,90}", code) else "CATALOG_CONFIGURATION_INVALID"
    domains = "、".join(str(v) for v in business_domain_ids) or "当前模型范围"
    lines = ["当前语义模型目录配置不完整，无法安全执行查询。",
             f"语义模型 ID：{semantic_model_id}；业务域：{domains}；错误码：{code}。"]
    details = []
    raw = getattr(exc, "issues", ())
    for issue in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(issue, dict) or issue.get("key") not in _FIELDS or issue.get("reason") not in _REASONS:
            continue
        if business_domain_ids and issue.get("business_domain_id") not in (None, *business_domain_ids):
            continue
        key = issue["key"]
        name, location, action = _FIELDS[key]
        objects = []
        for title, keys in (("实体", ("entity_name", "entity_code", "entity_id")),
                            ("属性", ("attr_name", "attr_code", "attribute_id")),
                            ("关系", ("relation_name", "relation_code")),
                            ("指标", ("metric_name", "metric_code", "indicator_code")),
                            ("维度", ("dim_name", "dim_code")),
                            ("物理表", ("table_name", "table_id")),
                            ("物理字段", ("field_name", "field_id")),
                            ("数据源", ("data_source_id",))):
            values = list(dict.fromkeys(v for k in keys if (v := _label(issue.get(k)))))
            if values:
                objects.append(title + "「" + " / ".join(values) + "」")
        mapping = _label(issue.get("field_mapping")) or ".".join(
            v for k in ("mapping_table", "mapping_column") if (v := _label(issue.get(k))))
        if mapping:
            objects.append("物理映射「" + mapping + "」")
        for title, slot in (("子表", "sub_table_name"), ("主表连接字段", "main_join_column"),
                            ("子表连接字段", "sub_join_column"), ("目标实体", "target_entity")):
            if value := _label(issue.get(slot)):
                objects.append(title + "「" + value + "」")
        text = f"{'；'.join(objects) or '当前目录对象'}：{name}（{location}）{_REASONS[issue['reason']]}。处理：{action}。"
        if text not in details:
            details.append(text)
    if details:
        lines.append(f"已定位 {len(details)} 项配置问题：")
        lines.extend(f"{i}、{text}" for i, text in enumerate(details[:50], 1))
        if len(details) > 50:
            lines.append(f"本次仅展示前 50 项，另有 {len(details) - 50} 项；修正后请重新校验目录。")
    else:
        lines.append("上游仅返回上述错误码，未提供具体对象明细，当前无法确认是哪个字段或关系；请管理员按模型 ID 和错误码检查目录加载日志，不要据此猜测或修改业务字段。")
    lines.append("本次在目录加载阶段停止，尚未执行 SQL；以上为当前范围的目录问题，不代表都由本次查询指标引起。修正并保存配置后请重试。")
    return "\n\n".join(lines)
