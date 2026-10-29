"""Identify explicitly declared calculation targets, never infer a new metric."""
import re


def declared_calculation_targets(question: str) -> list[str]:
    targets = []
    pattern = r'(?:计算|算出|求得|得到)\s*([^，,。;；（(]{2,40}?(?:金额|客单价|销售额|增长率|覆盖率|占比|比例|均值|平均值|总数|笔数|数量|时长))(?:[，,。;；（(]|$|，?按|降序|升序|排序|最高|最低|取前)'
    for match in re.finditer(pattern, question or ''):
        name = re.split(r'作为|记作|写作|即', match.group(1))[0].rsplit('的',1)[-1].strip()
        if name and name not in targets:
            targets.append(name)
        alias = {'订单平均金额':'平均订单金额','平均订单金额':'订单平均金额'}.get(name)
        if alias and alias not in targets:
            targets.append(alias)
    return targets
