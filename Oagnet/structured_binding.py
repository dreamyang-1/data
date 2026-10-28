"""Bind planner slots to finite catalog candidates; never extract from a question."""
import json
import logging
import re
from datetime import date, datetime, timedelta
from query_binding_review import _metadata, _object, _field
from structured_time import compile_time, grouping_grain, is_temporal, dimension_fields, normalize_unit, time_anchor

logger = logging.getLogger(__name__)
from binding_ownership import (bound_filter_catalog, entity_label_candidates, field_owners, matching_dimensions, matching_fields,
                               normalized, parameter_catalog, registered_terms, resolve_entities, value_allowed)
from binding_errors import invoke_binding_object

SECTIONS = {'指标': 'metrics', '维度': 'dimensions', '展示字段': 'display_fields', '过滤条件': 'filters', '排序': 'sort'}
OPERATORS = {'=', '!=', '>', '>=', '<', '<=', 'IN', 'NOT IN', 'LIKE', 'BETWEEN'}


def _grain_slot_unit(label):
    """维度槽位出现纯时间粒度词（季度/月度/年度…）时还原成分组粒度。

    结构化抽取常把“季度对比”拆出「季度」维度槽位，而 ASL 合同规定
    时间粒度由程序依据结构化时间参数赋值、不作为维度返回；该词形
    不能被当成缺失的业务维度拦停澄清。
    """
    if not isinstance(label, str):
        return None
    text = re.sub(r'\s+', '', label)
    text = re.sub(r'^(?:按|以)', '', text)
    text = re.sub(r'(?:对比|分布|趋势|汇总|统计|粒度|维度)$', '', text)
    return normalize_unit(text)


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


def apply_surface_requirements(ast, extraction):
    """结构化参数是 ASL 的唯一业务要求来源（2026-10-21 用户要求）。

    问题式生成、绑定修复或契约补全都不得丢失结构化声明过的行数限制
    与明细去重口径；在最终 ASL 上确定性回填，返回追加的 repairs。
    限制合同与 bind 一致：1~10000 的整数；去重仅在无指标的明细投影上生效。
    """
    repairs = []
    if not isinstance(ast, dict) or ast.get('ambiguity') or not isinstance(extraction, dict):
        return repairs
    limit = extraction.get('限制')
    if (isinstance(limit, int) and not isinstance(limit, bool)
            and 1 <= limit <= 10000 and ast.get('limit') != limit):
        ast['limit'] = limit
        repairs.append({'type': 'APPLY_STRUCTURED_LIMIT',
                        'source': 'STRUCTURED_EXTRACTION', 'limit': limit})
    dedup = str(extraction.get('是否去重') or '').strip().casefold()
    if (not ast.get('metrics') and ast.get('dimensions')
            and str(ast.get('projection_mode') or '').strip().upper() not in {'DISTINCT', 'ROWS'}
            and dedup in {'是', 'true', '1', 'yes', 'y', 'distinct'}):
        ast['projection_mode'] = 'DISTINCT'
        repairs.append({'type': 'APPLY_STRUCTURED_DEDUP',
                        'source': 'STRUCTURED_EXTRACTION'})
    return repairs


def catalog_candidates(knowledge):
    entities, metrics, fields, dimensions = {}, {}, {}, {}
    allowed = set(knowledge.get('_vector_authorized_fields') or [])
    def add_field(field, attr, owner):
        if field not in allowed:
            return
        existing = fields.get(field) or {}
        bindings = list(existing.get('attribute_bindings') or [])
        binding = dict(attr, owner=owner)
        if binding not in bindings:
            bindings.append(binding)
        fields[field] = dict(attr, owner=owner, owners=sorted(field_owners(existing) | ({owner} if owner else set())),
                             attribute_bindings=bindings)
    for item in knowledge.get('entities', []):
        meta = _metadata(item); code = meta.get('entity_code')
        if code:
            entities[code] = meta
        for attr in _object(meta.get('attributes')) or []:
            field = _field(attr.get('field_mapping'))
            add_field(field, attr, code)
    for item in knowledge.get('attributes', []):
        meta = _metadata(item); field = _field(meta.get('field_mapping'))
        add_field(field, meta, meta.get('parent'))
    for item in knowledge.get('metrics', []):
        meta = _metadata(item)
        if meta.get('metric_code'): metrics[meta['metric_code']] = meta
    for item in knowledge.get('dimensions', []):
        meta = _metadata(item)
        if meta.get('dim_code'): dimensions[meta['dim_code']] = meta
        if is_temporal(meta):
            for field in dimension_fields(meta) & allowed:
                fields.setdefault(field, {'attr_name': meta.get('dim_name'), 'data_type': 'DATE'})
    values, value_owners = [], {}
    def add_value(field, value, label=None, attr_name=None, owners=()):
        """Add one executable standard value without duplicating aliases."""
        if field not in allowed or value is None:
            return
        record = {'field': field, 'value': value}
        if label is not None and str(label) != str(value):
            record['label'] = label
        if attr_name:
            record['attr_name'] = attr_name
        matching = next((i for i, item in enumerate(values) if item.get('field') == field and item.get('value') == value), None)
        if matching is None:
            matching = len(values)
            values.append(record)
        value_owners.setdefault(matching, set()).update(owners)

    value_pool = [*knowledge.get('entity_attribute_values', []),
                  *(knowledge.get('_ambiguity_candidates') or {}).get('entity_attribute_value', [])]
    for item in value_pool:
        meta = _metadata(item); field = _field(meta.get('field_mapping') or meta.get('source_field'))
        if field and '.' not in field:
            field = f"{meta.get('source_table')}.{field}" if meta.get('source_table') else ''
        if not field:
            owners = {code for code, entity in entities.items()
                      if meta.get('entity_code') == code or meta.get('entity_name') in (code, entity.get('entity_name'))}
            matches = [key for key, attr in fields.items() if any(
                field_owners(binding) & owners and meta.get('attr_code') == binding.get('attr_code')
                for binding in attr.get('attribute_bindings') or [attr])]
            if len(matches) == 1: field = matches[0]
        # A field-qualified value must still agree with its published owner.
        declared = resolve_entities(meta.get('entity_code') or meta.get('entity_name'), entities)
        known = field_owners(fields.get(field) or {})
        if meta.get('entity_code') and known and meta['entity_code'] not in known:
            continue
        if declared and known and not declared.intersection(known):
            continue
        attr_code = meta.get('attr_code')
        expected = fields.get(field) or {}
        bindings = [attr for attr in expected.get('attribute_bindings') or [expected]
                    if not declared or not field_owners(attr) or field_owners(attr) & declared]
        if attr_code and bindings and all(attr.get('attr_code') and attr_code != attr['attr_code'] for attr in bindings):
            continue
        value = meta.get('attr_value', meta.get('canonical_value'))
        # attr_name is the field label, not the display value.  Only an
        # explicitly published value label may be used for enum matching.
        add_value(field, value, meta.get('label'), meta.get('attr_name'), declared or known)

    # Include enumerations published on physical fields or scoped dimensions.
    # They are valid vector-grounded filter values even when no separate
    # entity_attribute_value record was recalled.
    for field, meta in fields.items():
        for attr in meta.get('attribute_bindings') or [meta]:
            for enum in attr.get('enum_values') or []:
                if not isinstance(enum, dict):
                    continue
                code = enum.get('value', enum.get('code'))
                label = enum.get('name', enum.get('label'))
                add_value(field, code if code is not None else label, label, owners=field_owners(attr))
    for dimension in dimensions.values():
        mappings = _object(dimension.get('bind_entities')) or []
        mapped_fields = []
        direct = _field(dimension.get('field_mapping'))
        if direct:
            mapped_fields.append(direct)
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            table = mapping.get('mappingTable')
            column = mapping.get('mappingColumn')
            if table and column:
                mapped_fields.append(f'{table}.{column}')
        for field in dict.fromkeys(mapped_fields):
            for enum in dimension.get('enum_list') or []:
                if not isinstance(enum, dict):
                    continue
                code = enum.get('code', enum.get('value'))
                label = enum.get('name', enum.get('label'))
                add_value(field, code if code is not None else label, label)
    values = [dict(record, id=index) for index, record in enumerate(values)]
    relations = [_metadata(item) for item in knowledge.get('relations', [])]
    for entity in entities.values():
        relations.extend(_object(entity.get('relations')) or [])
    from query_binding_review import binding_options
    options = binding_options({'filters': [{'field': field, 'operator': '=', 'value': None} for field in fields]}, knowledge)
    dictionary_owners = {}
    dictionary_choices = {}
    for option in options:
        dictionary_owners[option['predicate']['field']] = {choice['owner_entity'] for choice in option['choices']}
        dictionary_choices[option['predicate']['field']] = option['choices']
    return dict(entities=entities, metrics=metrics, fields=fields, dimensions=dimensions, values=values,
                relations=relations, _dictionary_owners=dictionary_owners,
                _dictionary_choices=dictionary_choices, _value_owners=value_owners)


