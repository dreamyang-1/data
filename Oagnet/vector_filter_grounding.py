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
    return repairs
