"""Private PyMySQL templates: values never become SQL grammar during planning."""
import hashlib
import json
import math


class BoundSQLInvalid(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise BoundSQLInvalid(code)


def statement_fingerprint(sql, parameters):
    return hashlib.sha256(json.dumps({'sql':sql, 'parameters':parameters}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_bound_sql(sql, parameters):
    """Only generated, unquoted named placeholders, each with one scalar value."""
    _require(isinstance(sql, str) and 0 < len(sql) <= 200000, 'BOUND_SQL_TEMPLATE_INVALID')
    _require(isinstance(parameters, dict) and len(parameters) <= 10000, 'BOUND_SQL_PARAMETERS_INVALID')
    _require(set(parameters) == {'v2_p'+str(i) for i in range(len(parameters))}, 'BOUND_SQL_PARAMETER_KEYS_INVALID')
    for value in parameters.values():
        _require(value is None or type(value) in (str,int,float,bool), 'BOUND_SQL_PARAMETER_TYPE_INVALID')
        if isinstance(value, float): _require(math.isfinite(value), 'BOUND_SQL_PARAMETER_TYPE_INVALID')
    try:
        encoded = json.dumps(parameters, ensure_ascii=False).encode('utf-8')
    except (UnicodeError, ValueError):
        raise BoundSQLInvalid('BOUND_SQL_PARAMETER_ENCODING_INVALID') from None
    _require(len(encoded) <= 1000000, 'BOUND_SQL_PARAMETERS_TOO_LARGE')
    seen, quote, index = [], None, 0
    while index < len(sql):
        char = sql[index]
        if char == '%':
            if sql[index:index+2] == '%%':
                index += 2
                continue
            end = sql.find(')s', index+2, index+24)
            _require(quote is None and sql[index:index+2] == '%(' and end >= 0, 'BOUND_SQL_PLACEHOLDER_INVALID')
            key = sql[index+2:end]
            _require(key in parameters, 'BOUND_SQL_PARAMETER_MISSING')
            seen.append(key); index = end+2
            continue
        if quote:
            _require(char != '\\', 'BOUND_SQL_TEMPLATE_SQL_MODE_UNPROVEN')
            if char == quote:
                if sql[index:index+2] == quote*2:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"', '`'):
            quote = char
        elif char == '#' or sql[index:index+2] in ('--', '/*'):
            raise BoundSQLInvalid('BOUND_SQL_TEMPLATE_COMMENT_UNSUPPORTED')
        index += 1
    _require(quote is None and len(seen) == len(parameters) and set(seen) == set(parameters), 'BOUND_SQL_PARAMETER_COUNT_MISMATCH')


def parameterize_sql(sql, parameters):
    """Convert compiler-owned markers; escape other percent signs for DB-API."""
    template = sql.replace('%', '%%')
    for index, key in enumerate(parameters):
        _require(key == 'v2_p'+str(index), 'BOUND_SQL_PARAMETER_KEYS_INVALID')
        marker = '__v2_bind_'+str(index)+'__'
        _require(template.count(marker) == 1, 'BOUND_SQL_MARKER_COLLISION')
        template = template.replace(marker, '%('+key+')s')
    _require('__v2_bind_' not in template, 'BOUND_SQL_MARKER_COLLISION')
    validate_bound_sql(template, parameters)
    return template
