"""Bind planner slots to finite catalog candidates; never extract from a question."""
import json
import re
from datetime import date
from query_binding_review import _metadata, _object, _field, _time_value

SECTIONS = {'指标': 'metrics', '维度': 'dimensions', '展示字段': 'display_fields', '过滤条件': 'filters', '排序': 'sort'}
OPERATORS = {'=', '!=', '>', '>=', '<', '<=', 'IN', 'NOT IN', 'LIKE', 'BETWEEN'}


def filter_value_queries(extraction):
    """One bounded value recall per declared field/value, not the whole request."""
    queries = []
    for item in extraction.get('过滤条件') or []:
        if not isinstance(item, dict) or not item.get('field'):
            continue
        values = item.get('value', [])
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, str) and value.strip():
                query = ' '.join(str(item[k]).strip() for k in ('entity', 'field')
                                 if item.get(k)) + ' ' + value
                if query not in queries:
                    queries.append(query)
    return queries


def retrieval_terms(extraction):
    """Recall each declared business parameter, not words inferred from prose."""
    terms = []
    def add(value):
        if isinstance(value, str) and value.strip() and value not in terms:
            terms.append(value)
    for section in ('实体', '指标', '维度', '展示字段', '过滤条件', '排序'):
        for item in extraction.get(section) or []:
            if isinstance(item, str): add(item)
            elif isinstance(item, dict):
                for key in ('entity', 'name', 'field'): add(item.get(key))
                values = item.get('value', [])
                for value in values if isinstance(values, list) else [values]:
                    add(value)
    return terms


def issue(slot, value, reason, candidates=()):
    label = json.dumps(value, ensure_ascii=False)
    phrase = value
    if isinstance(value, dict):
        values = value.get('value')
        phrase = (values[0] if isinstance(values, list) and len(values) == 1
                  else values if isinstance(values, str)
                  else value.get('name') or value.get('field'))
    if not isinstance(phrase, str):
        phrase = label
    prefix = slot.split('[', 1)[0]
    kind = {'指标':'metric','维度':'dimension','展示字段':'dimension','过滤条件':'filter_slot',
            '过滤条件归属':'entity_role','时间粒度':'time_anchor','实体':'subject',
            '输出要求':'schema_relation','排序':'operation_intent','限制':'operation_intent',
            '指标/展示字段':'operation_intent'}.get(prefix,'context')
    return {'type': kind, 'field': slot, 'phrase': phrase,
            'affected_slots': [kind], 'question': f'结构化参数【{slot}】{label}：{reason}。请补充或确认该项；已明确的其他条件无需重复提供。',
            'candidates': list(candidates),
            'candidate_details': [dict(canonical_name=name, structured_slot=slot, source_parameter=value)
                                  for name in candidates],
            'source': 'STRUCTURED_EXTRACTION'}


def catalog_candidates(knowledge):
    entities, metrics, fields, dimensions = {}, {}, {}, {}
    allowed = set(knowledge.get('_vector_authorized_fields') or [])
    for item in knowledge.get('entities', []):
        meta = _metadata(item); code = meta.get('entity_code')
        if code:
            entities[code] = meta
        for attr in _object(meta.get('attributes')) or []:
            field = _field(attr.get('field_mapping'))
            if field in allowed:
                fields[field] = dict(attr, owner=code)
    for item in knowledge.get('attributes', []):
        meta = _metadata(item); field = _field(meta.get('field_mapping'))
        if field in allowed:
            fields.setdefault(field, meta)
    for item in knowledge.get('metrics', []):
        meta = _metadata(item)
        if meta.get('metric_code'): metrics[meta['metric_code']] = meta
    for item in knowledge.get('dimensions', []):
        meta = _metadata(item)
        if meta.get('dim_code'): dimensions[meta['dim_code']] = meta
    values = []
    value_pool = [*knowledge.get('entity_attribute_values', []),
                  *(knowledge.get('_ambiguity_candidates') or {}).get('entity_attribute_value', [])]
    for item in value_pool:
        meta = _metadata(item); field = _field(meta.get('field_mapping') or meta.get('source_field'))
        if field and '.' not in field:
            field = f"{meta.get('source_table')}.{field}" if meta.get('source_table') else ''
        if not field:
            owners = {code for code, entity in entities.items()
                      if meta.get('entity_code') == code or meta.get('entity_name') in (code, entity.get('entity_name'))}
            matches = [key for key, attr in fields.items() if attr.get('owner') in owners
                       and meta.get('attr_code') == attr.get('attr_code')]
            if len(matches) == 1: field = matches[0]
        value = meta.get('attr_value', meta.get('canonical_value'))
        if field in allowed and value is not None:
            record = {'field': field, 'value': value}
            if record not in values: values.append(record)
    values = [dict(record, id=index) for index, record in enumerate(values)]
    return dict(entities=entities, metrics=metrics, fields=fields, dimensions=dimensions, values=values)


