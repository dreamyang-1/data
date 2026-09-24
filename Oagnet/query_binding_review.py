"""Contextual predicate ownership and time review, without changing public ASL.

The model selects a published relationship, not SQL or a made-up region code.
The selected dictionary's actual rows supply the FK values. Query shape remains
owned by the existing grain review; this pass cannot change metrics or grouping.
"""
import calendar
from datetime import date, timedelta
import json
import re


def _metadata(item):
    return getattr(item, "metadata", {}) or {}


def _object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def _field(value):
    if isinstance(value, dict):
        table, column = value.get("mappingTable"), value.get("mappingColumn")
        return f"{table}.{column}" if table and column else ""
    return value if isinstance(value, str) else ""


def binding_options(ast, knowledge):
    allowed = set(knowledge.get("_vector_authorized_fields") or [])
    entities = [_metadata(item) for item in knowledge.get("entities", [])]
    owners = {}
    for entity in entities:
        for attr in _object(entity.get("attributes")) or []:
            field = _field(attr.get("field_mapping"))
            if isinstance(field, str) and field in allowed:
                owners[field] = entity
    relations = [_metadata(item) for item in knowledge.get("relations", [])]
    for entity in entities:
        relations.extend(_object(entity.get("relations")) or [])
    result = []
    for index, predicate in enumerate(ast.get("filters") or []):
        field = predicate.get("field", "")
        if field not in owners or predicate.get("operator") not in {"=", "!=", "IN", "NOT IN"}:
            continue
        table = field.partition(".")[0]
        choices = []
        for relation in relations:
            join = _object(relation.get("join_key"))
            if not isinstance(join, dict):
                continue
            a, b = _field(join.get("source_field")), _field(join.get("target_field"))
            for key, foreign in ((a, b), (b, a)):
                if (key not in owners or foreign not in owners or key == field
                        or key.partition(".")[0] != table
                        or foreign.partition(".")[0] == table):
                    continue
                # Both dictionary columns must belong to the same scoped entity.
                if owners[key].get("entity_code") != owners[field].get("entity_code"):
                    continue
                choice = {
                    "field": foreign, "dictionary_key": key,
                    "dictionary_entity": owners[key].get("entity_code"),
                    "business_domain_id": owners[key].get("business_domain_id") or owners[key].get("business_domain"),
                    "owner": owners[foreign].get("entity_name") or owners[foreign].get("entity_code"),
                    "relation": relation.get("relation_semantic") or relation.get("description") or "",
                }
                if choice not in choices:
                    choices.append(choice)
        if choices:
            result.append({"filter_index": index, "predicate": predicate, "choices": choices})
    return result


def _time_value(decision, today):
    """Date arithmetic, not a hardcoded business default; months != calendar year."""
    mode = decision.get("mode")
    if mode == "none":
        return None
    anchor = decision.get("anchor")
    if mode == "rolling":
        amount, unit = decision.get("amount"), decision.get("unit")
        if type(amount) is not int or not 1 <= amount <= 3660:
            raise ValueError("invalid rolling period")
        if unit in {"month", "year"}:
            months = amount * (12 if unit == "year" else 1)
            year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
            start = date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))
        elif unit in {"day", "week"}:
            start = today - timedelta(days=amount * (7 if unit == "week" else 1))
        else:
            raise ValueError("invalid rolling unit")
        return {"type": "range", "start": start.isoformat(), "end": today.isoformat(),
                "unit": "day", "anchor": anchor}
    if mode == "range":
        start, end = date.fromisoformat(decision["start"]), date.fromisoformat(decision["end"])
        if end < start:
            raise ValueError("reversed range")
        return {"type": "range", "start": start.isoformat(), "end": end.isoformat(),
                "unit": "day", "anchor": anchor}
    if mode == "calendar" and decision.get("type") in {"this_year", "this_month", "last_month", "today", "yesterday", "year"}:
        kind = decision["type"]
        result = {"type": kind, "anchor": anchor,
                  "unit": "year" if kind in {"year", "this_year"} else "month" if "month" in kind else "day"}
        if kind == "year":
            if type(decision.get("value")) is not int or not 1 <= decision["value"] <= 9999:
                raise ValueError("invalid year")
            result["value"] = decision["value"]
        return result
    raise ValueError("unknown time decision")


