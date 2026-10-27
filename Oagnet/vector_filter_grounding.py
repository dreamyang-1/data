"""The executable ASL boundary requires scoped vector-standard business values."""
import re

from asl_contract import ASLValidationError
from scope_contract import require_candidate_scope, scope_filter
from structured_binding import (
    _field_is_identifier, _metadata, _temporal_bounds, _vector_matches,
    catalog_candidates,
)
from structured_time import is_temporal


# Month-style physical columns (fiscal_month, 会计月份) are temporal even when
# the published metadata carries no date/time token; their strict date bounds
# are literal thresholds, never vector-grounded business terms.
_MONTH_FIELD_RE = re.compile(r'月|month', re.I)

# ASL 生成 LLM 偶发把任务问题的叙述片段幻觉成等值过滤值（Q60 实测：商品名称
# EQ "2025年销售额占比前3的产品线下各"，规划期提取的过滤条件为空、值在执行
# 期生成）。真实业务值（商品/经销商/医院/省份名等）不会同时携带时间前缀、
# 排名短语、指标描述、群扫指代等多重叙述标记；零候选 + 命中多重特征的条件
# 在绑定闸口剥离并留 repair 记录，不猜标准值。真实业务值（零候选但无散文
# 特征）仍保持 fail-closed，由澄清流程兜底。
_PROSE_VALUE_MARKERS = (
    re.compile(r'\d{4}\s*年'),
    re.compile(r'前\d+'),
    re.compile(r'占比|排名|销量|销售额|金额|总数|数量|总计|合计|平均|最高|最低'),
    re.compile(r'(?:各|每)(?:条|个|笔|项|类|产品|商品|订单|经销商|医院|省份|客户|业务)'),
    re.compile(r'上述|前序|依赖任务|任务\d'),
    re.compile(r'[的与和及或下各]$'),
)


def _is_prose_placeholder(value):
    text = str(value).strip()
    if len(text) < 6:
        return False
    return sum(bool(pattern.search(text)) for pattern in _PROSE_VALUE_MARKERS) >= 2


def _date_bound_field(field, metadata):
    return is_temporal(metadata) or bool(_MONTH_FIELD_RE.search(field))


def predicates(ast):
    yield from ast.get('filters') or []
    for related in ast.get('related_filters') or []:
        yield from related.get('target_filters') or []


def business_values(item, catalog):
    """Numbers, dates and explicit identifiers retain their declared literal type."""
    field = str(item.get('field') or '')
    metadata = catalog['fields'].get(field) or {}
    operator = str(item.get('operator') or '').upper()
    if operator not in {'=', '!=', 'IN', 'NOT IN', 'LIKE', '>', '>=', '<', '<=', 'BETWEEN'}:
        return []
    value = item.get('value')
    values = value if isinstance(value, list) else [value]
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        if _temporal_bounds(value) and _date_bound_field(field, metadata):
            continue
        numeric_type = str(metadata.get('data_type') or '').upper()
        semantic_type = str(metadata.get('semantic_type') or '')
        numeric_literal = re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value)
        if numeric_literal and (
                any(kind in numeric_type for kind in ('INT', 'DECIMAL', 'NUMERIC', 'FLOAT', 'DOUBLE', 'NUMBER'))
                or '数值' in semantic_type
                # 阈值比较字面量：量纲连续值没有向量标准值（数值列不发布
                # entity_attribute_value 向量），类型元数据缺失（如 130 目录
                # sales_order.quantity data_type=null）时也按字面量直通，
                # 由 SQL 执行与 translator 物理类型适配兜底；等号枚举/编码
                # 值不受影响，仍必须唯一匹配本字段的向量标准值。
                or operator in {'>', '>=', '<', '<=', 'BETWEEN'}):
            continue
        if (_field_is_identifier(field, metadata)
                and re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z\-/.]*', value)):
            continue
        # LIKE keeps its explicit leading/trailing wildcard semantics, while
        # the business term inside the pattern must use the same standard value.
        surface = value.strip('%') if operator == 'LIKE' else value
        result.append((value, surface))
    return result


def matches(field, surface, catalog):
    candidates = _vector_matches(field, surface, catalog)
    exact = [value for value in candidates if value == surface]
    return exact or candidates


