"""Executed capability results, shared by progress, evidence and final answers."""
import math
from app.domain.models import PrimaryIntent as I


def enrich(request, columns, rows, facts):
    reports = []

    def add(name, method, summary, table=None, *, status='COMPLETED', reason=''):
        from app.analysis.tool_selection import report_allowed, report_capability
        if not report_allowed(request, method):
            return
        readiness=(facts.get('capability_readiness') or {}).get(report_capability(request.primary_intent,method))
        if readiness:
            status=readiness['status'];reason=readiness['reason'];table=[]
        table = table or []
        reports.append({'name': name, 'method': method, 'status': status, 'summary': summary,
                        'reason': reason, 'columns': list(dict.fromkeys(k for r in table for k in r)), 'rows': table})

    intent = request.primary_intent
    if intent == I.ROOT_CAUSE_ANALYSIS and 'volume_effect' in facts:
        add('量价效应与交互项', 'price_volume_decomposition', '基期单价×数量变化、基期数量×价格变化及交互项。', [facts.copy()])
        add('量价对账残差', 'effect_reconciliation', '总变化减去三个效应之和，检验恒等式。',
            [{'residual': facts.get('reconciliation_residual')}])
        add('业务因果核实', 'causal_evidence_check', '量价恒等式不能证明业务因果。', status='NOT_COMPUTABLE', reason='缺少外生业务证据')
        facts['capability_results'] = reports
        return reports
    if intent == I.ROOT_CAUSE_ANALYSIS and 'root_causes' in facts:
        add('多维贡献候选与解释力', 'multidimensional_root_cause', '各维度分别评估，不跨维度累计贡献。', [facts.copy()])
        facts['capability_results'] = reports
        return reports
    if intent == I.COMPOSITION_ANALYSIS and 'transitions' in facts:
        add('漏斗逐阶段转化与流失', 'funnel_conversion_analysis', '各阶段顺序和数量通过校验，逐阶段计算转化率及流失率。', facts['transitions'])
        facts['capability_results'] = reports
        return reports
    if intent == I.COMPOSITION_ANALYSIS and 'shifts' in facts:
        add('两期占比及结构百分点变化', 'structural_share_shift', '按同一维度的基期、本期总量计算份额变化。', facts['shifts'])
        add('结构变化对账', 'share_shift_reconciliation', '互斥分组份额变化之和应为0。',
            [{'base_total': facts['base_total'], 'current_total': facts['current_total'], 'residual': facts['share_change_residual']}])
        facts['capability_results'] = reports
        return reports
    if intent == I.TREND_ANALYSIS:
        if 'selected_extrema' in facts:
            add('区间极值', 'extrema', '仅在完整目标时间桶上计算最高与最低值。', facts['selected_extrema'])
        if 'selected_volatility' in facts:
            add('波动系数', 'volatility', '变异系数=总体标准差/|均值|；均值为0不可计算。', facts['selected_volatility'])
        add('整体趋势、极值与波动', 'theil_sen_pairwise_slope',
            '斜率与方向一致性刻画整体走势，变异系数刻画波动；汇总基于用户目标窗口。',
            [{k: facts.get(k) for k in ('start', 'end', 'absolute_change', 'change_rate', 'minimum', 'maximum', 'point_count')},
             facts.get('trend_diagnostics') or {}])
        for key, name in [('period_over_period', '环比增减量与变化率'), ('year_over_year', '同比增减量与变化率')]:
            value = facts.get(key) or {}
            table = value.get('changes') or []
            add(name, key, '变化率=(本期−基期)/|基期|；基期为0时只列增减量，残缺时间桶不比较。', table,
                status=value.get('status', 'COMPLETED' if table else 'NOT_COMPUTABLE'),
                reason=value.get('reason') or ('' if table else '未提供可比较基期，或用户明确排除此项'))
        add('方向拐点', 'neighbor_direction_reversal', '相邻完整时间桶涨跌方向反转；无命中时表为空。', facts.get('change_points'))
        add('连续上涨／下跌区间', 'adjacent_sign_runs', '只连接连续时间桶，同值或缺期会中断区间。', facts.get('continuous_runs'))
        add('时间完整性', 'temporal_continuity', '检查缺期与首尾残缺时间桶。',
            [facts.get('temporal_continuity') or {}, facts.get('edge_bucket_integrity') or {}])
    elif intent == I.COMPARISON_ANALYSIS:
        add('基期对齐、差额与变化率', facts.get('period_order_source', 'explicit_comparable_groups'),
            '按明确期间／角色对齐，不以返回行顺序猜测基期。',
            [{k: facts.get(k) for k in ('base_label', 'current_label', 'base', 'current', 'absolute_change', 'change_rate')}]
            if 'base' in facts else facts.get('comparisons') or facts.get('rankings') or facts.get('metric_summaries') or facts.get('entities') or [])
        base, current = facts.get('base'), facts.get('current')
        from app.analysis.tool_selection import report_allowed
        valid = report_allowed(request, 'current_divided_by_base') and isinstance(base, (int, float)) and isinstance(current, (int, float)) and base != 0
        if valid:
            facts['ratio'] = current / base
            facts['base_100_index'] = current / base * 100
        grouped_valid = facts.get('grouped_period_comparison') and any(
            item['ratio'] is not None for item in facts.get('comparisons', []))
        add('倍数与基期100指数', 'current_divided_by_base', '指数=本期/基期×100。',
            ([{'label': item['label'], '倍数': item['ratio'], '基期100指数': item['base_100_index']}
              for item in facts.get('comparisons', [])] if facts.get('grouped_period_comparison') else
             [{'倍数': facts.get('ratio'), '基期100指数': facts.get('base_100_index')}]),
            status='COMPLETED' if valid or grouped_valid else 'NOT_COMPUTABLE',
            reason='' if valid or grouped_valid else '没有可比较的非零基期；不得跨指标合成倍数')
        add('分组差异明细与排序', 'comparable_entity_difference', '逐组差额／变化率用于比较对象。',
            facts.get('comparisons') or facts.get('rankings') or facts.get('metric_summaries') or facts.get('entities') or [],
            status='COMPLETED' if facts.get('comparisons') or facts.get('rankings') or facts.get('metric_summaries') or facts.get('entities') else 'NOT_APPLICABLE',
            reason='' if facts.get('comparisons') or facts.get('rankings') or facts.get('metric_summaries') or facts.get('entities') else '本问题仅有总体两期，未请求业务分组')
    elif intent == I.COMPOSITION_ANALYSIS:
        shares = facts.get('shares') or facts.get('ranked_parts') or []
        if not shares and facts.get('threshold_set') and not facts.get('negative_group_count'):
            metric, label, total = facts['metric_column'], facts['label_column'], facts['total']
            shares = sorted([{'label': str(row[label]), 'value': float(row[metric]), 'share': float(row[metric])/total}
                             for row in rows], key=lambda part: part['share'], reverse=True)
            facts['shares'] = shares
        from app.analysis.tool_selection import report_allowed
        if shares:
            cumulative = 0
            for rank, part in enumerate(shares, 1):
                cumulative += part['share']
                part.update(rank=rank, cumulative_share=cumulative)
            if report_allowed(request, 'hhi_and_entropy'):
                fractions = [part['share'] for part in shares]
                hhi = sum(v*v for v in fractions)
                concentration = {'top1_share': fractions[0], 'top3_share': sum(fractions[:3]),
                    'hhi_0_to_1': hhi, 'effective_group_count': 1/hhi,
                    'entropy_nats': -sum(v*math.log(v) for v in fractions if v > 0),
                    'groups_to_reach_80_percent': next(i+1 for i, p in enumerate(shares) if p['cumulative_share'] >= .8 - 1e-12),
                    'share_sum': sum(fractions)}
                facts['concentration'] = concentration
        params=((getattr(request,'_analysis_contract',None) or {}).get('tool_selection') or {}).get('parameters',{}).get('composition_share',{})
        if 'minimum_share' in params:
            from app.analysis.chain_tools.composition import group_share_threshold
            limit,inclusive=group_share_threshold(params)
            qualified=[part for part in shares if part['share']>=limit] if inclusive else [part for part in shares if part['share']>limit]
            facts.update(share_threshold=limit,share_threshold_inclusive=inclusive,
                qualified_shares=qualified,qualified_count=len(qualified))
        thresholded = facts.get('share_threshold') is not None
        # 用户明确要求按单组占比阈值筛选时，交付物就是阈值集合，
        # 主表不能再把全部完整分组当作结果铺开。
        display_shares = (facts.get('qualified_shares') or []) if thresholded else shares
        add('总体对账、占比与降序排名', 'part_divided_by_total', '分母为完整、互斥分组的合计；不把TopN当总体。', display_shares)
        if thresholded:
            limit = facts['share_threshold']
            operator_text = '不低于' if facts.get('share_threshold_inclusive') else '超过'
            qualified = facts.get('qualified_shares') or []
            reason = ''
            if not qualified:
                reason = f'没有分组占比{operator_text}{limit:.0%}'
                highest = shares[0] if shares else None
                if highest:
                    reason += f'，最高为{highest["label"]}的{highest["share"]:.2%}'
            add('占比阈值名单', 'group_share_threshold_filter',
                f'仅保留单组占比{operator_text}{limit:.0%}的分组。', qualified,
                status='COMPLETED' if qualified else 'NOT_APPLICABLE',
                reason=reason)
        add('累计贡献与80%贡献集合', 'descending_cumulative_share', '包括首次跨过80%阈值的边界分组。', shares)
        add('集中度与分布均衡', 'hhi_and_entropy', 'HHI范围0至1，数值越高表示越集中。',
            [facts['concentration']] if 'concentration' in facts else [],
            status='COMPLETED' if 'concentration' in facts else 'NOT_COMPUTABLE',
            reason='' if 'concentration' in facts else '当前专用算子未返回完整分组占比')
        add('跨期结构变化', 'share_percentage_point_shift', '占比百分点变化需两期同维度数据。',
            facts.get('share_shifts') or [], status='COMPLETED' if facts.get('share_shifts') else 'NOT_APPLICABLE',
            reason='' if facts.get('share_shifts') else '问题只指定一个总体窗口，未定义比较基期')
    elif intent == I.ANOMALY_ANALYSIS:
        add('样本与时间轴检查', 'series_readiness_check', '不填补缺失日期，不将缺失值当成0。',
            [{'样本数': facts.get('point_count'), **(facts.get('time_axis') or {})}])
        for detector in facts.get('detectors') or []:
            add(detector['name']+'异常检测', detector['name'], '参数／阈值：'+str(detector['parameters']),
                detector['matches'], status=detector['status'], reason=detector['reason'])
        add('异常日期及判定依据', 'detector_union', facts.get('decision_rule', ''), facts.get('candidates'))
    elif intent == I.ROOT_CAUSE_ANALYSIS:
        candidates = facts.get('ranked_candidates') or []
        lifts = sum(max(0, p['contribution']) for p in candidates)
        drags = sum(min(0, p['contribution']) for p in candidates)
        offset = {'positive_change': lifts, 'negative_change': drags, 'net_change': lifts+drags,
                                        'offset_amount': min(lifts, abs(drags))}
        from app.analysis.tool_selection import report_allowed
        if report_allowed(request, 'signed_contribution_sum'):
            facts['offset_decomposition'] = offset
        add('分项增减贡献与排名', 'current_minus_baseline_by_member',
            '贡献率=分项变化/总体变化；绝对贡献份额=|分项变化|/Σ|变化|，两者不能混用。', candidates)
        add('拉升、拖累与相互抵消', 'signed_contribution_sum', '正负分项相抵后形成净变化。', [offset])
        add('整体变化对账与未解释残差', 'contribution_reconciliation', '残差=独立总体变化−分项变化合计。',
            [{'整体变化': facts.get('observed_change'), '分项合计': facts.get('contribution_sum'), **(facts.get('additivity_check') or {})}])
        add('业务原因证据', 'matched_knowledge', '统计贡献不能证明因果关系。', facts.get('matched_knowledge') or [],
            status='COMPLETED' if facts.get('matched_knowledge') else 'NOT_COMPUTABLE',
            reason='' if facts.get('matched_knowledge') else '未提供可核实的业务事件或外生变量')
        add('量价分解', 'price_volume_decomposition', '仅在有同口径、可加的销量／价格输入时执行。',
            status='NOT_APPLICABLE', reason='本任务按渠道贡献拆解，金额水平不能冒充销量与单价')
    elif intent == I.FORECAST_ANALYSIS:
        add('历史范围与连续性', 'forecast_readiness_check', '至少6个完整、连续、同口径观测点；不自动超出用户训练区间。',
            [{k: facts.get(k) for k in ('history_start', 'history_end', 'history_points', 'forecast_horizon')}])
        add('候选模型比较', 'expanding_window_one_step_backtest', '按相同滚动验证窗口的MAE选优；同分优先简单模型。',
            [{'模型': key, **value} for key, value in (facts.get('candidate_scores') or {}).items()])
        add('逐折回测明细', 'train_prefix_predict_next', '每折只使用验证点之前的历史，避免未来信息泄漏。', facts.get('backtests'))
        add('下一期预测与残差参考区间', 'selected_model_refit',
            '区间来自留出误差绝对值P90，不宣称具有90%覆盖概率的置信区间。',
            [{k: facts.get(k) for k in ('selected_model', 'forecast_period', 'forecast', 'residual_reference_interval', 'validation_mae', 'validation_mape', 'validation_points', 'forecast_quality')}])
        seasonal_period = 12 if facts.get('history_granularity') == 'month' else None
        add('季节性模型适用性', 'seasonal_history_check', '年度月度季节性至少需要两个完整周期（24期）。',
            status='NOT_COMPUTABLE', reason='当前候选集未包含经过验证的季节模型；'+('6个月无法估计年度季节性' if seasonal_period and facts.get('history_points', 0) < 24 else '需额外季节性回测和业务变量'))
    facts['capability_results'] = reports
    return reports