def review_bindings(content, knowledge, question, extraction, model, resolve_keys,
                    semantic_model_id, domain_scope, *, today=None, explicit_time=False):
    ast = json.loads(content)
    options = binding_options(ast, knowledge)
    if not options and not ast.get("time_context"):
        return content, []
    selected = {m.get("name") for m in ast.get("metrics") or []}
    metrics = [_metadata(m) for m in knowledge.get("metrics", []) if _metadata(m).get("metric_code") in selected]
    entities = [_metadata(e) for e in knowledge.get("entities", [])]
    # Policy evidence is restricted to the selected metric / subject; an unrelated
    # recalled metric cannot supply a default period.
    subject = (ast.get("subject") or {}).get("entity")
    policies = [*metrics, *(e for e in entities if e.get("entity_code") == subject)]
    context = {"completed_question": question, "structured_extraction": extraction,
               "draft_asl": ast, "filter_options": options, "selected_policies": policies,
               "current_date": (today or date.today()).isoformat()}
    try:
        response = model.invoke([
            {"role": "system", "content": (
                "复核筛选归属及时间，不改主体、指标、分组、排序或结果列。根据完整问题和所选指标的业务定义理解修饰关系，"
                "不要根据词表、分组实体或最短JOIN路径猜筛选归属。按经销商分组与筛选医院所在地是独立要求；"
                "反之明确查询上海的经销商时保留经销商所在地。共享字典名称没有表达归属，优先从filter_options选择"
                "符合上下文的真实owner；不要把筛选改成分组。允许分别选择两个已给出的条件，不得复制一个条件擅自增加约束。"
                "返回JSON：{\"bindings\":[{\"filter_index\":0,\"choice_index\":0,\"reason\":\"简短业务依据\"}],"
                "\"time\":{\"mode\":\"keep|none|rolling|calendar|range\",\"source\":\"question|catalog\","
                "\"evidence\":\"来源中逐字存在的时间要求或规则\",\"anchor\":\"目录日期字段\","
                "\"amount\":12,\"unit\":\"month\",\"type\":\"this_year\",\"start\":\"YYYY-MM-DD\",\"end\":\"YYYY-MM-DD\"}}。"
                "只填所选mode需要的字段；绑定不需修改时不填该项。rolling表示向前滚动的期间，不等于calendar自然年/月；"
                "具体月份数只能来自实际问题，上例12不是默认值。最新产品规则取消系统默认时间范围；"
                "用户和已确认上下文未指定时间就none，即使旧目录说明还有默认12个月也不使用。"
                "不能把时间锚点、统计周期、示例或指标名称当默认筛选。已有明确范围无须修改用keep。"
                "不得输出思维链、SQL、新字段、编码值或追问。"
            )}, {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
        ])
        decision = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip()))
        if not isinstance(decision, dict):
            return content, []
    except Exception:
        return content, []  # Transport failure does not become a new hard gate.
    repairs = []
    by_index = {o["filter_index"]: o for o in options}
    seen = set()
    bindings = decision.get("bindings")
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, dict):
            continue
        index, choice_index = binding.get("filter_index"), binding.get("choice_index")
        if (type(index) is not int or index in seen or index not in by_index or type(choice_index) is not int
                or not 0 <= choice_index < len(by_index[index]["choices"]) or not binding.get("reason")):
            continue
        seen.add(index)
        choice = by_index[index]["choices"][choice_index]
        old = ast["filters"][index]
        values = old["value"] if isinstance(old["value"], list) else [old["value"]]
        keys = []
        try:
            for value in values:
                found = resolve_keys(semantic_model_id, domain_scope, choice, old["field"], value)
                if not found:
                    raise ValueError("dictionary key unavailable")
                keys.extend(k for k in found if k not in keys)
        except Exception:
            continue  # Never manufacture a region code from its name.
        if not keys:
            continue
        operator = old["operator"]
        if len(keys) > 1 or operator in {"IN", "NOT IN"}:
            operator = "NOT IN" if operator in {"!=", "NOT IN"} else "IN"
            value = keys
        else:
            value = keys[0]
        ast["filters"][index] = dict(old, field=choice["field"], operator=operator, value=value)
        repairs.append({"type": "RESOLVE_FILTER_BUSINESS_OWNER", "previous_filter": old,
                        "resolved_filter": ast["filters"][index], "reason": str(binding["reason"])[:300],
                        "source": "SCOPED_RELATION_AND_DICTIONARY"})
    temporal = decision.get("time")
    if isinstance(temporal, dict) and temporal.get("mode") != "keep":
        quote = temporal.get("evidence")
        source = temporal.get("source")
        source_text = question if source == "question" else json.dumps(policies, ensure_ascii=False, default=str) if source == "catalog" else ""
        no_time = temporal.get("mode") == "none"
        if ((no_time and not explicit_time) or (not no_time and source == "question" and isinstance(quote, str)
                                               and quote.strip() and quote in source_text)):
            try:
                value = _time_value(temporal, today or date.today())
                if value and value["anchor"] not in set(knowledge.get("_vector_authorized_fields") or []):
                    raise ValueError("unregistered time anchor")
                if value != ast.get("time_context"):
                    previous = ast.get("time_context")
                    ast["time_context"] = value
                    repairs.append({"type": "RESOLVE_CONTEXTUAL_TIME_POLICY", "previous_time": previous,
                                    "time_context": value, "source": source, "evidence": quote})
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
    return (json.dumps(ast, ensure_ascii=False), repairs) if repairs else (content, [])