def _group_exposes_field(group, field, catalog):
    """Recognize the identity/label pair already projected by SQL Translator."""
    code = group['name']
    if code == field:
        return True
    definition = catalog['dimensions'].get(code)
    entity = catalog['entities'].get(code)
    if not definition or not entity or group.get('granularity') or definition.get('enum_list'):
        return False
    mappings = _object(definition.get('bind_entities')) or []
    if not isinstance(mappings, list):
        return False
    fields = set()
    direct = _field(definition.get('field_mapping'))
    if direct:
        fields.add(direct)
    for mapping in mappings:
        if not isinstance(mapping, dict):
            continue
        if group.get('attr') and mapping.get('attr') != group['attr']:
            continue
        table, column = mapping.get('mappingTable'), mapping.get('mappingColumn')
        if table and column:
            fields.add(f'{table}.{column}')
    if field in fields:
        return True
    attrs = _object(entity.get('attributes')) or []
    flag = lambda value: str(value).lower() in {'true', '1', 'yes'}
    identities = {a.get('field_mapping') for a in attrs if a.get('field_mapping') in fields
                  and (flag(a.get('is_primary_key')) or a.get('attr_code') in {code+'_id', code+'_code'})}
    labels = {a.get('field_mapping') for a in attrs
              if (flag(a.get('is_main_attribute')) or a.get('attr_code') == code+'_name')
              and any(str(a.get('field_mapping', '')).split('.')[0] == identity.split('.')[0]
                      for identity in identities)}
    return len(labels) == 1 and field in labels