def ground_filter_values(ast, knowledge, *, store=None, embed=None,
                         semantic_model_id=None, domain_ids=None, validate_only=False):
    """Bind each string to its own field; missing/ambiguous evidence fails closed.

    Similarity retrieves candidates only. It never proves that two business
    values are equivalent. The existing finite binder owns semantic synonyms.
    """
    catalog = catalog_candidates(knowledge)
    loaded = set()
    repairs = []
    dropped_items = []
    for item in predicates(ast):
        field = str(item.get('field') or '')
        for original, surface in business_values(item, catalog):
            choices = matches(field, surface, catalog)
            if not choices and not validate_only and store is not None and embed is not None:
                key = (field, surface)
                if key not in loaded and field in catalog['fields']:
                    loaded.add(key)
                    table, _, column = field.partition('.')
                    # Published value vectors support both historical storage
                    # forms: source_field=table.column and table + bare column.
                    where = {'$and': [scope_filter(semantic_model_id,
                        business_domain_ids=domain_ids or [], record_type='entity_attribute_value'),
                        {'$or': [{'source_field': field}, {'$and': [
                            {'source_table': table}, {'source_field': column}]}]}]}
                    recalled = list(store.search(embed(surface), top_k=20, where=where))
                    for record in recalled:
                        metadata = _metadata(record)
                        require_candidate_scope(metadata, semantic_model_id, domain_ids or [])
                        source = str(metadata.get('source_field') or '')
                        qualified = source if '.' in source else str(metadata.get('source_table') or '')+'.'+source
                        if qualified != field:
                            raise ValueError('Vector filter value field does not match requested field')
                    knowledge.setdefault('entity_attribute_values', []).extend(recalled)
                    catalog = catalog_candidates(knowledge)
                    choices = matches(field, surface, catalog)
            if len(choices) != 1 or (validate_only and choices[0] != surface):
                if (not choices and not validate_only
                        and field in catalog['fields']
                        and _is_prose_placeholder(surface)):
                    old = item.get('value')
                    if isinstance(old, list):
                        item['value'] = [value for value in old if value != original]
                        if not item['value']:
                            dropped_items.append(item)
                    else:
                        dropped_items.append(item)
                    repairs.append({'type': 'PROSE_PLACEHOLDER_FILTER_DROPPED',
                        'source': 'VECTOR_CATALOG', 'resolved_field': field,
                        'mention': original})
                    if not isinstance(old, list):
                        break
                    continue
                raise ASLValidationError('ASL_FILTER_VALUE_UNGROUNDED',
                    '筛选值未唯一匹配到该字段的向量标准值，不能标记 ASL 校验通过',
                    field=field, details={'validation_stage': 'vector_value_grounding',
                        'failed_filter': {key: item[key] for key in ('field', 'operator', 'value') if key in item},
                        'candidate_values': choices})
            canonical = choices[0]
            if original != surface:
                canonical = original[:len(original)-len(original.lstrip('%'))] + str(canonical) + original[len(original.rstrip('%')):]
            if canonical != original:
                old = item['value']
                item['value'] = [canonical if value == original else value for value in old] if isinstance(old, list) else canonical
                # Use the existing receipt understood by caller contract and
                # SQL alignment checks. The caller keeps the user's wording.
                repairs.append({'type': 'ADD_SOURCE_RESOLVED_ENTITY_FILTER', 'source': 'VECTOR_CATALOG',
                    'resolved_field': field, 'mention': original, 'canonical_value': canonical})
                knowledge.setdefault('_source_canonical_values', {})[
                    re.sub(r'\s+', '', original).casefold()] = canonical
    if dropped_items:
        # 幻觉条件按身份从容器移除：剥离后 ASL 继续走原有翻译与执行链路，
        # 校验复检（validate_only）看到的已是干净 AST。
        dropped_ids = {id(item) for item in dropped_items}
        if isinstance(ast.get('filters'), list):
            ast['filters'] = [f for f in ast['filters'] if id(f) not in dropped_ids]
        for related in ast.get('related_filters') or []:
            targets = related.get('target_filters')
            if isinstance(targets, list):
                related['target_filters'] = [
                    f for f in targets if id(f) not in dropped_ids]
    return repairs
