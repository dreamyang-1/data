"""Structural JOIN checks for the service's SELECT boundary (not a SQL rewrite).

Each JOIN must connect its new alias to an earlier alias in the same query.
Nested SELECTs are checked independently; their admission remains the existing
read-only policy's responsibility. Database parsing still checks column syntax.
"""
import re


class SQLJoinError(ValueError):
    pass


_TOKEN = re.compile(
    r"/\*.*?\*/|--[^\r\n]*|\#[^\r\n]*|'(?:''|\\.|[^'])*'|"
    r'"(?:""|\\.|[^"])*"|`(?:``|[^`])*`|%\([A-Za-z_]\w*\)s|'
    r'[\w]+|<=>|>=|<=|<>|!=|[^\s]', re.S)
_IDENT = re.compile(r'(?:`(?:``|[^`])+`|[^\W\d]\w*)', re.UNICODE)
_END = {'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'WINDOW', 'UNION', 'FOR', ';'}
_JOIN = {'JOIN', 'LEFT', 'RIGHT', 'FULL', 'INNER', 'OUTER', 'CROSS', 'NATURAL', 'STRAIGHT_JOIN'}


def _word(item):
    return item.upper() if isinstance(item, str) else ''


def _name(item):
    if not isinstance(item, str) or not _IDENT.fullmatch(item):
        raise SQLJoinError('SQL_JOIN_INVALID: 关联表或别名不合法')
    return item[1:-1].replace('``', '`') if item.startswith('`') else item


def _tree(sql):
    root, stack = [], []
    current = root
    for token in _TOKEN.findall(sql):
        if token.startswith(('/*', '--', '#')):
            if token.startswith('/*!') or (token.startswith('/*+') and re.search(
                    r'\b(?:MAX_EXECUTION_TIME|MAX_STATEMENT_TIME|SET_VAR)\b', token, re.I)):
                raise SQLJoinError('SQL_JOIN_INVALID: SQL 不得覆盖服务端执行保护')
            continue
        if token == '(':
            child = []; current.append(child); stack.append(current); current = child
            if len(stack) > 100:
                raise SQLJoinError('SQL_JOIN_INVALID: SQL 嵌套过深')
        elif token == ')':
            if not stack:
                raise SQLJoinError('SQL_JOIN_INVALID: SQL 括号不匹配')
            current = stack.pop()
        else:
            current.append(token)
    if stack:
        raise SQLJoinError('SQL_JOIN_INVALID: SQL 括号不匹配')
    return root


def _flat(items):
    for item in items:
        if isinstance(item, list):
            yield from _flat(item)
        else:
            yield item


def _references(items):
    tokens = list(_flat(items)); names = set()
    for i in range(len(tokens) - 2):
        if tokens[i + 1] == '.' and _IDENT.fullmatch(tokens[i]) and _IDENT.fullmatch(tokens[i + 2]):
            if i + 3 >= len(tokens) or tokens[i + 3] != '.':
                names.add(_name(tokens[i]))
    return names


def _connects(items, new, known):
    while len(items) == 1 and isinstance(items[0], list):
        items = items[0]
    # Every OR alternative must retain a relationship; AND requires one.
    for operator, combine in [('OR', all), ('AND', any)]:
        indexes = [i for i, item in enumerate(items) if _word(item) == operator]
        if indexes and not (operator == 'AND' and any(_word(x) == 'BETWEEN' for x in items)):
            bounds = [-1, *indexes, len(items)]
            return combine(_connects(items[a + 1:b], new, known) for a, b in zip(bounds, bounds[1:]))
    for i, item in enumerate(items):
        if _word(item) in {'=', '<=>', '<>', '!=', '<', '>', '<=', '>=', 'BETWEEN'}:
            left, right = _references(items[:i]), _references(items[i + 1:])
            if (new in left and right & known) or (new in right and left & known):
                return True
    return False


def _table(items, index):
    if index >= len(items):
        raise SQLJoinError('SQL_JOIN_INVALID: 缺少关联表')
    key = _name(items[index]); index += 1
    while index < len(items) and items[index] == '.':
        if index + 1 >= len(items):
            raise SQLJoinError('SQL_JOIN_INVALID: 表名不完整')
        key = _name(items[index + 1]); index += 2
    if index < len(items) and _word(items[index]) == 'AS':
        if index + 1 >= len(items):
            raise SQLJoinError('SQL_JOIN_INVALID: 缺少关联别名')
        return _name(items[index + 1]), index + 2
    if index < len(items) and _word(items[index]) not in _END | _JOIN | {'ON', 'USING', ','}:
        key = _name(items[index]); index += 1
    return key, index


def _select(items):
    if sum(_word(x) == 'SELECT' for x in items) > 1:
        raise SQLJoinError('SQL_JOIN_INVALID: SQL 中出现拼接重复的 SELECT')
    starts = [i for i, x in enumerate(items) if _word(x) == 'FROM']
    if not starts:
        if any(_word(x) in _JOIN for x in items):
            raise SQLJoinError('SQL_JOIN_INVALID: JOIN 缺少 FROM')
        return
    if len(starts) != 1:
        raise SQLJoinError('SQL_JOIN_INVALID: 重复的 FROM')
    base, index = _table(items, starts[0] + 1)
    known = {base}
    while index < len(items) and _word(items[index]) not in _END:
        while index < len(items) and _word(items[index]) in _JOIN - {'JOIN'}:
            index += 1
        if index >= len(items) or _word(items[index]) != 'JOIN':
            raise SQLJoinError('SQL_JOIN_INVALID: 不允许无连接条件的表组合')
        new, index = _table(items, index + 1)
        if new in known:
            raise SQLJoinError('SQL_JOIN_INVALID: 查询内存在重复关联别名')
        mode = _word(items[index]) if index < len(items) else ''
        index += 1
        if mode == 'USING' and index < len(items) and isinstance(items[index], list):
            columns = items[index]
            if not columns or any(x != ',' and not (isinstance(x, str) and _IDENT.fullmatch(x)) for x in columns):
                raise SQLJoinError('SQL_JOIN_INVALID: USING 缺少有效关联字段')
            index += 1
        elif mode == 'ON':
            start = index
            while index < len(items) and _word(items[index]) not in _END | _JOIN:
                index += 1
            condition = items[start:index]
            refs = _references(condition)
            if any(_word(x) == 'SELECT' for x in _flat(condition)) or refs - known - {new}:
                raise SQLJoinError('SQL_JOIN_INVALID: ON 引用了尚未加入或不属于本查询的表')
            if not _connects(condition, new, known):
                raise SQLJoinError('SQL_JOIN_INVALID: ON 未连接新表与已有表，已阻止笛卡尔积风险')
        else:
            raise SQLJoinError('SQL_JOIN_INVALID: JOIN 缺少 ON 或 USING')
        known.add(new)


def validate_join_connectivity(sql):
    def visit(items):
        for item in items:
            if isinstance(item, list):
                visit(item)
        bounds = [-1, *[i for i, x in enumerate(items) if _word(x) == 'UNION'], len(items)]
        for a, b in zip(bounds, bounds[1:]):
            part = items[a + 1:b]
            if any(_word(x) == 'SELECT' for x in part):
                _select(part)
    visit(_tree(sql))
