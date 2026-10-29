"""Keep an amount per counted object distinct from a price per quantity.

Only current registered definitions supply names and formulas. This is a
planning constraint, not an ASL binding or an addition to catalog aliases.
"""
from dataclasses import dataclass
import re


_COUNT_UNITS = {'笔', '单', '次', '个', '家', '人', '张', '份', '户'}
_MONEY = r'(?:金额|金額|总额|總額|销售额|收入|费用|成本|amount|revenue|cost)'


def _metric_rows(spec, registered):
    columns = {}
    result = {}
    for line in spec.splitlines():
        if not line.lstrip().startswith('|'):
            columns = {}
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if cells and cells[0] in {'指标', '指标名称'}:
            columns = {label: index for index, label in enumerate(cells)}
            continue
        if not columns or not cells or cells[0] not in registered:
            continue
        def value(*labels):
            index = next((columns[label] for label in labels if label in columns), None)
            return cells[index] if index is not None and index < len(cells) else ''
        result[cells[0]] = {'unit': value('单位', '计量单位'),
                            'definition': value('业务口径', '计算口径', '指标口径')}
    return result


def _currency(unit):
    return bool(re.search(r'元|人民币|CNY|RMB|USD|美元', unit, re.I)) and not re.search(r'[/÷每]', unit)


def _object_roots(name, aliases):
    result = []
    for label in [name, *(alias for alias, canonical in aliases.items() if canonical == name)]:
        stem = re.sub(r'(?:总)?(?:笔数|次数|数量|总数|人数|家数|个数|单数|数)$', '', label)
        stem = re.sub(r'^(?:累计|有效|去重|全部|已合作)', '', stem)
        if stem != label and len(stem) >= 2 and stem not in result:
            result.append(stem)
    return result


def _asks_amount_per_object(question, root):
    root = re.escape(root)
    amount = r'(?:含税|不含税|税前|税后|销售|成交|采购|交易|结算|的){0,3}' + _MONEY
    return bool(re.search(
        root + r'(?:的)?(?:平均|均)' + amount + '|'
        r'平均(?:每)?(?:笔|个|次|张|份)?' + root + r'(?:的)?(?:平均|均)?' + amount,
        question, re.I))


@dataclass(frozen=True)
class MetricTargetContract:
    candidates: tuple[str, ...]
    conflicting_ratios: frozenset[str]
    explicitly_requested: frozenset[str]
    definitions: tuple[str, ...]

    def instruction(self):
        return ('\n本次问题的金额/计数口径合同（仅来自当前模型登记）：'
                + '；'.join(self.definitions)
                + '。任务必须交付这里匹配的指标；不可用金额除以商品数量的单价替换金额除以对象笔数的平均金额。'
                '保留用户的业务分组、时间和关联展示字段；除非用户明确要求逐对象展示，不另按分母对象逐条分组。\n')

    def validate(self, boundaries, catalog, target_reader):
        if len(self.candidates) != 1:
            raise ValueError('平均金额存在多个已登记口径，不能替用户选择：' + '、'.join(self.candidates))
        selected = {name for task in boundaries for name in target_reader(task, catalog)}
        unwanted = (selected & self.conflicting_ratios) - self.explicitly_requested - set(self.candidates)
        if self.candidates[0] not in selected or unwanted:
            raise ValueError('任务计划替换或遗漏了问题要求的金额/计数口径；必须交付：'
                             + self.candidates[0] + '；登记依据：' + '；'.join(self.definitions))


def object_average_contract(question, spec, catalog, dependencies):
    """Require unique registered formula evidence; preserve explicit other metrics."""
    if re.match(r'^(?:请)?(?:解释|说明|介绍|定义|什么是|如何计算)', question.strip()):
        return None
    registered, aliases = catalog
    rows = _metric_rows(spec, registered)
    ratios, eligible = set(), {}
    explicit = {name for name in dependencies for term in [name, *(
        alias for alias, canonical in aliases.items() if canonical == name)] if term in question}
    explicit_ratios = {name for name in explicit if _currency(rows.get(name, {}).get('unit', ''))
                       and re.search(r'÷|/|除以', rows.get(name, {}).get('definition', ''))}
    explicit_denominators = {name for name in registered for term in [name, *(
        alias for alias, canonical in aliases.items() if canonical == name)]
        if re.search(r'(?:÷|/|除以)\s*' + re.escape(term), question)}
    for name, components in dependencies.items():
        row = rows.get(name, {})
        definition = row.get('definition', '')
        if len(components) != 2 or not _currency(row.get('unit', '')):
            continue
        parts = re.split(r'÷|/|除以', definition)
        if len(parts) != 2 or not re.search(_MONEY, parts[0], re.I):
            continue
        monetary = [item for item in components if _currency(rows.get(item, {}).get('unit', ''))]
        if len(monetary) != 1:
            continue
        denominator = next(item for item in components if item != monetary[0])
        ratios.add(name)
        if explicit_denominators and denominator not in explicit_denominators:
            continue
        if rows.get(denominator, {}).get('unit') not in _COUNT_UNITS:
            continue
        roots = _object_roots(denominator, aliases)
        if not any(root in parts[1] for root in roots):
            continue
        if name in explicit_ratios or (not explicit_ratios and any(_asks_amount_per_object(question, root) for root in roots)):
            eligible[name] = f'{name}：{definition}（分母登记指标：{denominator}）'
    if not eligible or (explicit_ratios and not explicit_ratios.issubset(eligible)):
        return None
    return MetricTargetContract(tuple(eligible), frozenset(ratios), frozenset(explicit), tuple(eligible.values()))