def bind(extraction, knowledge, model, *, today=None, detail_subject_resolver=None):
    """Model chooses bindings; deterministic assembly owns shape/operators/literals."""
    ast = dict(version='2.0', intent='query', subject={}, metrics=[], dimensions=[], filters=[],
               time_context=None, sort=None, limit=None, having=[], ambiguity=[])
    repairs = []
    if not isinstance(extraction, dict):
        ast['ambiguity'] = [issue('结构化提取', extraction, '未收到有效结构化参数，不能根据原问题重新提取')]
        return ast, repairs
    for key in SECTIONS:
        if not isinstance(extraction.get(key), list):
            ast['ambiguity'].append(issue(key, extraction.get(key), '缺少参数数组，空数组也应显式提供'))
    if ast['ambiguity']: return ast, repairs
    catalog = catalog_candidates(knowledge)
    # Do not repeat full entity attributes/relations alongside the same fields.
    # Keep binding labels visible instead of burying value candidates in schema
    # serialization; relationship ownership is resolved in its dedicated step.
    def select(meta, keys):
        return {key: meta[key] for key in keys if meta.get(key) is not None}
    prompt_catalog = dict(catalog)
    prompt_catalog['entities'] = {key: select(meta, ('entity_name', 'entity_alias', 'description'))
                                 for key, meta in catalog['entities'].items()}
    prompt_catalog['fields'] = {key: select(meta, ('attr_name', 'attr_code', 'description', 'owner', 'data_type'))
                               for key, meta in catalog['fields'].items()}
    context = {'catalog': prompt_catalog, 'current_date': (today or date.today()).isoformat(),
               'structured_extraction': extraction}
    prompt = '''你只做结构化参数到授权目录的绑定，不做自然语言问题提取。structured_extraction 是唯一业务要求。
实体数组表示涉及的表，不是筛选值；指标/维度/展示字段/过滤/排序/时间/限制的角色及数量不得更改。
目录只用于标准化，不允许从目录说明补指标、条件、默认时间或分组。空数组表示没有该要求。
结合参数内 entity、field、输出要求判断字段归属；信息不足就返回 error 和目录候选，不猜业务要求。
每个参数逐项绑定。返回 JSON: {subject:目录实体编码, metrics:[{index:0,key:目录指标编码}], dimensions:[{index:0,key:维度编码或物理字段}], display_fields:[{index:0,key:物理字段}], filters:[{index:0,key:物理字段,value_ids:[目录values序号]}], sort:[{index:0,key:目录指标编码或物理字段}], time:{mode:keep|range|rolling|calendar,anchor:目录时间字段,amount:整数,unit:day|week|month|year,start:日期,end:日期,type:时间类型,value:年份}, relationship_required:false}。
每个输入 index 恰好返回一项，无法匹配的项改为 {index:0,error:具体原因,candidates:[标准候选名称]}。
过滤值是数值阈值、日期、编码/型号时不需要 value_ids，原值原样保留。字符串可用 values 中同字段的标准值替换，逐输入值提供一个序号；不能凭空创造值、截断型号或扩大集合。
按结构化条件的字段含义选择名称字段或编号字段，不按值中有无数字/字母猜测：商品名称对应目录名称字段，商品编号对应目录编号字段。名称匹配后保留标准名称，不能为了表连接而改绑编码字段；编号按原值精确筛选，保留前导零，不用相似名称替换。
名称、品牌、分类等中文等值条件必须联合绑定字段和标准值：先找能表达该业务值的catalog.values条目，再使用该条目的field与id，不能仅凭字段标题相似就把原词填到无对应标准值的字段。多级分类尤其要按标准值所在层级绑定；同一“商品品类”可映射产品类别或一级/二级分类，取决于值的目录证据。找不到对应标准值则返回该参数error，不伪装成已验证。
只绑定已声明的时间范围，缺少时间锚点则 time={error:具体原因}；未声明时间范围时 time=null。不增加默认范围。
没有指标时 dimensions/display_fields 表示明细列；有指标时 dimensions 才是分组，展示字段不能变成额外分组。
实体数组列出本次涉及的业务对象，不是要求用户从中选一个；同时出现多个实体不构成歧义。展示字段的 entity/field 表示要返回的对象，过滤条件表示限定哪些记录。
subject 是执行查询的主表：指标查询依据已选指标的实体绑定；明细查询结合已绑定展示字段、过滤字段及目录关系选择主体，允许使用授权目录中连接这些对象的关联实体，不要求主体必须出现在结构化实体数组中。不能据此增加指标、分组或筛选条件。无法判断时 subject=null 并给 subject_error。
输出要求含明确的共享属性关联范围（如以目标商品适用科室寻找相关渠道），relationship_required=true；不能只看到实体中有科室就判定。不生成 SQL 或其他业务要求。'''
    prompt += ('value_ids必须使用catalog.values条目的显式id，不要自己数数组位置。'
               '维度绑定可带attr（目录中该维度已有的属性ID）和granularity；只有时间维度才能设置granularity，'
               '且必须对应结构化时间粒度unit，不能自行选择默认粒度或新增时间维度。')
    try:
        result = model.invoke([{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, default=str)}])
        plan = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', result.content.strip()))
        if not isinstance(plan, dict): raise ValueError('binding response must be object')
    except Exception:
        ast['ambiguity'] = [issue('目录绑定', extraction, '绑定服务未返回可用结果，请稍后重试；不是用户参数缺失')]
        return ast, repairs
    subject = plan.get('subject')
    # Accept the ordinary ASL object shape as well as the compact binding key.
    if isinstance(subject, dict): subject = subject.get('entity')
    if isinstance(subject, str): subject = subject.strip()
    if isinstance(subject,str) and subject in catalog['entities']: ast['subject'] = {'entity': subject}
    for source, target in SECTIONS.items():
        rows = plan.get(target) or []
        if not isinstance(rows, list): rows = []
        for index, original in enumerate(extraction[source]):
            label = (original.get('name') or original.get('field')) if isinstance(original,dict) else original
            if not isinstance(label,str) or not label.strip():
                ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,'上游未明确该参数的业务字段或指标名称'))
                continue
            found = [r for r in rows if isinstance(r, dict) and type(r.get('index')) is int and r['index'] == index]
            choice = found[0] if len(found) == 1 else {}
            key = choice.get('key')
            allowed = (catalog['metrics'] if target == 'metrics' else
                       {**catalog['dimensions'], **catalog['fields']} if target == 'dimensions' else
                       {**catalog['metrics'], **catalog['dimensions'], **catalog['fields']} if target == 'sort' else catalog['fields'])
            if choice.get('error') or not isinstance(key,str) or key not in allowed:
                reason = choice.get('error') or '未匹配到可执行的标准字段'
                if target == 'display_fields' and not extraction['指标'] and any(isinstance(r.get('key'),str) and r.get('key') in catalog['fields'] for r in rows if isinstance(r, dict)):
                    # Existing partial display behavior; filters/groupings remain mandatory.
                    repairs.append({'type': 'OMIT_UNAVAILABLE_DISPLAY_FIELD', 'source': 'VECTOR_DISPLAY_PROJECTION',
                                    'field': original.get('field') if isinstance(original, dict) else original,
                                    'entity': original.get('entity', '') if isinstance(original, dict) else ''})
                else:
                    problem = issue(f'{source}[{index+1}]', original, reason, choice.get('candidates') or [])
                    if target == 'filters':
                        for detail in problem['candidate_details']:
                            label = detail['canonical_name']
                            if any(str(v['value']) == label for v in catalog['values']):
                                detail['binding_kind'] = 'value'
                            elif label in catalog['fields'] or any(
                                    f.get('attr_name') == label for f in catalog['fields'].values()):
                                detail['binding_kind'] = 'field'
                    ast['ambiguity'].append(problem)
                continue
            if target == 'metrics':
                ast['metrics'].append({'name': key, 'alias': allowed[key].get('metric_name') or key, 'time_anchor': None})
            elif target in {'dimensions', 'display_fields'}:
                if target == 'display_fields' and extraction['指标']:
                    if not any(_group_exposes_field(d, key, catalog) for d in ast['dimensions']):
                        ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '聚合查询的展示列没有声明为分组维度，请上游明确展示口径'))
                elif key not in {d['name'] for d in ast['dimensions']}:
                    units = {'时':'hour','小时':'hour','日':'day','天':'day','周':'week','月':'month','季度':'quarter','季':'quarter','年':'year'}
                    temporal = extraction.get('时间粒度') or {}
                    unit = temporal.get('unit') if isinstance(temporal,dict) else None
                    unit = units.get(unit, unit)
                    granularity = choice.get('granularity')
                    if granularity is not None and (granularity != unit or unit not in set(units.values())):
                        ast['ambiguity'].append(issue(source,original,'时间分组粒度与结构化参数不一致'))
                        continue
                    ast['dimensions'].append(dict(name=key, attr=choice.get('attr') if target=='dimensions' and '.' not in key else None,
                                                  level=None, granularity=granularity))
            elif target == 'filters':
                if not isinstance(original, dict):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '条件必须包含 field/op/value'));continue
                operator = str(original.get('op') or original.get('operator') or '').upper()
                value = original.get('value'); vals = value if isinstance(value, list) else [value]
                if operator not in OPERATORS or not vals or any(v is None or isinstance(v, (dict,list,bool)) or v == '' for v in vals):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '运算符或条件值缺失/不支持'));continue
                ids = choice.get('value_ids') or []
                if ids:
                    if (not isinstance(ids,list) or len(ids)!=len(vals) or any(type(i) is not int or not 0<=i<len(catalog['values']) for i in ids)
                            or any(catalog['values'][i]['field']!=key for i in ids)
                            or any(not isinstance(v,str) for v in vals)):
                        ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '标准值绑定与字段或输入值数量不一致'));continue
                    bound_vals=[catalog['values'][i]['value'] for i in ids]
                    if any(re.search(r'[0-9A-Za-z]',v) and v!=str(b) for v,b in zip(vals,bound_vals)):
                        ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,'编码、型号或数字字符串不得被替换成其他值'));continue
                    vals=bound_vals
                elif operator in {'=','!=','IN','NOT IN'} and any(
                    isinstance(v,str) and re.search(r'[\u4e00-\u9fff]',v) for v in vals
                ) and not all(any(c['field']==key and c['value']==v for c in catalog['values']) for v in vals):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,
                        f'字段 {key} 已匹配，但条件值尚未匹配到该字段的向量标准值，请核对字段层级及值目录'))
                    continue
                if len(vals)>1 and operator in {'=','!='}: operator='IN' if operator=='=' else 'NOT IN'
                if (operator=='BETWEEN' and len(vals)!=2) or (len(vals)>1 and operator not in {'IN','NOT IN','BETWEEN'}):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '该运算符与多个条件值不兼容'));continue
                ast['filters'].append({'field':key,'operator':operator,'value':vals if operator in {'IN','NOT IN','BETWEEN'} else vals[0]})
            elif target == 'sort':
                direction = str(original.get('order') or original.get('direction') or '').upper() if isinstance(original,dict) else ''
                if len(extraction[source])>1 or direction not in {'ASC','DESC'}:
                    ast['ambiguity'].append(issue(source, original, '当前ASL仅支持一个明确升序/降序的排序字段'));continue
                ast['sort']={'field':key,'direction':direction,'field_type':'metric' if key in catalog['metrics'] else 'field' if '.' in key else 'dimension'}
        if any(not isinstance(r,dict) or type(r.get('index')) is not int or not 0<=r['index']<len(extraction[source]) for r in rows):
            ast['ambiguity'].append(issue(source, extraction[source], '绑定输出添加了结构化提取中不存在的参数'))
    limit=extraction.get('限制')
    if limit is not None and (type(limit) is not int or not 1<=limit<=10000):
        ast['ambiguity'].append(issue('限制',limit,'需要1到10000的整数'))
    else:ast['limit']=limit
    temporal=extraction.get('时间粒度') or {}
    if not isinstance(temporal,dict):
        ast['ambiguity'].append(issue('时间粒度',temporal,'时间参数格式不完整'))
    elif temporal.get('time_range'):
        decision=plan.get('time') or {}
        if not isinstance(decision,dict):decision={'error':'时间绑定格式无效'}
        try:
            if decision.get('anchor') not in catalog['fields']:raise ValueError('未匹配到授权的时间字段')
            ast['time_context']=_time_value(decision,today or date.today())
            if ast['time_context'] is None:raise ValueError('声明的时间范围未绑定')
        except (ValueError,TypeError,KeyError,OverflowError) as exc:
            ast['ambiguity'].append(issue('时间粒度',temporal,decision.get('error') or str(exc)))
    if not ast['metrics'] and not ast['dimensions'] and not ast['ambiguity']:
        ast['ambiguity'].append(issue('指标/展示字段', [], '未声明要计算的指标或返回的字段，不能自行补医院名称或计数'))
    if not ast['subject'] and not ast['ambiguity']:
        candidate = None
        if not extraction['指标'] and ast['dimensions'] and detail_subject_resolver is not None:
            # Reuse the existing scoped relation-graph binding, not raw question
            # extraction or a hardcoded business table. Only a unique hub wins.
            candidate = detail_subject_resolver(ast, knowledge)
        if isinstance(candidate, str) and candidate in catalog['entities']:
            ast['subject'] = {'entity': candidate}
            repairs.append({'type': 'STRUCTURED_SUBJECT_BOUND', 'source': 'SCOPED_RELATION_GRAPH',
                            'entity': candidate})
        else:
            problem = issue('实体', extraction.get('实体'),
                            plan.get('subject_error') or '未能确定连接已绑定字段的查询主体')
            problem['question'] = (
                '查询主体绑定未完成：已识别涉及实体 ' + json.dumps(extraction.get('实体'), ensure_ascii=False)
                + '，已绑定返回字段 ' + json.dumps([d['name'] for d in ast['dimensions']], ensure_ascii=False)
                + '；模型未给出可执行主体，当前目录也未能唯一确定连接这些字段和筛选条件的主体。'
                '请核对实体物理映射与关联关系；不需要重复解释已明确的实体或重新输入原问题。'
            )
            ast['ambiguity'].append(problem)
    repairs.append({'type':'STRUCTURED_BINDING_ONLY','source':'STRUCTURED_EXTRACTION',
                    'relationship_required':plan.get('relationship_required') is True})
    return ast, repairs
