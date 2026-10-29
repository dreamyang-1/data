"""Materialize executed analysis facts for tables and immutable follow-ups."""
from app.analysis.chain_tools.temporal_window import bucket_bounds


def analysis_result_rows(columns, rows, facts):
    if facts.get('grouped_period_comparison'):
        label, identity = facts['label_column'], facts['identity_column']
        selected = facts.get('tool_selection')
        difference = selected is None or bool({'comparison_difference', 'group_comparison'} & set(selected))
        ratio = selected is None or 'ratio_and_index' in selected
        columns = [label, identity, '基期值', '本期值']
        if difference:
            columns += ['增减额', '变化率']
        if ratio:
            columns += ['倍数', '基期100指数']
        columns += ['状态']
        rows = []
        for item in facts['comparisons']:
            rows.append({label: item['label'], identity: '／'.join(item['group_key']),
                '基期值': item['base'], '本期值': item['current'], '增减额': item['absolute_change'],
                '变化率': None if item['change_rate'] is None else f"{item['change_rate']:.4%}",
                '倍数': item['ratio'], '基期100指数': item['base_100_index'], '状态': item['status']})
        return columns, [{col: row[col] for col in columns} for row in rows]
    columns, rows = list(columns), [dict(row) for row in rows]
    scope = facts.get('result_scope') or {}
    temporal = scope.get('time_column')
    if temporal:
        labels = set(scope['labels'])
        rows = [row for row in rows if str(row.get(temporal, '')).strip() in labels]
        rows.sort(key=lambda row: bucket_bounds(str(row[temporal]))[0])
        # An overall monthly change cannot be attributed to individual groups.
        if len({str(row[temporal]) for row in rows}) == len(rows):
            for key, prefix in (('period_over_period', '环比'), ('year_over_year', '同比')):
                if key not in facts:
                    continue
                # Keep unavailable comparisons in evidence and explanatory text,
                # rather than adding two entirely empty columns to the table.
                if facts[key].get('status') == 'NOT_COMPUTABLE':
                    continue
                changes = {str(item.get('to', item.get('label'))): item
                           for item in facts[key].get('changes') or []}
                if not any(change.get('absolute_change') is not None for label, change in changes.items() if label in labels):
                    continue
                fields = [prefix + '增减量', prefix + '变化率']
                columns.extend(field for field in fields if field not in columns)
                for row in rows:
                    change = changes.get(str(row[temporal]), {})
                    row[fields[0]] = change.get('absolute_change')
                    rate = change.get('change_rate')
                    row[fields[1]] = None if rate is None else f'{rate:.4%}'
    if facts.get('share_threshold') is not None:
        # 阈值筛选类问题的交付物是阈值集合；主结果表不能继续铺开全量分组。
        qualified_labels = {
            str(item['label']) for item in facts.get('qualified_shares') or []
        }
        label_column = next(
            (col for col in columns if col != facts.get('metric_column')
             and qualified_labels and {str(row.get(col)) for row in rows} == qualified_labels),
            None,
        )
        rows = ([row for row in rows if str(row.get(label_column)) in qualified_labels]
                if label_column else [])
    shares = facts.get('shares') or []
    if shares:
        labels = {str(item['label']) for item in shares}
        candidates = [col for col in columns if col != facts.get('metric_column')
                      and {str(row.get(col)) for row in rows} == labels]
        if len(candidates) == 1 and len(labels) == len(rows):
            label = candidates[0]
            indexed = {str(row[label]): row for row in rows}
            rows = [{**indexed[str(item['label'])], '占比': f"{item['share']:.4%}"}
                    for item in shares]
            if '占比' not in columns:
                columns.append('占比')
    return columns, rows
