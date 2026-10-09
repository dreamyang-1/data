"""Bind structured predicate ownership/relationships without changing query shape.

The model selects a published relationship, not SQL or a made-up region code.
Ordinary predicates retain their bound fields and standard values. Only explicit
business-owner disambiguation may use dictionary rows to supply FK values. Query shape remains
owned exclusively by planner parameters; original wording is never model input.
"""
import copy
from datetime import date
import json
import re
from binding_ownership import entity_label_candidates, parameter_owners


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
    by_entity = {entity.get('entity_code'): entity for entity in entities if entity.get('entity_code')}
    def add_owner(field, entity):
        if isinstance(field, str) and field in allowed and entity.get('entity_code'):
            owners.setdefault(field, {})[entity['entity_code']] = entity
    for entity in entities:
        for attr in _object(entity.get("attributes")) or []:
            field = _field(attr.get("field_mapping"))
            add_owner(field, entity)
    for item in knowledge.get('attributes', []):
        attr = _metadata(item)
        if attr.get('parent') in by_entity:
            add_owner(_field(attr.get('field_mapping')), by_entity[attr['parent']])
    relations = [_metadata(item) for item in knowledge.get("relations", [])]
    for entity in entities:
        relations.extend(_object(entity.get("relations")) or [])
    relationship_fields = set()
    for relation in relations:
        join = _object(relation.get('join_key'))
        if isinstance(join, dict):
            relationship_fields.update(_field(join.get(side)) for side in ('source_field', 'target_field'))
    result = []
    for index, predicate in enumerate(ast.get("filters") or []):
        field = predicate.get("field", "")
        if (field not in owners or field in relationship_fields
                or predicate.get("operator") not in {"=", "!=", "IN", "NOT IN"}):
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
                for dictionary in sorted(owners[key].keys() & owners[field].keys()):
                    for owner in owners[foreign].values():
                        choice = {
                            "field": foreign, "dictionary_key": key,
                            "dictionary_entity": dictionary,
                            "business_domain_id": owners[key][dictionary].get("business_domain_id") or owners[key][dictionary].get("business_domain"),
                            "owner": owner.get("entity_name") or owner.get("entity_code"),
                            "owner_entity": owner.get("entity_code"),
                            "relation": relation.get("relation_semantic") or relation.get("description") or "",
                        }
                        # Repeated published edges do not create a second
                        # business-owner choice when their executable keys agree.
                        identity = ('field', 'dictionary_key', 'dictionary_entity', 'business_domain_id', 'owner_entity')
                        if not any(all(existing.get(k) == choice.get(k) for k in identity) for existing in choices):
                            choices.append(choice)
        if choices:
            result.append({"filter_index": index, "predicate": predicate, "choices": choices})
    return result


