"""Query-local assembly of compiler-owned JOINs, not a SQL text rewrite.

Reuse identical relations; retain distinct aliases and reject conflicting
definitions of one alias. Subquery scopes are assembled independently.
"""
import re
from dataclasses import dataclass

_IDENT = r'(?:`(?:``|[^`])+`|[A-Za-z_]\w*)'
_TABLE = rf'{_IDENT}(?:\s*\.\s*{_IDENT})?'
_HEADER = re.compile(
    rf'(?P<kind>(?:(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?|INNER|CROSS)?\s*JOIN)\s+'
    rf'(?P<table>{_TABLE})(?:\s+(?:AS\s+)?(?P<alias>{_IDENT}))?\s+ON\s+(?P<on>.+)',
    re.I | re.S)
_JOIN = re.compile(r'\b(?:(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+|(?:INNER|CROSS)\s+)?JOIN\b', re.I)
_TOKENS = re.compile(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"|`(?:``|[^`])*`|[\w]+|<=>|>=|<=|<>|!=|[^\s]", re.S)


def _identifier(text):
    return text[1:-1].replace('``', '`') if text.startswith('`') else text


def table_key(table):
    """SQL's visible name is the alias or the last part of the table name."""
    return _identifier(re.findall(_IDENT, table)[-1])


def _mask_quotes(text):
    chars = list(text)
    index = 0
    while index < len(text):
        if text[index] not in "'\"`":
            index += 1
            continue
        quote = text[index]
        start = index
        index += 1
        while index < len(text):
            if text[index] == '\\':
                index += 2
            elif text[index] == quote:
                if text[index:index + 2] == quote * 2:
                    index += 2
                else:
                    index += 1
                    break
            else:
                index += 1
        else:
            raise ValueError('JOIN 引号未闭合，请检查语义关联配置')
        chars[start:index] = ' ' * (index - start)
    return ''.join(chars)


def _condition_key(condition):
    tokens = tuple(_identifier(token) for token in _TOKENS.findall(condition))
    # Equality endpoint order and AND order do not change a published join.
    atoms, current, depth = [], [], 0
    for token in tokens:
        if token.upper() == 'AND' and depth == 0:
            atoms.append(tuple(current)); current = []
        else:
            current.append(token)
            depth += (token == '(') - (token == ')')
    atoms.append(tuple(current))
    simple = re.compile(r'[A-Za-z_]\w*\.[A-Za-z_]\w*=[A-Za-z_]\w*\.[A-Za-z_]\w*')
    if all(simple.fullmatch(''.join(atom)) for atom in atoms):
        return tuple(sorted(tuple(sorted(''.join(atom).split('='))) for atom in atoms))
    return tokens


@dataclass(frozen=True)
class JoinPart:
    key: str
    signature: tuple
    sql: str


def split_joins(text):
    """Split top-level JOIN boundaries, never letters in ON, strings or subqueries."""
    masked = _mask_quotes(text)
    boundaries, depth, cursor = [], 0, 0
    for match in _JOIN.finditer(masked):
        between = masked[cursor:match.start()]
        depth += between.count('(') - between.count(')')
        if depth == 0:
            boundaries.append(match.start())
        cursor = match.start()
    prefix = text[:boundaries[0]] if boundaries else text
    if prefix.strip() and not re.fullmatch(rf'\s*FROM\s+{_TABLE}(?:\s+(?:AS\s+)?{_IDENT})?\s*', prefix, re.I):
        raise ValueError('无法解析 JOIN 片段，请检查语义关联配置')
    result = []
    for start, end in zip(boundaries, boundaries[1:] + [len(text)]):
        segment = text[start:end].strip()
        match = _HEADER.fullmatch(segment)
        if not match:
            raise ValueError('JOIN 缺少有效表名或 ON 条件，请检查语义关联配置')
        table = tuple(_identifier(part) for part in re.findall(_IDENT, match['table']))
        key = _identifier(match['alias']) if match['alias'] else table[-1]
        kind = ' '.join(match['kind'].upper().split()).replace(' OUTER', '')
        if kind == 'JOIN':
            kind = 'INNER JOIN'
        result.append(JoinPart(key, (table, kind, _condition_key(match['on'])), segment))
    return result


class JoinAssembly:
    def __init__(self, existing_tables=(), *, strict_base=False):
        self._seen = {table_key(table): None for table in existing_tables}
        self.strict_base = strict_base
        self.fragments = []

    @property
    def tables(self):
        return set(self._seen)

    def add(self, clauses):
        for clause in clauses:
            for part in split_joins(clause):
                if part.key in self._seen:
                    previous = self._seen[part.key]
                    if previous == part.signature or previous is None and not self.strict_base:
                        continue
                    raise ValueError(f'关联表/别名 {part.key} 被重复用于不同连接条件；请使用明确的关联角色和独立别名')
                self._seen[part.key] = part.signature
                self.fragments.append(part.sql)