FIELD_LABELS = {
    'label': '日期／对象', 'from': '基期', 'to': '本期', 'base': '基期值', 'current': '本期值',
    'value': '指标值', 'absolute_change': '增减量', 'change_rate': '变化率', 'status': '状态',
    'start': '起始值', 'end': '结束值', 'minimum': '最低值', 'maximum': '最高值', 'point_count': '样本数',
    'base_label': '基期标签', 'current_label': '本期标签', 'share': '占比', 'rank': '名次',
    'cumulative_share': '累计占比', 'contribution': '增减贡献', 'absolute_contribution_share': '绝对贡献份额',
    'contribution_pct_of_total_delta': '占总体净变化贡献率', 'residual': '未解释残差',
    'positive_change': '拉升合计', 'negative_change': '拖累合计', 'net_change': '净变化', 'offset_amount': '抵消量',
    'score': '检验值', 'detectors': '命中方法', 'reasons': '判定依据', 'forecast': '预测值',
    'selected_model': '选定模型', 'forecast_period': '预测期间', 'forecast_quality': '预测适用性',
    'history_start': '历史起点', 'history_end': '历史终点', 'history_points': '历史期数',
    'forecast_horizon': '预测步数', 'residual_reference_interval': '回测残差参考区间',
    'validation_mae': '验证MAE', 'validation_mape': '验证MAPE', 'validation_points': '验证期数',
    'model': '候选模型', 'train_points': '训练期数', 'validation_index': '验证点序号（从0开始）',
    'actual': '实际值', 'predicted': '预测值', 'error': '实际值减预测值', 'absolute_error': '绝对误差',
    'mae': '平均绝对误差MAE', 'mape': '平均绝对百分比误差MAPE', 'rmse': '均方根误差RMSE',
    'mean_error': '平均偏差', 'direction': '方向', 'start_label': '区间起点', 'end_label': '区间终点',
    'period_count': '连续变动次数', 'type': '类型', 'top1_share': '第一名占比', 'top3_share': '前三名占比',
    'hhi_0_to_1': '集中度HHI（0至1）', 'effective_group_count': '等效分组数', 'entropy_nats': '分布熵',
    'groups_to_reach_80_percent': '达到80%所需分组数', 'share_sum': '占比合计',
    'robust_slope_per_period': '每期稳健斜率', 'normalized_slope': '归一化斜率',
    'neutral_threshold': '方向中性阈值', 'direction_consistency': '方向一致率',
    'coefficient_of_variation': '变异系数', 'is_complete': '时间序列完整', 'gaps': '缺期',
    'grain': '粒度', 'regular': '时间轴连续', 'tolerance': '对账容差', 'residual_pct': '残差比例',
}
PERCENT_FIELDS = {'change_rate', 'share', 'cumulative_share', 'absolute_contribution_share',
                  'contribution_pct_of_total_delta', 'validation_mape', 'mape', 'top1_share',
                  'top3_share', 'share_sum', 'direction_consistency', 'residual_pct', 'base_share', 'current_share'}


def capability_text(report, rows=None):
    """Render executed facts; bounded pages use the existing progress message."""
    status = {'COMPLETED': '已完成', 'NOT_COMPUTABLE': '不可计算', 'NOT_APPLICABLE': '不适用'}.get(report['status'], report['status'])
    text = f"{report['name']}｜{status}｜方法：{report['method']}。{report['summary']}"
    if report.get('reason'):
        text += '\n原因：' + report['reason']
    data = report['rows'] if rows is None else rows
    if data:
        cols = report['columns']
        def cell(value, key):
            if value is None:
                return '不可计算／不适用'
            if isinstance(value, float):
                if key in PERCENT_FIELDS:
                    return f'{value:.4%}'
                if key == 'share_change':
                    return f'{value*100:+.4f}个百分点'
                return f'{value:.8g}'
            return str(value).replace('|', '／').replace('\n', ' ')
        text += '\n|' + '|'.join(FIELD_LABELS.get(col, col) for col in cols) + '|\n|' + '|'.join('---' for _ in cols) + '|\n'
        text += '\n'.join('|' + '|'.join(cell(row.get(col), col) for col in cols) + '|' for row in data)
    return text