def review_bindings(content, knowledge, question, extraction, model, resolve_keys,
                    semantic_model_id, domain_scope, *, today=None, explicit_time=False,
                    structured_only=False, relationship_required=None):
    ast = json.loads(content)
    options = binding_options(ast, knowledge)
    from related_scope import shared_scope_options, apply_shared_scope
    related_options = (shared_scope_options(ast, knowledge)
                       if ast.get('metrics') and relationship_required is not False else [])
    if not options and not related_options:
        return content, []
    selected = {m.get("name") for m in ast.get("metrics") or []}
    metrics = [_metadata(m) for m in knowledge.get("metrics", []) if _metadata(m).get("metric_code") in selected]
    entities = [_metadata(e) for e in knowledge.get("entities", [])]
    declared_filters = (extraction.get('过滤条件') or []) if isinstance(extraction, dict) else []
    origins = knowledge.get('_structured_filter_origins')
    entity_catalog = {entity['entity_code']: entity for entity in entities if entity.get('entity_code')}
    fields = {}
    for code, entity in entity_catalog.items():
        for attr in _object(entity.get('attributes')) or []:
            fields.setdefault(_field(attr.get('field_mapping')), set()).add(code)
    for item in knowledge.get('attributes', []):
        attr = _metadata(item)
        if attr.get('parent') in entity_catalog:
            fields.setdefault(_field(attr.get('field_mapping')), set()).add(attr['parent'])
    required_owners, directly_owned, source_filters = {}, set(), {}
    for option in options:
        index = option['filter_index']
        source_index = origins[index] if isinstance(origins, list) and len(origins) == len(ast.get('filters') or []) else index
        if type(source_index) is not int or not 0 <= source_index < len(declared_filters):
            continue
        original = declared_filters[source_index]
        source_filters[index] = original
        required = parameter_owners(original, {'entities': entity_catalog})
        if isinstance(original, dict) and not original.get('entity'):
            aliases = entity_label_candidates(original.get('field') or original.get('name'), entity_catalog)
            # A bare geography/dictionary label identifies the value catalog,
            # not whose location it is. Keep established entity aliases (e.g.
            # department aliases) intact; location ownership needs evidence.
            shared_location = (structured_only
                and re.search(r'province|city|district|county|region|省份|城市|地区|区域',
                              option['predicate']['field'], re.I)
                and len({c['owner_entity'] for c in option['choices']}) > 1)
            if len(aliases) == 1 and not shared_location:
                required = aliases
        required_owners[index] = required
        if required and fields.get(option['predicate']['field'], set()) & required:
            directly_owned.add(index)
    if not related_options and directly_owned == {option['filter_index'] for option in options}:
        # Already grounded on the requested entity, not a shared dictionary
        # that needs foreign-owner selection. No model/FK rewrite is needed.
        return content, []
    # A declared owner and one authorized edge need no second linguistic
    # decision. In strict planner mode an unowned shared dictionary must not
    # be kept merely because a model says so: SQL cannot recover the owner.
    deterministic = []
    pending = []
    for option in options:
        index = option['filter_index']
        required = required_owners.get(index) or set()
        if index in directly_owned:
            deterministic.append({'filter_index': index, 'keep': True,
                                  'reason': '字段已经属于结构化条件指定实体'})
        elif required:
            matches = [i for i, choice in enumerate(option['choices'])
                       if choice.get('owner_entity') in required]
            deterministic.append({'filter_index': index, 'bind_owner': True,
                                  'choice_index': matches[0] if len(matches) == 1 else None,
                                  'reason': '按结构化条件所属实体和已发布关系绑定'})
        elif structured_only and len({c['owner_entity'] for c in option['choices']}) > 1:
            deterministic.append({'filter_index': index})  # report missing ownership below
        else:
            pending.append(option)
    # Policy evidence is restricted to the selected metric / subject; an unrelated
    # recalled metric cannot supply a default period.
    subject = (ast.get("subject") or {}).get("entity")
    policies = [*metrics, *(e for e in entities if e.get("entity_code") == subject)]
    context = {"structured_extraction": extraction,
               "draft_asl": ast, "filter_options": [dict(option, choices=[
                   dict(choice, choice_index=i) for i, choice in enumerate(option['choices'])
               ]) for option in options], "selected_policies": policies,
               "shared_scope_options": [dict(option, option_index=i, target_filters=[
                   ast['filters'][j] for j in option['target_filter_indices']
               ]) for i, option in enumerate(related_options)],
               "current_date": (today or date.today()).isoformat()}
    question = json.dumps(extraction, ensure_ascii=False, separators=(',', ':'))
    try:
        messages = [
            {'role': 'system', 'content': (
                '仅将structured_extraction已声明的筛选归属和关联要求映射到授权目录。没有原问题，不得重新提取要求。'
                '不改主体、指标、分组、展示、排序、时间和限制。实体是表，不是条件值。'
                '根据过滤条件的字段/实体归属、已选指标语义与输出要求选择filter_options；无法判断返回error说明缺少哪个归属。'
                '先确定关联目标集合，再对不属于目标集合的filter_options项目逐项给bindings决定。'
                '默认保留已绑定字段和标准值，返回keep:true和reason。用户给名称就用标准名称条件，给编号就保留编号条件；'
                '不能仅因名称字段所在表存在关联键，就将商品名称、医院名称、品牌等改成订单外键或枚举编号集合。'
                '关系中的编号只用于表连接，不是把名称条件改成编号条件的理由；同名多编号应由名称条件匹配全部记录。'
                '只有共享字典的业务归属必须区分（例如同一个地区字典区分医院所在地和经销商所在地），'
                '且保留当前字典字段会丢失该归属时，才返回bind_owner:true、choice_index及具体归属依据。'
                '普通商品名称/编号筛选不属于这种归属改写。不要给keep:true的同时设置bind_owner:true。'
                '返回JSON：{"bindings":[{"filter_index":0,"keep":true,"reason":"保留已绑定的名称条件"}],'
                '"related_scope":{"mode":"direct"}}。若输出要求明确是共享属性关联而不是目标商品既有销售，'
                'related_scope改为{"mode":"shared_attribute","option_index":0,"evidence":"结构化输出要求中的逐字关系描述"}。'
                '只能选shared_scope_options中的路径；必须使用条目显式option_index/choice_index，不要自己数数组位置。'
                '选择前核对target_entity和target_filters是否是输出要求中的目标对象，而不是外层场所或排名对象；'
                '例如目标是商品的共同属性时不能选择医院的共同属性路径。不能见到科室就选关联模式。'
                '目标对象条件移入目标集合，其他条件保留；适用科室不代表实际成交科室。不得补任何条件。'
            )},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, default=str)},
        ]
        if pending or related_options:
            response = model.invoke(messages)
            decision = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip()))
        else:
            decision = {'bindings': [], 'related_scope': {'mode': 'direct'}}
        if not isinstance(decision, dict):
            raise ValueError('invalid binding response')
    except Exception:
        from structured_binding import issue
        ast.setdefault('ambiguity', []).append(issue('筛选归属/关联范围', extraction,
            '目录绑定服务未返回可用结果，请稍后重试；当前不能确认关联范围'))
        return json.dumps(ast, ensure_ascii=False), []
    repairs = []
    scope_probe = copy.deepcopy(ast)
    scope_repairs = apply_shared_scope(scope_probe, related_options, decision.get('related_scope'), question)
    target_indices = (set(related_options[decision['related_scope']['option_index']]['target_filter_indices'])
                      if scope_repairs else set())
    by_index = {o["filter_index"]: o for o in options}
    seen = set()
    bindings = decision.get("bindings")
    # Preserve index correspondence: owner requirements belong to each filter,
    # not the GROUP BY entity or overall execution subject.
    bindings = list(bindings) if isinstance(bindings, list) else []
    fixed_indices = {b['filter_index'] for b in deterministic}
    bindings = [b for b in bindings if isinstance(b, dict) and b.get('filter_index') not in fixed_indices]
    bindings.extend(deterministic)
    for index, option in by_index.items():
        source_index = origins[index] if isinstance(origins, list) and len(origins) == len(ast.get('filters') or []) else index
        if type(source_index) is not int or not 0 <= source_index < len(declared_filters) or index in target_indices:
            continue
        if index in directly_owned:
            bindings = [b for b in bindings if not isinstance(b, dict) or b.get('filter_index') != index]
            bindings.append({'filter_index': index, 'keep': True, 'reason': '筛选字段已经属于结构化参数指定的实体，保留标准名称和值'})
            continue
        required = required_owners.get(index) or set()
        if not required:
            continue
        bindings = [b for b in bindings if not isinstance(b, dict) or b.get('filter_index') != index]
        matches = [i for i, choice in enumerate(option['choices']) if choice.get('owner_entity') in required]
        if len(matches) == 1:
            bindings.append({'filter_index': index, 'choice_index': matches[0], 'bind_owner': True,
                             'reason': '结构化条件明确指定所属实体，按已发布关系绑定该实体'})
        elif required:
            bindings.append({'filter_index': index})  # unresolved owner cannot silently keep a shared predicate
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, dict):
            continue
        index, choice_index = binding.get("filter_index"), binding.get("choice_index")
        if (type(index) is int and index in by_index and binding.get('keep') is True
                and binding.get('bind_owner') is not True and binding.get('reason')):
            seen.add(index)
            continue
        if type(index) is int and index in target_indices:
            continue  # Target conditions are already scoped; do not rebind onto the bridge.
        if (type(index) is not int or index in seen or index not in by_index or type(choice_index) is not int
                or not 0 <= choice_index < len(by_index[index]["choices"]) or not binding.get("reason")):
            continue
        choice = by_index[index]["choices"][choice_index]
        # Selecting a JOIN edge alone does not authorize changing a user's
        # name predicate into an enumerated ID set. FK resolution is reserved
        # for an explicitly requested business-owner disambiguation.
        if binding.get('bind_owner') is not True:
            seen.add(index)
            continue
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
        seen.add(index)
        operator = old["operator"]
        if len(keys) > 1 or operator in {"IN", "NOT IN"}:
            operator = "NOT IN" if operator in {"!=", "NOT IN"} else "IN"
            value = keys
        else:
            value = keys[0]
        ast["filters"][index] = dict(old, field=choice["field"], operator=operator, value=value)
        repairs.append({"type": "RESOLVE_FILTER_BUSINESS_OWNER", "previous_filter": old,
                        "resolved_filter": ast["filters"][index], "reason": str(binding["reason"])[:300],
                        "owner_entity": choice['owner_entity'],
                        "source_filter": copy.deepcopy(source_filters.get(index)),
                        "source": "SCOPED_RELATION_AND_DICTIONARY"})
        knowledge.setdefault('_filter_owner_bindings', []).append(copy.deepcopy(repairs[-1]))
    from structured_binding import issue
    for option in options:
        index = option['filter_index']
        if index in target_indices:
            continue
        if index not in seen:
            ast.setdefault('ambiguity', []).append(issue('过滤条件归属', option['predicate'],
                '无法确认该筛选作用于哪个业务实体或无法解析对应标准值',
                [c['owner'] for c in option['choices']]))
    if scope_repairs:
        repairs.extend(apply_shared_scope(ast, related_options, decision.get('related_scope'), question))
    return json.dumps(ast, ensure_ascii=False), repairs