def _dim_bound_field(code, catalog):
    """把维度编码翻译成目录授权的物理字段；映射不唯一或未授权时返回 None。"""
    meta = catalog['dimensions'].get(code)
    if not isinstance(meta, dict):
        return None
    fields = set()
    direct = _field(meta.get('field_mapping'))
    if direct and direct in catalog['fields']:
        fields.add(direct)
    for mapping in _object(meta.get('bind_entities')) or []:
        if not isinstance(mapping, dict):
            continue
        table, column = mapping.get('mappingTable'), mapping.get('mappingColumn')
        if table and column and f'{table}.{column}' in catalog['fields']:
            fields.add(f'{table}.{column}')
    return fields.pop() if len(fields) == 1 else None


def _dimension_filter_field(code, original, catalog):
    """Resolve a dimension predicate using its own governed fields and values.

    A dimension's ID mappings may legitimately have a name predicate. Resolve
    it before exact-label correction overwrites a valid physical field. Never
    use unrelated entities' same-valued attributes or a model's rank as proof.
    """
    if not isinstance(original, dict) or str(original.get('op') or original.get('operator') or '').upper() not in {'=', '!=', 'IN', 'NOT IN'}:
        return None
    values = original.get('value')
    values = values if isinstance(values, list) else [values]
    if not values or any(value is None or isinstance(value, (dict, list, bool)) for value in values):
        return None
    definition = catalog['dimensions'].get(code) or {}
    mapped = {_field(definition.get('field_mapping'))}
    for item in _object(definition.get('bind_entities')) or []:
        if isinstance(item, dict) and item.get('mappingTable') and item.get('mappingColumn'):
            mapped.add(f"{item['mappingTable']}.{item['mappingColumn']}")
    mapped &= catalog['fields'].keys()
    candidates = set(mapped)
    for field in mapped:
        if not _field_is_identifier(field, catalog['fields'][field]):
            continue
        for name_field in _name_field_candidates(field, catalog):
            # A common suffix alone is not an entity relationship. Names must
            # be on the mapped table or share its registered logical owner.
            if (name_field.split('.')[0] == field.split('.')[0]
                    or field_owners(catalog['fields'][name_field]) & field_owners(catalog['fields'][field])):
                candidates.add(name_field)
    hits = []
    for field in sorted(candidates):
        matches = [_vector_matches(field, value, catalog) for value in values]
        if matches and all(len(match) == 1 for match in matches):
            hits.append(field)
    # Exact canonical name evidence can beat an ID's display-label alias, but
    # two owners/fields carrying the literal itself are still ambiguous.
    exact = [field for field in hits if all(any(
        value_allowed(record, catalog) and record['field'] == field and record['value'] == value
        for record in catalog['values']) for value in values)]
    unique = exact or hits
    return unique[0] if len(unique) == 1 else None


def _metric_subject_candidates(metric_keys, catalog):
    """Use published execution bindings, never the order of involved entities."""
    candidates = []
    for key in metric_keys:
        dependency = _object((catalog['metrics'].get(key) or {}).get('source_dependency'))
        if not isinstance(dependency, dict):
            continue
        bound = _object(dependency.get('bind_entity'))
        if isinstance(bound, str):
            bound = [bound]
        if not isinstance(bound, list):
            continue
        for entity in bound:
            if isinstance(entity, str) and entity in catalog['entities'] and entity not in candidates:
                candidates.append(entity)
    return candidates


def _vector_surface(value):
    import unicodedata
    if not isinstance(value, str):
        return value
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value).strip()).casefold()


# 类别词收尾的差异视为同一事物（协和医院=协和）；"科"不入表，杜绝心内科=内科这类误伤
_VALUE_SUFFIX_WORDS = ('省', '市', '自治区', '特别行政区', '自治州', '盟', '医院', '卫生院',
                       '诊所', '门诊部', '公司', '集团', '厂家', '品牌', '大学', '学院')
_PROVINCE_SHORT_NAMES = ('北京', '天津', '上海', '重庆', '河北', '山西', '辽宁', '吉林',
                         '黑龙江', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南',
                         '湖北', '湖南', '广东', '海南', '四川', '贵州', '云南', '陕西',
                         '甘肃', '青海', '台湾', '内蒙古', '广西', '西藏', '宁夏', '新疆',
                         '香港', '澳门')


def _vector_value_equivalent(surface, canonical):
    if not isinstance(surface, str) or not isinstance(canonical, str):
        return surface == canonical
    left, right = _vector_surface(surface), _vector_surface(canonical)
    if left == right:
        return True
    # 输入与标准值互为"前缀+类别词"或"地域+主体"时视为同一事物：
    # 三级医院=三级、上海市=上海、江苏苏云=苏云、云南白药=白药。
    # 差异部分必须在白名单内精确匹配，防止任意包含关系把不同机构绑到一起
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    if not shorter or len(shorter) < 2:
        return False
    if longer.startswith(shorter):
        suffix = longer[len(shorter):]
        return len(suffix) <= 8 and suffix in _VALUE_SUFFIX_WORDS
    if longer.endswith(shorter):
        prefix = longer[:-len(shorter)]
        return len(prefix) <= 3 and prefix in _PROVINCE_SHORT_NAMES
    return False


# Structured extraction is authoritative. These helpers only map an already
# declared slot to one exact catalog key; they never create a new slot.
def _metadata_terms(metadata):
    if not isinstance(metadata, dict):
        return []
    terms = []
    for name in ('metric_code', 'metric_name', 'dim_code', 'dim_name',
                 'attr_code', 'attr_name', 'field_mapping', 'field',
                 'name', 'label', 'canonical_name'):
        value = metadata.get(name)
        if isinstance(value, str) and value.strip() and value not in terms:
            terms.append(value.strip())
    return terms

