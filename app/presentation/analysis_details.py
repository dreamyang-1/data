"""Business-facing time-series details; executed facts stay unchanged."""
from decimal import Decimal


TREND_METHODS = {
    'theil_sen_pairwise_slope', 'period_over_period', 'year_over_year',
    'neighbor_direction_reversal', 'adjacent_sign_runs', 'temporal_continuity',
    'extrema', 'volatility',
}
DIRECTIONS = {'UP': '上升', 'DOWN': '下降', 'FLAT': '持平',
              'MIXED': '震荡', 'UNDETERMINED': '暂无法判断'}


def compact_time_series(facts):
    reports = facts.get('capability_results') or []
    return bool(reports) and all(report.get('method') in TREND_METHODS for report in reports)


def _number(value, *, percent=False):
    if value is None:
        return '—'
    number = Decimal(str(value))
    if not number.is_finite():
        return '—'
    if percent:
        return f'{number * 100:.4f}%'
    return f'{number:,.4f}'.rstrip('0').rstrip('.')


def trend_capability_text(report, rows=None):
    """One display policy for progress and final output, without sparse tables.

    Period comparisons already live in the combined result table. Diagnostics
    explain their scope in prose rather than exposing internal schemas.
    """
    name, method = report['name'], report['method']
    if report['status'] != 'COMPLETED':
        status = {'NOT_COMPUTABLE': '不可计算', 'NOT_APPLICABLE': '不适用'}.get(report['status'], '未完成')
        return f"{name}：{status}。{report.get('reason') or report.get('summary') or ''}"
    data = (report.get('rows') or []) if rows is None else rows
    if method in {'period_over_period', 'year_over_year'}:
        count = sum(row.get('absolute_change') is not None for row in data)
        text = f'{name}：已完成{count}组期间比较，明细统一列入最终结果表。'
        if any(row.get('base') == 0 and row.get('change_rate') is None for row in data):
            text += '基期为0时仅展示增减量，变化率记为“—”。'
        if count < len(data):
            text += f'另有{len(data)-count}期缺少可比较基期，未补0。'
        return text
    if method == 'theil_sen_pairwise_slope':
        values = {key: value for row in data for key, value in row.items() if value is not None}
        lines = []
        labels = [('point_count', '时间点数'), ('start', '起始值'), ('end', '结束值'),
                  ('absolute_change', '首尾增减量'), ('change_rate', '首尾变化率'),
                  ('minimum', '最低值'), ('maximum', '最高值')]
        metrics = [f'{label}：{_number(values[key], percent=key == "change_rate")}'
                   for key, label in labels if key in values]
        if metrics:
            lines.append('目标窗口：' + '；'.join(metrics) + '。')
        direction = values.get('direction')
        if direction:
            diagnostic = '完整时间桶走势：' + DIRECTIONS.get(direction, '暂无法判断')
            for key, label in [('robust_slope_per_period', '每期稳健斜率'),
                               ('direction_consistency', '方向一致率'),
                               ('coefficient_of_variation', '变异系数')]:
                if key in values:
                    diagnostic += f'；{label}：{_number(values[key], percent=key != "robust_slope_per_period")}'
            basis = values.get('basis_labels') or []
            if basis:
                diagnostic += '；判定所用期间：' + '、'.join(map(str, basis))
            lines.append(diagnostic + '。')
        return name + '：\n' + '\n'.join(lines or ['完整时间点不足，暂无法判断走势。'])
    if method == 'temporal_continuity':
        values = {key: value for row in data for key, value in row.items()}
        grain = {'day': '日', 'week': '周', 'month': '月', 'quarter': '季', 'year': '年'}.get(values.get('grain'))
        gaps = values.get('gaps') or []
        if gaps:
            text = '存在时间间隔：' + '；'.join(
                f"{gap.get('from', '未知起点')}至{gap.get('to', '未知终点')}之间不连续"
                if isinstance(gap, dict) else str(gap) for gap in gaps) + '。'
        elif values.get('is_complete') is True:
            text = f'按{grain}检查，时间序列连续，未发现缺期。' if grain else '时间序列连续，未发现缺期。'
        else:
            text = '现有时间标签不足以确认序列连续性。'
        partial = [str(values.get(label) or position) for flag, label, position in [
            ('first_bucket_partial', 'first_partial_label', '首期'),
            ('last_bucket_partial', 'last_partial_label', '末期')] if values.get(flag)]
        if partial:
            text += '、'.join(dict.fromkeys(partial)) + '为残缺时间桶，不与完整期间直接比较。'
        elif all(values.get(key) is False for key in ('first_bucket_partial', 'last_bucket_partial')):
            text += '首尾时间桶未被查询边界截断。'
        return name + '：' + text
    if method == 'neighbor_direction_reversal':
        details = [f"{row['label']}为{'峰值' if row.get('type') == 'PEAK' else '谷值'}"
                   for row in data]
        return name + '：' + ('；'.join(details) + '。' if details else '未发现相邻完整期间的涨跌方向反转。')
    if method == 'adjacent_sign_runs':
        details = [f"{row['start_label']}至{row['end_label']}：{DIRECTIONS.get(row.get('direction'), '方向未定')}，"
                   f"连续{row['period_count']}次变动，累计增减{_number(row.get('absolute_change'))}"
                   + (f"（{_number(row['change_rate'], percent=True)}）" if row.get('change_rate') is not None else '')
                   for row in data]
        return name + '：\n' + ('\n'.join(details) if details else '未发现可连接的连续涨跌区间。')
    # Independently selected extrema/volatility have compact Chinese fields.
    return name + '：' + ('；'.join(f'{key}：{_number(value)}' for row in data for key, value in row.items())
                         if data else '没有可计算的完整期间数据。')
