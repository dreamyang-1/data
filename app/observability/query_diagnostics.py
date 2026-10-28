"""Request-scoped query shape diagnostics without SQL literals or result rows."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from exception_explainer.diagnostics import redact

logger = logging.getLogger(__name__)
_FIELDS = ('name', 'field', 'entity', 'attr', 'level', 'granularity',
           'operator', 'op', 'order', 'direction', 'stage', 'field_type', 'alias')


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    # Names are catalog/slot labels, never arbitrary text or serialized payloads.
    if not re.fullmatch(r'[\w\u4e00-\u9fff.@: <>=!\[\]/-]{1,160}', value):
        return '[非标准名称]'
    return redact(value, 160)


def _value(value: Any) -> dict[str, Any]:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {'type': type(value).__name__, 'present': value is not None and value != '' and value != [],
            'fingerprint': hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}


def _items(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:50]:
        if isinstance(item, dict):
            entry = {key: _name(item[key]) for key in _FIELDS if key in item and _name(item[key]) is not None}
            for key in ('value', 'values'):
                if key in item:
                    entry[key + '_summary'] = _value(item[key])
            result.append(entry)
        elif isinstance(item, str):
            result.append(_name(item))
    if len(value) > 50:
        result.append({'omitted_count': len(value) - 50})
    return result


def extraction_shape(extraction: Any) -> dict[str, Any]:
    if not isinstance(extraction, dict):
        return {'present': False}
    result: dict[str, Any] = {'present': True}
    for key in ('实体', '指标', '维度', '展示字段', '过滤条件', '排序'):
        result[key] = _items(extraction.get(key))
    for key in ('意图', '是否去重', '是否查询数据库'):
        result[key] = _name(extraction.get(key))
    time = extraction.get('时间粒度')
    if isinstance(time, dict):
        result['时间粒度'] = {'unit': _name(time.get('unit')),
                              'anchor': _name(time.get('anchor')),
                              'time_range_summary': _value(time.get('time_range'))}
    result['限制'] = extraction.get('限制') if type(extraction.get('限制')) in (int, float) else None
    return result


def asl_shape(asl: Any) -> dict[str, Any]:
    if not isinstance(asl, dict):
        return {'present': False}
    result: dict[str, Any] = {'present': True, 'version': _name(asl.get('version'))}
    subject = asl.get('subject')
    result['subject'] = _name(subject.get('entity')) if isinstance(subject, dict) else _name(subject)
    for key in ('metrics', 'dimensions', 'display_fields', 'filters', 'related_filters'):
        result[key] = _items(asl.get(key))
    sort = asl.get('sort')
    result['sort'] = _items([sort]) if isinstance(sort, dict) else []
    result['having_count'] = len(asl.get('having') or []) if isinstance(asl.get('having'), list) else 0
    result['having'] = [_expression_shape(item) for item in (asl.get('having') or [])[:50]] if isinstance(asl.get('having'), list) else []
    result['ambiguity'] = [
        {'type': _name(item.get('type')), 'field': _name(item.get('field')),
         'ambiguity_id': _name(item.get('ambiguity_id')),
         'phrase_summary': _value(item.get('phrase')),
         'reason': _reason(item.get('question'))}
        for item in (asl.get('ambiguity') or [])[:50] if isinstance(item, dict)
    ]
    time = asl.get('time_context')
    if isinstance(time, dict):
        result['time_context'] = {key: _name(time.get(key)) for key in ('type', 'unit', 'anchor')}
        for key in ('start', 'end'):
            value = time.get(key)
            result['time_context'][key] = value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
            result['time_context'][key + '_summary'] = _value(value)
    result['limit'] = asl.get('limit') if type(asl.get('limit')) is int else None
    return result


def _reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Structured blocker messages may embed complete user-provided JSON values.
    if '}' in value:
        value = value.rsplit('}', 1)[-1]
    value = re.sub(r'(?i)(?:value|values|值)\s*[:=]\s*[^,;，；]+', '[值已省略]', value)
    value = re.sub(r'“[^”]*”|\"[^\"]*\"|\'[^\']*\'', '[内容已省略]', value)
    return redact(value, 800).replace('\n', ' ')


def _expression_shape(value: Any) -> dict[str, Any]:
    summary = _value(value)
    if isinstance(value, str):
        # Expressions can contain literal strings; remove them before extracting identifiers.
        expression = re.sub(r"'(?:''|\\.|[^'])*'", '?', value)
        expression = re.sub(r'"(?:""|\\.|[^"])*"', '?', expression)
        summary['fields'] = list(dict.fromkeys(re.findall(r'\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b', expression)))[:50]
        summary['aggregates'] = list(dict.fromkeys(x.upper() for x in re.findall(r'\b(SUM|COUNT|AVG|MIN|MAX)\s*\(', expression, re.I)))
        summary['operators'] = re.findall(r'>=|<=|!=|<>|(?<![<>!])=(?!=)|(?<![<>])>|(?<![<>])<', expression)[:50]
    return summary


def sql_shape(sql: Any) -> dict[str, Any]:
    if not isinstance(sql, str):
        return {'present': False}
    scrubbed = re.sub(r'/\*.*?\*/|--[^\r\n]*', ' ', sql, flags=re.S)
    scrubbed = re.sub(r"'(?:''|\\.|[^'])*'", "?", scrubbed)
    scrubbed = re.sub(r'"(?:""|\\.|[^"])*"', '?', scrubbed)
    return {'present': True, 'fingerprint': hashlib.sha256(sql.encode('utf-8')).hexdigest(),
            'method': 'text_summary', 'has_group_by': bool(re.search(r'\bGROUP\s+BY\b', scrubbed, re.I)),
            'has_having': bool(re.search(r'\bHAVING\b', scrubbed, re.I)),
            'has_where': bool(re.search(r'\bWHERE\b', scrubbed, re.I)),
            'has_join': bool(re.search(r'\bJOIN\b', scrubbed, re.I)),
            'has_limit': bool(re.search(r'\bLIMIT\b', scrubbed, re.I))}


def record(stage: str, request: Any, *, extraction: Any = None, asl: Any = None,
           sql: Any = None, status: str | None = None, error_code: Any = None,
           row_count: Any = None, repairs: Any = None, source_id: Any = None,
           reason: Any = None) -> None:
    try:
        details = {}
        if extraction is not None or stage in {'STRUCTURED_EXTRACTION', 'STRUCTURED_BLOCKER'}:
            details['extraction'] = extraction_shape(extraction)
        if asl is not None:
            details['asl'] = asl_shape(asl)
            labels = set()
            if isinstance(extraction, dict):
                for key in ('实体', '指标', '维度', '展示字段', '过滤条件', '排序'):
                    for item in _items(extraction.get(key)):
                        if isinstance(item, str):
                            labels.add(item)
                        elif isinstance(item, dict):
                            labels.update(value for key, value in item.items()
                                          if key in ('name', 'field', 'entity') and isinstance(value, str))
            if isinstance(asl, dict) and isinstance(asl.get('ambiguity'), list):
                issues = [item for item in asl['ambiguity'][:50] if isinstance(item, dict)]
                for item, output in zip(issues, details['asl']['ambiguity']):
                    phrase = _name(item.get('phrase'))
                    if phrase and phrase in labels:
                        output['parameter_name'] = phrase
        if sql is not None:
            details['sql'] = sql_shape(sql)
        if error_code is not None:
            details['error_code'] = _name(str(error_code))
        if type(row_count) is int:
            details['row_count'] = row_count
        if source_id is not None:
            details['data_source_id'] = _name(str(source_id))
        if reason is not None:
            details['reason'] = _reason(reason)
        if isinstance(repairs, list):
            details['repairs'] = []
            for item in repairs[:50]:
                if not isinstance(item, dict):
                    continue
                fields = ('type', 'source', 'slot', 'section', 'original_key', 'key', 'field')
                entry = {key: _name(item.get(key)) for key in fields if item.get(key) is not None}
                if item.get('type') == 'VECTOR_MENTION_CANONICALIZED':
                    entry.update({key: _name(item.get(key)) for key in ('original', 'canonical') if item.get(key) is not None})
                for key in ('score', 'similarity'):
                    if type(item.get(key)) in (int, float) and 0 <= item[key] <= 1:
                        entry[key] = item[key]
                details['repairs'].append(entry)
            if len(repairs) > 50:
                details['repairs'].append({'omitted_count': len(repairs) - 50})
        emit(stage, request_id=getattr(request, 'request_id', None),
             model_id=getattr(request, 'semantic_model_id', None),
             domains=getattr(request, 'business_domain_ids', None), status=status, **details)
    except Exception:
        logger.warning('query_diagnostic_unavailable stage=%s', stage)


def emit(stage: str, *, request_id: Any, model_id: Any = None,
         domains: Any = None, status: str | None = None, **details: Any) -> None:
    """Diagnostic failure must never change the query path."""
    try:
        from app.services.progress import _task_progress_context
        task = _task_progress_context.get() or {}
        record = {'stage': stage, 'request_id': str(request_id), 'task_id': task.get('task_id'),
                  'semantic_model_id': model_id, 'business_domain_ids': domains, 'status': status, **details}
        logger.info('query_diagnostic %s', json.dumps(record, ensure_ascii=False, default=str, separators=(',', ':')))
    except Exception:
        logger.warning('query_diagnostic_unavailable stage=%s', stage)