def _exact_choice_keys(target, original, catalog):
    """Return a single key only when the structured label has one exact match."""
    if isinstance(original, dict):
        label = original.get('name') or original.get('field')
    else:
        label = original
    if not isinstance(label, str) or not label.strip():
        return []
    if target == 'dimensions':
        # A typed dimension slot must not compete with entity aliases or
        # attributes. Only fall back when this category has no registered hit.
        dimensions = matching_dimensions(original, catalog)
        if dimensions:
            return dimensions
    if target == 'metrics':
        pool = catalog.get('metrics', {})
    elif target == 'dimensions':
        pool = {**catalog.get('dimensions', {}), **catalog.get('fields', {})}
    elif target == 'display_fields':
        pool = catalog.get('fields', {})
    elif target in {'filters', 'sort'}:
        pool = {**catalog.get('fields', {}), **catalog.get('dimensions', {}),
                **catalog.get('metrics', {})}
    else:
        return []
    if label in pool:
        return [label]
    exact = []
    field_matches = set(matching_fields(original, catalog, aliases=False))
    for key, metadata in pool.items():
        terms = [key, *_metadata_terms(metadata)]
        if (key in field_matches or any(_vector_surface(label) == _vector_surface(term) for term in terms
               if isinstance(term, str))) and key not in exact:
            exact.append(key)
    if exact:
        return _equivalent_slot_keys(exact, catalog)
    field_aliases = set(matching_fields(original, catalog))
    aliases = [key for key, metadata in pool.items()
               if key in field_aliases or normalized(label) in {
                   normalized(term) for term in registered_terms(metadata.get('synonyms'))}]
    if aliases:
        return _equivalent_slot_keys(aliases, catalog)
    # A whole entity alias denotes its published identity dimension (grouping)
    # or main display attribute (projection/sort), not a neighbor's homonym.
    owners = entity_label_candidates(label, catalog['entities'])
    if len(owners) != 1 or target not in {'dimensions', 'display_fields', 'sort'}:
        return []
    owner = next(iter(owners))
    entity = catalog['entities'][owner]
    if target == 'dimensions':
        named = [key for key, metadata in catalog['dimensions'].items()
                 if key == owner or normalized(metadata.get('dim_name')) == normalized(entity.get('entity_name'))]
        if named:
            return named
        # 裸实体维度词兜底（Q60 实测 2026-10-22）：维度名与实体名不同字时，
        # 目录 bind_entities 是唯一可用的归属证据。只认该实体独占发布的
        # 维度（映射实体恰好等于该实体）；多个候选保留歧义并给出真实名单。
        owned = []
        evidence = {}
        for key, metadata in catalog['dimensions'].items():
            resolved = set()
            for mapping in _object(metadata.get('bind_entities')) or []:
                if isinstance(mapping, dict):
                    resolved |= resolve_entities(mapping.get('entity'), catalog['entities'])
            evidence[key] = sorted(resolved)
            if resolved == {owner}:
                owned.append(key)
        if not owned:
            # 诊断：裸实体维度词零命中时转储维度归属证据（q60x 实测 2026-10-22）
            logger.warning('Bare-entity dimension fallback empty: owner=%s label=%r evidence=%s',
                           owner, label, json.dumps(evidence, ensure_ascii=False)[:1500])
        return owned
    return [key for key, metadata in catalog['fields'].items()
            if any(attr.get('is_main_attribute') is True and owner in field_owners(attr)
                   for attr in metadata.get('attribute_bindings') or [metadata])]

def _equivalent_slot_keys(keys, catalog):
    """Coalesce a plain dimension and its own attribute, not genuine homonyms.

    Published physical mapping, owner and attribute identity must agree. A
    hierarchy or derived grouping is not interchangeable with a column.
    """
    result = list(keys)
    for key in keys:
        dimension = catalog['dimensions'].get(key)
        if not dimension or key in catalog['metrics']:
            continue
        if any(dimension.get(name) for name in ('dim_hierarchy', 'special_rules', 'granularity_support')):
            continue
        field = _dim_bound_field(key, catalog)
        if not field or field not in keys:
            continue
        metadata = catalog['fields'][field]
        attributes = metadata.get('attribute_bindings') or [metadata]
        valid = True
        for mapping in _object(dimension.get('bind_entities')) or []:
            if not isinstance(mapping, dict):
                valid = False
                break
            mapped = f"{mapping.get('mappingTable')}.{mapping.get('mappingColumn')}"
            owners = resolve_entities(mapping.get('entity'), catalog['entities'])
            if mapped != field or not owners or not owners & field_owners(metadata):
                valid = False
                break
            if mapping.get('attr') is not None and not any(
                str(attr.get('attribute_id')) == str(mapping['attr'])
                and owners & field_owners(attr) for attr in attributes
            ):
                valid = False
                break
        if valid:
            result.remove(key)
    return result


def _field_is_identifier(field, metadata):
    column = str(field or '').rsplit('.', 1)[-1].casefold()
    attr_code = str((metadata or {}).get('attr_code') or '').casefold()
    return (column in {'id', 'code', 'key'} or attr_code in {'id', 'code', 'key'}
            or column.endswith(('_id', '_code', '_key')) or attr_code.endswith(('_id', '_code', '_key')))


def _name_field_candidates(key, catalog):
    fields = catalog.get('fields', {})
    table, _, column = str(key).partition('.')
    stem = re.sub(r'(_id|_code|_key)$', '', column.casefold())
    owner = (fields.get(key) or {}).get('owner')
    ranked = []
    for field, metadata in fields.items():
        if field == key:
            continue
        candidate_table, _, candidate_column = str(field).partition('.')
        attr_code = str(metadata.get('attr_code') or '').casefold()
        attr_name = str(metadata.get('attr_name') or '').casefold()
        same_stem = re.sub(r'(_name|_label|_title)$', '', candidate_column.casefold()) == stem
        is_name = (candidate_column.casefold() in {'name', 'label', 'title'}
                   or attr_code in {'name', 'label', 'title'}
                   or metadata.get('is_main_attribute') is True
                   or candidate_column.casefold().endswith(('_name', '_label', '_title'))
                   or attr_code.endswith(('_name', '_label', '_title')) or 'name' in attr_name or 'label' in attr_name)
        if is_name and (candidate_table == table or (owner and metadata.get('owner') == owner) or same_stem):
            ranked.append(((0 if same_stem else 1, 0 if candidate_table == table else 1, field), field))
    return [field for _, field in sorted(ranked)]


def _vector_matches(field, surface, catalog):
    if field not in catalog.get('fields', {}):
        return []
    matches = []
    for record in catalog.get('values', []):
        if value_allowed(record, catalog) and record.get('field') == field and (
            _vector_value_equivalent(surface, record.get('value'))
            or _vector_value_equivalent(surface, record.get('label'))
        ):
            if record.get('value') not in matches:
                matches.append(record.get('value'))
    return matches


def _vector_locate_value(values, catalog):
    """字段没绑上时，过滤值在授权字段上唯一命中即视为字段+值可代绑定；多义维持澄清。"""
    selected, corrected = None, []
    for surface in values:
        if not isinstance(surface, str):
            return None
        candidates = [(field, match) for field in catalog.get('fields', {}) for match in _vector_matches(field, surface, catalog)]
        if len(candidates) != 1:
            return None
        field, canonical = candidates[0]
        if selected is not None and selected != field:
            return None
        selected = field
        corrected.append(canonical)
    return (selected, corrected) if selected is not None else None


def _vector_correct_filter(key, values, catalog, operator):
    """Return a unique vector-grounded field/value repair.

    The caller supplies the current candidate scope. After field binding this
    contains only the selected field and its published dictionary fields.
    Before field binding it may contain several unresolved candidates. Every
    literal must resolve to one field; ties remain unresolved.
    """
    if operator not in {'=', '!=', 'IN', 'NOT IN'} or not values:
        return None
    if not any(isinstance(value, str) and value.strip() for value in values):
        return None
    fields_to_try = [key]
    metadata = catalog.get('fields', {}).get(key) or {}
    if _field_is_identifier(key, metadata):
        fields_to_try.extend(_name_field_candidates(key, catalog))
    broad_fields = list(dict.fromkeys(
        [str(field) for field in (catalog.get('fields') or {}) if field]
        + [str(record.get('field') or '') for record in catalog.get('values', [])
           if record.get('field') in catalog.get('fields', {})]
    ))
    fields_to_try = list(dict.fromkeys(fields_to_try + broad_fields))
    selected_field = None
    corrected = []
    for surface in values:
        if not isinstance(surface, str):
            return None
        candidates = []
        for field in fields_to_try:
            matches = _vector_matches(field, surface, catalog)
            if len(matches) == 1:
                candidates.append((field, matches[0]))
        if not candidates:
            return None
        declared = [item for item in candidates if item[0] == key]
        if len(declared) == 1:
            field, canonical = declared[0]
        else:
            by_field = {}
            for field_name, canonical_value in candidates:
                by_field.setdefault(field_name, set()).add(str(canonical_value))
            unique_fields = [
                field_name for field_name, canonical_values in by_field.items()
                if len(canonical_values) == 1
            ]
            if len(unique_fields) != 1:
                return None
            field = unique_fields[0]
            canonical = next(iter(by_field[field]))
        if selected_field is None:
            selected_field = field
        elif selected_field != field:
            return None
        corrected.append(canonical)
    return (selected_field, corrected) if selected_field is not None else None


def _declared_field_locate(declared, vals, catalog):
    """extraction 声明的字段名与值记录属性名对上时，把值修到该字段。

    省份/城市等同值异字段场景靠声明的字段语义消歧，比全字段唯一性检查更贴合上游意图。
    """
    text = str(declared or '').strip()
    if not text or len(text) < 2:
        return None
    fields = set()
    for record in catalog.get('values', []):
        attr_name = str(record.get('attr_name') or '')
        if not attr_name or (text not in attr_name and attr_name not in text):
            continue
        fields.add(record['field'])
    hits = {}
    for field in fields:
        matches = [_vector_matches(field, surface, catalog) for surface in vals]
        # Every input must bind; never execute just the matched part of IN.
        if matches and all(len(match) == 1 for match in matches):
            hits[field] = [match[0] for match in matches]
    if len(hits) != 1:
        return None
    field, values = next(iter(hits.items()))
    return field, values


def _metric_formula(metric_code, catalog):
    meta = catalog['metrics'].get(metric_code) or {}
    rule = _object(meta.get('calculation_rule')) or {}
    formula = str(rule.get('calc_formula') or meta.get('calc_formula') or '')
    return re.sub(r'^\s*[^\W\d]\w*\s*=\s*', '', formula).strip()


def _metric_having_expression(metric_code, original, catalog):
    """Use the complete registered formula, including scalar/date expressions."""
    if not isinstance(original, dict):
        return None
    operator = str(original.get('op') or original.get('operator') or '').strip()
    value = original.get('value')
    vals = value if isinstance(value, list) else [value]
    if (operator not in {'=', '!=', '>', '>=', '<', '<='} or len(vals) != 1
            or isinstance(vals[0], bool) or not isinstance(vals[0], (int, float))):
        return None
    import math
    formula = _metric_formula(metric_code, catalog)
    fields = set(re.findall(r'\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b', formula))
    if not math.isfinite(vals[0]) or not formula:
        return None
    aggregate = re.fullmatch(r'(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(DISTINCT\s+)?([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*\)', formula, re.I)
    if aggregate:
        distinct = 'DISTINCT ' if aggregate[2] else ''
        return f'{aggregate[1].upper()}({distinct}{aggregate[3]}) {operator} {vals[0]}'
    if not fields or not fields <= catalog['fields'].keys():
        return None
    # 行级日期差采用目录完整公式；不能截取复合公式中的第一个聚合。
    date_arg = r'(?:[A-Za-z_]\w*\.[A-Za-z_]\w*|CURRENT_DATE(?:\s*\(\s*\))?|CURDATE\s*\(\s*\)|NOW\s*\(\s*\))'
    if not re.fullmatch(r'(?:DATEDIFF\s*\(\s*' + date_arg + r'\s*,\s*' + date_arg
                       + r'\s*\)|TIMESTAMPDIFF\s*\(\s*(?:DAY|MONTH|YEAR|HOUR|MINUTE|SECOND|WEEK|QUARTER)\s*,\s*'
                       + date_arg + r'\s*,\s*' + date_arg + r'\s*\))', formula, re.I):
        return None
    return f"{formula} {operator} {vals[0]}"


def _temporal_bounds(value):
    """把日期字面量解析成闭区间 [起, 止]；不是日期字面量返回 None。

    支持 2025年 / 2025年7月 / 2025年7月3日 及 2025-07 / 2025-07-03 形式。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = re.fullmatch(r'(\d{4})年\s*(?:(\d{1,2})月\s*(?:(\d{1,2})日\s*)?)?', text)
    if not m:
        m = re.fullmatch(r'(\d{4})-(\d{1,2})(?:-(\d{1,2}))?', text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    try:
        if day:
            start = end = date(year, month, day)
        elif month:
            start = date(year, month, 1)
            end = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
        else:
            start, end = date(year, 1, 1), date(year, 12, 31)
    except ValueError:
        return None
    return start.isoformat(), end.isoformat()


def bind(extraction, knowledge, model, *, today=None, detail_subject_resolver=None,
         row_level_detail=None):
    """Model chooses bindings; deterministic assembly owns shape/operators/literals."""
    ast = dict(version='2.0', intent='query', subject={}, metrics=[], dimensions=[], display_fields=[], filters=[],
               time_context=None, sort=None, limit=None, having=[], ambiguity=[])
    repairs = []
    projection_bindings = []
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
    prompt_catalog = {key: value for key, value in catalog.items() if not key.startswith('_')}
    prompt_catalog['entities'] = {key: select(meta, ('entity_name', 'entity_alias', 'description'))
                                 for key, meta in catalog['entities'].items()}
    prompt_catalog['fields'] = {key: select(meta, ('attribute_id', 'attr_name', 'attr_code', 'description', 'owner', 'owners', 'data_type', 'attribute_bindings'))
                               for key, meta in catalog['fields'].items()}
    for meta in prompt_catalog['fields'].values():
        meta['attribute_bindings'] = [select(attr, ('owner', 'attribute_id', 'attr_code', 'attr_name'))
                                      for attr in meta.get('attribute_bindings') or []]
    prompt_catalog['values'] = [dict(value, owners=sorted(catalog['_value_owners'].get(value['id']) or []))
                                for value in catalog['values']]
    clock = today or datetime.now()
    context = {'catalog': prompt_catalog, 'current_date': clock.isoformat(),
               'structured_extraction': extraction}
    prompt = '''你只做结构化参数到授权目录的绑定，不做自然语言问题提取。structured_extraction 是唯一业务要求。
实体数组表示涉及的表，不是筛选值；指标/维度/展示字段/过滤/排序/时间/限制的角色及数量不得更改。
目录只用于标准化，不允许从目录说明补指标、条件、默认时间或分组。空数组表示没有该要求。
结合参数内 entity、field、输出要求判断字段归属；信息不足就返回 error 和目录候选，不猜业务要求。
每个参数逐项绑定。返回 JSON: {subject:目录实体编码, metrics:[{index:0,key:目录指标编码}], dimensions:[{index:0,key:维度编码或物理字段}], display_fields:[{index:0,key:物理字段}], filters:[{index:0,key:物理字段,value_ids:[目录values序号]}], sort:[{index:0,key:目录指标编码或物理字段}], time:{anchor:目录时间字段}, relationship_required:false}。
每个输入 index 恰好返回一项，无法匹配的项改为 {index:0,error:具体原因,candidates:[标准候选名称]}。
过滤值是数值阈值、日期、编码/型号时不需要 value_ids，原值原样保留。字符串可用 values 中同字段的标准值替换，逐输入值提供一个序号；不能凭空创造值、截断型号或扩大集合。
按结构化条件的字段含义选择名称字段或编号字段，不按值中有无数字/字母猜测：商品名称对应目录名称字段，商品编号对应目录编号字段。名称匹配后保留标准名称，不能为了表连接而改绑编码字段；编号按原值精确筛选，保留前导零，不用相似名称替换。
名称、品牌、分类等中文等值条件必须联合绑定字段和标准值：先找能表达该业务值的catalog.values条目，再使用该条目的field与id，不能仅凭字段标题相似就把原词填到无对应标准值的字段。多级分类尤其要按标准值所在层级绑定；同一“商品品类”可映射产品类别或一级/二级分类，取决于值的目录证据。找不到对应标准值则返回该参数error，不伪装成已验证。
时间只允许选择目录中的时间字段anchor；起止日期、数量、时间类型和分组粒度全部由程序从结构化参数确定，禁止返回或改写这些值。未声明时间范围时time=null，不增加默认范围。季度筛选不代表按季度分组，明细日期列不做时间截断。
没有指标时 dimensions/display_fields 表示明细列；有指标时 dimensions 才是分组，展示字段不能变成额外分组。
实体数组列出本次涉及的业务对象，不是要求用户从中选一个；同时出现多个实体不构成歧义。展示字段的 entity/field 表示要返回的对象，过滤条件表示限定哪些记录。
subject 是执行查询的主表：指标查询依据已选指标的实体绑定；明细查询结合已绑定展示字段、过滤字段及目录关系选择主体，允许使用授权目录中连接这些对象的关联实体，不要求主体必须出现在结构化实体数组中。不能据此增加指标、分组或筛选条件。无法判断时 subject=null 并给 subject_error。
输出要求含明确的共享属性关联范围（如以目标商品适用科室寻找相关渠道），relationship_required=true；不能只看到实体中有科室就判定。不生成 SQL 或其他业务要求。'''
    prompt += ('value_ids必须使用catalog.values条目的显式id，不要自己数数组位置。'
               '维度绑定可带attr（目录中该维度已有的属性ID），不返回granularity；程序依据结构化时间粒度赋值，不能新增时间维度。'
               'time.anchor、filters、display_fields的key都必须用物理字段（表.字段格式）；'
               '目录中的维度编码不能填到这些位置，应换绑到该维度绑定的物理字段。')
    # The model is a catalog selector only; it cannot reinterpret input.
    prompt += (
        '\nSTRICT BINDING CONTRACT: structured_extraction is authoritative. '
        'Return exactly one candidate row for every declared item, preserving '
        'order and cardinality. Never add, remove, reorder, or reinterpret a '
        'metric, dimension, display field, filter, sort, limit, or time grain. '
        'Use only recalled catalog keys/values. Do not invent a time dimension '
        'or default date range. If a slot cannot be selected, return an error '
        'with candidates instead of guessing.'
    )
    prompt += '\n同名属性必须结合参数entity和目录owner/owners区分；同名枚举必须使用该实体属性下的标准值。查询subject不是所有字段的归属。'
    plan = invoke_binding_object(model, [
        {'role': 'system', 'content': prompt},
        {'role': 'user', 'content': json.dumps(context, ensure_ascii=False, default=str)},
    ], stage='catalog_binding')
    subject = plan.get('subject')
    # Accept the ordinary ASL object shape as well as the compact binding key.
    if isinstance(subject, dict): subject = subject.get('entity')
    if isinstance(subject, str): subject = subject.strip()
    if isinstance(subject,str) and subject in catalog['entities']: ast['subject'] = {'entity': subject}
    knowledge['_structured_filter_origins'] = []
    def append_filter(predicate):
        ast['filters'].append(predicate)
        knowledge['_structured_filter_origins'].append(index)

    def locate_filter_slot(original, scope_catalog):
        """声明字段名 + 值唯一命中时返回 (field, values)，多义返回 None。"""
        if not isinstance(original, dict):
            return None
        operator = str(original.get('op') or original.get('operator') or '').upper()
        if operator not in {'=', '!=', 'IN', 'NOT IN'}:
            return None
        raw_vals = original.get('value')
        raw_vals = raw_vals if isinstance(raw_vals, list) else [raw_vals]
        return (_declared_field_locate(original.get('field'), raw_vals, scope_catalog)
                or _vector_locate_value(raw_vals, scope_catalog))
    for source, target in SECTIONS.items():
        rows = plan.get(target) or []
        if not isinstance(rows, list): rows = []
        for index, original in enumerate(extraction[source]):
            slot_catalog = parameter_catalog(catalog, original, extraction, target)
            owner_candidates = slot_catalog.get('_parameter_owner_candidates')
            owner_error = slot_catalog.get('_parameter_owner_error')
            if owner_candidates or owner_error:
                # 2026-10-23 用户要求：结构化过滤参数作为硬约束优先确定性绑定。
                # 字段名+值在授权目录里唯一命中时直接落 ASL，不再因实体归属
                # 追问，避免同一问题反复重新生成 ASL；多义仍保持澄清。
                located = (locate_filter_slot(original, catalog)
                           if target == 'filters' else None)
                if located:
                    located_field, fixed_vals = located
                    operator = str(original.get('op') or original.get('operator') or '').upper()
                    if len(fixed_vals) > 1 and operator in {'=', '!='}:
                        operator = 'IN' if operator == '=' else 'NOT IN'
                    append_filter({'field': located_field, 'operator': operator,
                                   'value': fixed_vals if operator in {'IN', 'NOT IN', 'BETWEEN'}
                                   else fixed_vals[0]})
                    repairs.append({'type': 'VECTOR_FILTER_VALUE_CORRECTED',
                                    'source': 'VECTOR_CATALOG_HARD_CONSTRAINT',
                                    'original_field': original.get('field'),
                                    'field': located_field,
                                    'values': fixed_vals})
                    knowledge.setdefault('_hard_constraint_filter_indices', set()).add(
                        len(ast['filters']) - 1)
                    continue
                ast['ambiguity'].append(issue(f'{source}[{index+1}]', original,
                    owner_error or '该名称或别名对应多个实体，尚未明确参数归属', owner_candidates or []))
                continue
            label = (original.get('name') or original.get('field')) if isinstance(original,dict) else original
            if not isinstance(label,str) or not label.strip():
                ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,'上游未明确该参数的业务字段或指标名称'))
                continue
            found = [r for r in rows if isinstance(r, dict) and type(r.get('index')) is int and r['index'] == index]
            choice = found[0] if len(found) == 1 else {}
            key = choice.get('key')
            exact_keys = _exact_choice_keys(target, original, slot_catalog)
            if target in {'dimensions', 'sort'} and not exact_keys and _grain_slot_unit(label):
                # Month/quarter/year denote a grain of the governed date field,
                # not a separate database attribute. Use the same key in SELECT
                # and ORDER BY; keep the existing unresolved path if no anchor.
                try:
                    anchor = time_anchor(extraction.get('时间粒度') or {}, ast,
                                         catalog, knowledge, plan.get('time') or {})
                except (ValueError, TypeError, KeyError):
                    anchor = None
                if anchor:
                    selected = [dim['name'] for dim in ast['dimensions']
                                if dim['name'] == anchor or anchor in dimension_fields(
                                    catalog['dimensions'].get(dim['name'], {}))]
                    exact_keys = [selected[0] if len(selected) == 1 else anchor]
            if target == 'filters' and all(
                value in slot_catalog['fields'] or value in catalog['dimensions']
                for value in exact_keys
            ):
                # An exact attribute is more specific than a homonymous
                # dimension. Keep multiple attributes ambiguous; do not
                # reinterpret metric thresholds as physical-field filters.
                physical_exact = [value for value in exact_keys if value in slot_catalog['fields']]
                if physical_exact:
                    exact_keys = physical_exact
            if target == 'filters' and len(exact_keys) == 1 and exact_keys[0] in catalog['dimensions']:
                resolved = _dimension_filter_field(exact_keys[0], original, slot_catalog)
                if resolved:
                    exact_keys = [resolved]
            if len(exact_keys) == 1:
                if key != exact_keys[0] or choice.get('error'):
                    repairs.append({'type': 'STRUCTURED_SLOT_KEY_CORRECTED',
                                'source': 'STRUCTURED_EXTRACTION',
                                'slot': f'{source}[{index+1}]',
                                'original_key': key, 'key': exact_keys[0]})
                key = exact_keys[0]
                choice = dict(choice, key=key)
                choice.pop('error', None)
            elif len(exact_keys) > 1:
                # Do not let a model pick one homonym by vector rank. Filters
                # may still be disambiguated by a unique value below.
                choice = dict(choice, error='同名名称或别名对应多个目录项，当前参数未能唯一确定含义', candidates=exact_keys)
            required_owners = slot_catalog.get('_parameter_owners') or set()
            if target in {'dimensions', 'sort'} and isinstance(key, str) and key in catalog['dimensions'] and required_owners:
                definition = catalog['dimensions'][key]
                bindings = [item for item in _object(definition.get('bind_entities')) or [] if isinstance(item, dict)
                            and (resolve_entities(item.get('entity'), catalog['entities']) & required_owners
                                 or f"{item.get('mappingTable')}.{item.get('mappingColumn')}" in slot_catalog['fields'])]
                if definition.get('bind_entities') and not bindings:
                    choice = dict(choice, error='该维度的已发布映射不属于参数指定的实体')
                if target == 'sort':
                    resolved = _dim_bound_field(key, slot_catalog)
                    if resolved:
                        key = resolved
                    elif key not in required_owners:
                        choice = dict(choice, error='该排序维度无法唯一对应到指定实体的字段')
                elif len(bindings) == 1 and bindings[0].get('attr') is not None:
                    choice = dict(choice, attr=bindings[0]['attr'])
                elif key not in required_owners and (not bindings or choice.get('attr') not in {b.get('attr') for b in bindings}):
                    choice = dict(choice, error='该维度未唯一绑定到参数指定的实体属性')
            elif target == 'dimensions' and isinstance(key, str) and key in catalog['dimensions'] and len(exact_keys) == 1:
                bindings = _object(catalog['dimensions'][key].get('bind_entities')) or []
                if len(bindings) == 1 and isinstance(bindings[0], dict) and bindings[0].get('attr') is not None:
                    # The selected dimension's published mapping owns its
                    # attribute; a model's discarded entity match does not.
                    choice = dict(choice, attr=bindings[0]['attr'])
            # 过滤/展示只认物理字段；模型绑了维度编码时先换绑到该维度的物理字段
            if target in {'filters', 'display_fields'} and isinstance(key, str) and key not in catalog['fields']:
                resolved = _dim_bound_field(key, slot_catalog)
                if resolved:
                    key = resolved
            # 已选指标的阈值按完整目录公式处理，不能把日期差等行级指标误判成不支持。
            if target == 'filters' and isinstance(key, str) and key in catalog['metrics']:
                having_expr = _metric_having_expression(key, original, catalog) if extraction['指标'] else None
                if having_expr:
                    ast['having'].append(having_expr)
                    continue
                ast['ambiguity'].append(issue(f'{source}[{index+1}]', original,
                    '该指标的筛选公式或数值阈值未能绑定到当前授权目录，请核对指标定义和条件值'))
                continue
            allowed = (catalog['metrics'] if target == 'metrics' else
                       {**catalog['dimensions'], **slot_catalog['fields']} if target == 'dimensions' else
                       {**catalog['metrics'], **catalog['dimensions'], **slot_catalog['fields']} if target == 'sort' else slot_catalog['fields'])
            if target == 'filters' and isinstance(original, dict):
                # 依赖任务的筛选值常被规划器写成占位引用，抽取阶段落成空列表。
                # 空列表无法生成有效 SQL，剥离该条件继续执行；
                # 执行期依赖接管若约束已编译仍会注入真实名单。
                empty_op = str(original.get('op') or original.get('operator') or '').upper()
                if empty_op in {'IN', 'NOT IN'} and original.get('value') == []:
                    repairs.append({'type': 'EMPTY_FILTER_VALUE_DROPPED',
                                    'source': 'STRUCTURED_EXTRACTION',
                                    'field': original.get('field') or label,
                                    'operator': empty_op})
                    continue
            if choice.get('error') or not isinstance(key,str) or key not in allowed:
                if target == 'filters' and isinstance(original, dict):
                    # 模型绑不出字段时，过滤值若在授权字段上唯一命中则字段+值一起修正
                    op_ = str(original.get('op') or original.get('operator') or '').upper()
                    raw_vals = original.get('value')
                    raw_vals = raw_vals if isinstance(raw_vals, list) else [raw_vals]
                    fallback_key = exact_keys[0] if len(exact_keys) == 1 else None
                    located = ((_declared_field_locate(original.get('field'), raw_vals, slot_catalog)
                                or _vector_correct_filter(fallback_key, raw_vals, slot_catalog, op_)
                                or _vector_locate_value(raw_vals, slot_catalog))
                               if op_ in {'=','!=','IN','NOT IN'} else None)
                    if located is None and op_ in {'=', '!=', 'IN', 'NOT IN'}:
                        # 声明实体与字段归属不一致时（例如维度字段“城市名称”被
                        # 挂在“销售订单”实体下），槽位目录里没有该字段；改在
                        # 完整授权目录里按声明字段名+值唯一命中绑定，不再为同一
                        # 问题整轮重新生成 ASL。多义仍保持澄清。
                        located = (_declared_field_locate(
                                       original.get('field'), raw_vals, catalog)
                                   or _vector_locate_value(raw_vals, catalog))
                    if located:
                        located_field, fixed_vals = located
                        operator2 = op_
                        if len(fixed_vals) > 1 and op_ in {'=', '!='}:
                            operator2 = 'IN' if op_ == '=' else 'NOT IN'
                        append_filter({'field': located_field, 'operator': operator2,
                                               'value': fixed_vals if operator2 in {'IN','NOT IN','BETWEEN'} else fixed_vals[0]})
                        repairs.append({'type': 'VECTOR_FILTER_VALUE_CORRECTED', 'source': 'VECTOR_CATALOG_FIELD_FALLBACK',
                                        'original_field': original.get('field'),
                                        'field': located_field, 'original_values': raw_vals, 'values': fixed_vals})
                        knowledge.setdefault('_hard_constraint_filter_indices', set()).add(
                            len(ast['filters']) - 1)
                        continue
                if target == 'dimensions':
                    # 维度槽位是纯时间粒度词（如「季度」）时不拦停澄清：
                    # 时间分组粒度由程序控制，把词形并入已绑定时间维度的
                    # granularity（或待后续时间维度绑定时经 时间粒度.unit 生效）。
                    grain = _grain_slot_unit(label)
                    temporal = extraction.get('时间粒度')
                    declared = ''
                    if isinstance(temporal, dict) and temporal.get('unit'):
                        declared = normalize_unit(temporal['unit']) or ''
                    if grain and (not declared or declared == grain):
                        applied = None
                        if ast['metrics']:
                            for dim in ast['dimensions']:
                                if not isinstance(dim, dict) or dim.get('granularity'):
                                    continue
                                meta = (catalog['dimensions'].get(dim.get('name'))
                                        or catalog['fields'].get(dim.get('name')) or {})
                                if is_temporal(meta):
                                    dim['granularity'] = grain
                                    applied = dim.get('name')
                                    break
                        if isinstance(temporal, dict) and not temporal.get('unit'):
                            temporal['unit'] = grain
                        repairs.append({'type': 'STRUCTURED_GRAIN_SLOT_AS_GRANULARITY',
                                        'source': 'STRUCTURED_EXTRACTION', 'field': label,
                                        'granularity': grain, 'applied_dimension': applied})
                        continue
                reason = choice.get('error') or '未匹配到可执行的标准字段'
                ownership_conflict = bool(len(exact_keys) > 1 or (required_owners and isinstance(key, str) and key in catalog['fields'] and key not in slot_catalog['fields']))
                if target == 'display_fields' and not extraction['指标'] and not ownership_conflict and any(isinstance(r.get('key'),str) and r.get('key') in catalog['fields'] for r in rows if isinstance(r, dict)):
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
            # 展示位出现指标编码时按指标落位，避免聚合场景在字段白名单处被拒
            if target == 'display_fields' and extraction['指标'] and isinstance(key, str) and key in catalog['metrics']:
                if key not in {m['name'] for m in ast['metrics']}:
                    ast['metrics'].append({'name': key, 'alias': catalog['metrics'][key].get('metric_name') or key,
                                           'time_anchor': None})
                continue
            if target == 'metrics':
                ast['metrics'].append({'name': key, 'alias': allowed[key].get('metric_name') or key, 'time_anchor': None})
            elif target in {'dimensions', 'display_fields'}:
                if target == 'display_fields' and key in catalog['fields']:
                    # Preserve the already resolved slot identity even when a
                    # grouping projects this same field under another label.
                    projection_bindings.append({'type': 'STRUCTURED_PROJECTION_BINDING',
                                    'source': 'STRUCTURED_EXTRACTION',
                                    'entity': original.get('entity', '') if isinstance(original, dict) else '',
                                    'field': (original.get('field') or original.get('name')) if isinstance(original, dict) else original,
                                    'resolved_field': key})
                if target == 'display_fields':
                    ast['display_fields'].append({'name': key, 'alias': allowed[key].get('attr_name') or key})
                elif key not in {d['name'] for d in ast['dimensions']}:
                    temporal = extraction.get('时间粒度') or {}
                    try:
                        granularity = grouping_grain(temporal, original, key, target, ast, catalog)
                    except ValueError as exc:
                        ast['ambiguity'].append(issue(source, original, str(exc)))
                        continue
                    if choice.get('granularity') != granularity:
                        repairs.append({'type': 'STRUCTURED_TIME_GRANULARITY_CORRECTED',
                                        'source': 'STRUCTURED_EXTRACTION', 'field': key,
                                        'previous_granularity': choice.get('granularity'),
                                        'granularity': granularity})
                    ast['dimensions'].append(dict(name=key, attr=choice.get('attr') if target=='dimensions' and '.' not in key else None,
                                                  level=None, granularity=granularity))
            elif target == 'filters':
                # Field selection is complete. Every value-binding path below
                # uses that field and its published dictionary, never siblings
                # left over from the broader pre-binding candidate pool.
                slot_catalog = bound_filter_catalog(slot_catalog, key)
                if not isinstance(original, dict):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '条件必须包含 field/op/value'));continue
                operator = str(original.get('op') or original.get('operator') or '').upper()
                value = original.get('value'); vals = value if isinstance(value, list) else [value]
                if operator not in OPERATORS or not vals or any(v is None or isinstance(v, (dict,list,bool)) or v == '' for v in vals):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '运算符或条件值缺失/不支持'));continue
                corrected = _vector_correct_filter(key, vals, slot_catalog, operator)
                vector_repaired = False
                if corrected is not None:
                    corrected_key, corrected_vals = corrected
                    if corrected_key != key or corrected_vals != vals:
                        vector_repaired = True
                        repairs.append({
                            'type': 'VECTOR_FILTER_VALUE_CORRECTED',
                            'source': 'VECTOR_CATALOG',
                            'original_field': key,
                            'field': corrected_key,
                            'original_values': list(vals),
                            'values': list(corrected_vals),
                        })
                    key, vals = corrected_key, corrected_vals
                ids = [] if vector_repaired else (choice.get('value_ids') or [])
                # A model-provided value_id can point at a nearby value from
                # the wrong field. Discard that hint for explicit codes/model
                # literals; any further lookup stays in the bound field scope.
                if ids and isinstance(ids, list) and len(ids) == len(vals) \
                        and all(type(i) is int and 0 <= i < len(catalog['values']) for i in ids) \
                        and all(isinstance(v, str) for v in vals):
                    provisional = [catalog['values'][i]['value'] for i in ids]
                    if any(
                        catalog['values'][i].get('field') != key
                        or not value_allowed(catalog['values'][i], slot_catalog)
                        or (corrected is not None and bound != value)
                        or (
                            re.search(r'[0-9A-Za-z]', value)
                            and value != str(bound)
                        )
                        for i, (value, bound) in zip(ids, zip(vals, provisional))
                    ):
                        repairs.append({
                            'type': 'VECTOR_VALUE_ID_DISCARDED',
                            'source': 'VECTOR_CATALOG',
                            'field': key,
                            'values': list(vals),
                            'bound_values': list(provisional),
                            'reason': 'VALUE_ID_FIELD_OR_LITERAL_MISMATCH',
                        })
                        ids = []
                if ids:
                    valid_ids = (
                        isinstance(ids, list)
                        and len(ids) == len(vals)
                        and all(type(i) is int and 0 <= i < len(catalog['values']) for i in ids)
                        and all(catalog['values'][i]['field'] == key for i in ids)
                        and all(value_allowed(catalog['values'][i], slot_catalog) for i in ids)
                        and all(isinstance(v, str) for v in vals)
                    )
                    if not valid_ids:
                        repaired = _vector_correct_filter(key, vals, slot_catalog, operator)
                        if repaired is not None:
                            repaired_key, repaired_vals = repaired
                            if repaired_key != key or repaired_vals != vals:
                                repairs.append({
                                    'type': 'VECTOR_FILTER_VALUE_CORRECTED',
                                    'source': 'VECTOR_CATALOG_FALLBACK',
                                    'original_field': key,
                                    'field': repaired_key,
                                    'original_values': list(vals),
                                    'values': list(repaired_vals),
                                })
                            key, vals = repaired_key, repaired_vals
                            # 字段和值都已修正为目录标准形态，直接落条件，不再过 ids 数量检查
                            operator2 = operator
                            if len(vals) > 1 and operator in {'=', '!='}:
                                operator2 = 'IN' if operator == '=' else 'NOT IN'
                            append_filter({'field': key, 'operator': operator2,
                                                   'value': vals if operator2 in {'IN', 'NOT IN', 'BETWEEN'} else vals[0]})
                            continue
                        elif operator in {'=', '!=', 'IN', 'NOT IN'} and any(
                            isinstance(v, str) and re.search(r'[\u4e00-\u9fff]', v)
                            for v in vals
                        ):
                            ast['ambiguity'].append(issue(
                                f'{source}[{index+1}]', original,
                                '\u591a\u503c\u6761\u4ef6\u4e2d\u6709\u503c\u672a\u80fd\u7ed1\u5b9a\u5230\u6807\u51c6\u503c\uff0c\u8bf7\u6838\u5bf9\u8be5\u503c\u7684\u8bed\u4e49\u914d\u7f6e'))
                            continue
                        ids = []
                    if (not isinstance(ids,list) or len(ids)!=len(vals) or any(type(i) is not int or not 0<=i<len(catalog['values']) for i in ids)
                            or any(catalog['values'][i]['field']!=key for i in ids)
                            or any(not value_allowed(catalog['values'][i], slot_catalog) for i in ids)
                            or any(not isinstance(v,str) for v in vals)):
                        ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '标准值绑定与字段或输入值数量不一致'));continue
                    bound_vals=[catalog['values'][i]['value'] for i in ids]
                    # A model may select semantic names only from recalled
                    # same-field values, one per input. Exact matches above
                    # win; codes/models remain literal below.
                    if bound_vals != vals:
                        repairs.append({'type': 'VECTOR_FILTER_VALUE_CORRECTED',
                                        'source': 'SCOPED_CATALOG_VALUE_SELECTION',
                                        'field': key, 'original_values': list(vals),
                                        'values': list(bound_vals)})
                    if ids:
                        # 编码/型号按字面量透传，模型绑到别的值时保留输入原值
                        literal_codes = all(
                            isinstance(v, str) and re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z\-/\.]*', v or '')
                            and re.search(r'\d', v) and re.search(r'[A-Za-z]', v)
                            for v in vals
                        )
                        if literal_codes and any(v != str(b) for v, b in zip(vals, bound_vals)):
                            if len(vals)>1 and operator in {'=','!='}:
                                operator = 'IN' if operator == '=' else 'NOT IN'
                            append_filter({'field': key, 'operator': operator,
                                                   'value': vals if operator in {'IN','NOT IN','BETWEEN'} else vals[0]})
                            continue
                        if any(re.search(r'[0-9A-Za-z]',v) and v!=str(b) for v,b in zip(vals,bound_vals)):
                            ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,'编码、型号或数字字符串不得被替换成其他值'));continue
                        vals=bound_vals
                    else:
                        # 修正后的值已确认在标准值目录内，直接落过滤条件
                        if len(vals)>1 and operator in {'=','!='}: operator='IN' if operator=='=' else 'NOT IN'
                        append_filter({'field':key,'operator':operator,'value':vals if operator in {'IN','NOT IN','BETWEEN'} else vals[0]})
                        continue
                elif operator == 'BETWEEN' and len(vals) == 2 and all(_temporal_bounds(v) for v in vals):
                    # 日期区间字面量确定性换算，不走标准值目录
                    first, last = _temporal_bounds(vals[0]), _temporal_bounds(vals[1])
                    append_filter({'field': key, 'operator': 'BETWEEN', 'value': [first[0], last[1]]})
                    continue
                elif operator in {'=', '>=', '>', '<=', '<'} and len(vals) == 1 and _temporal_bounds(vals[0]):
                    # 单个日期字面量：等值换算成区间，比较运算取边界
                    start, end = _temporal_bounds(vals[0])
                    if operator == '=':
                        append_filter({'field': key, 'operator': 'BETWEEN', 'value': [start, end]})
                    else:
                        append_filter({'field': key, 'operator': operator,
                                               'value': start if operator in {'>=', '>'} else end})
                    continue
                elif operator in {'=','!=','IN','NOT IN'} and any(
                    isinstance(v,str) and re.search(r'[\u4e00-\u9fff]',v) for v in vals
                ) and not all(any(value_allowed(c, slot_catalog) and c['field']==key and c['value']==v for c in catalog['values']) for v in vals):
                    # 模型没给 ids 时，仅在已绑定字段及其登记字典内找标准值。
                    located = (_vector_correct_filter(key, vals, slot_catalog, operator)
                               or _declared_field_locate(original.get('field') if isinstance(original, dict) else None, vals, slot_catalog)
                               or _vector_locate_value(vals, slot_catalog))
                    if located:
                        located_field, fixed_vals = located
                        operator2 = operator
                        if len(fixed_vals) > 1 and operator in {'=', '!='}:
                            operator2 = 'IN' if operator == '=' else 'NOT IN'
                        append_filter({'field': located_field, 'operator': operator2,
                                               'value': fixed_vals if operator2 in {'IN','NOT IN','BETWEEN'} else fixed_vals[0]})
                        repairs.append({'type': 'VECTOR_FILTER_VALUE_CORRECTED', 'source': 'VECTOR_CATALOG_FIELD_FALLBACK',
                                        'original_field': key, 'field': located_field,
                                        'original_values': list(vals), 'values': fixed_vals})
                        continue
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]',original,
                        f'字段 {key} 已匹配，但条件值尚未匹配到该字段的向量标准值，请核对字段层级及值目录'))
                    continue
                if len(vals)>1 and operator in {'=','!='}: operator='IN' if operator=='=' else 'NOT IN'
                if (operator=='BETWEEN' and len(vals)!=2) or (len(vals)>1 and operator not in {'IN','NOT IN','BETWEEN'}):
                    ast['ambiguity'].append(issue(f'{source}[{index+1}]', original, '该运算符与多个条件值不兼容'));continue
                append_filter({'field':key,'operator':operator,'value':vals if operator in {'IN','NOT IN','BETWEEN'} else vals[0]})
            elif target == 'sort':
                direction = str(original.get('order') or original.get('direction') or '').upper() if isinstance(original,dict) else ''
                if direction not in {'ASC','DESC'}:
                    ast['ambiguity'].append(issue(source, original, '当前ASL仅支持一个明确升序/降序的排序字段'));continue
                if ast.get('sort'):
                    # ASL 的 sort 是单一字段；多级排序保留用户声明的主排序，
                    # 次级排序不阻断执行（同分时由结果自然顺序呈现）。
                    repairs.append({'type':'SECONDARY_SORT_IGNORED','source':'STRUCTURED_EXTRACTION',
                                    'field':key,'direction':direction})
                    continue
                if key in catalog['metrics'] and key not in {m['name'] for m in ast['metrics']}:
                    # 按指标排序时该指标必须同时进 SELECT，否则 ORDER BY 会引用
                    # 一个未投影的指标编码，SQL 与发布公式对不上（METRIC_FORMULA_STALE）
                    ast['metrics'].append({'name': key,
                                           'alias': catalog['metrics'][key].get('metric_name') or key,
                                           'time_anchor': None})
                    repairs.append({'type': 'METRIC_FROM_SORT_SLOT',
                                    'source': 'STRUCTURED_EXTRACTION', 'metric': key})
                ast['sort']={'field':key,'direction':direction,'field_type':'metric' if key in catalog['metrics'] else 'field' if '.' in key else 'dimension'}
        if extraction[source] and any(not isinstance(r,dict) or type(r.get('index')) is not int or not 0<=r['index']<len(extraction[source]) for r in rows):
            ast['ambiguity'].append(issue(source, extraction[source], '绑定输出添加了结构化提取中不存在的参数'))
        # 空数组表示没有该要求，模型多绑的行直接忽略，不作为澄清事项抛给用户
        if not extraction[source] and rows:
            repairs.append({'type': 'IGNORED_UNDECLARED_BINDINGS',
                            'source': 'STRUCTURED_EXTRACTION', 'slot': source})
    limit=extraction.get('限制')
    if ast['metrics']:
        metric_subjects = _metric_subject_candidates([m['name'] for m in ast['metrics']], catalog)
        current = (ast.get('subject') or {}).get('entity')
        if metric_subjects:
            selected = current if current in metric_subjects else metric_subjects[0]
            if current != selected:
                repairs.append({'type': 'METRIC_SUBJECT_FROM_SOURCE_DEPENDENCY',
                                'source': 'METRIC_SOURCE_DEPENDENCY',
                                'previous_subject': current, 'entity': selected,
                                'candidates': metric_subjects})
            ast['subject'] = {'entity': selected}
    if limit is not None and (type(limit) is not int or not 1<=limit<=10000):
        ast['ambiguity'].append(issue('限制',limit,'需要1到10000的整数'))
    else:ast['limit']=limit
    temporal=extraction.get('时间粒度') or {}
    try:
        ast['time_context'] = compile_time(temporal, ast, catalog, knowledge, plan.get('time'), clock)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        ast['ambiguity'].append(issue('时间粒度', temporal, str(exc)))
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
    if not ast['ambiguity']:
        repairs.extend(projection_bindings)
    repairs.append({'type':'STRUCTURED_BINDING_ONLY','source':'STRUCTURED_EXTRACTION',
                    'relationship_required':plan.get('relationship_required') is True})
    return ast, repairs
