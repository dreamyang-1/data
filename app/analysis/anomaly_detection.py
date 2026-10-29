"""Independent, auditable detectors. Missing observations are never zero-filled."""
import math
import statistics as st
from app.analysis.forecasting import assess_time_axis


def detect(labels, values, *, selected=None):
    median = st.median(values)
    mad = st.median(abs(v - median) for v in values)
    mean, sigma = st.mean(values), st.pstdev(values)
    q1, _, q3 = st.quantiles(values, n=4, method='inclusive')
    iqr = q3 - q1
    detectors = []
    hits = {}

    def register(name, params, observations, reason=''):
        requested = 'MAD' if name == 'MEDIAN_RELATIVE_DEVIATION' else name
        if selected is not None and requested not in selected:
            return
        matches = []
        for label, value, score, matched in observations():
            if matched:
                item = {'label': label, 'value': value, 'score': score}
                matches.append(item)
                hits.setdefault(label, {'label': label, 'value': value, 'detectors': [], 'reasons': []})
                hits[label]['detectors'].append(name)
                hits[label]['reasons'].append(f'{name}: score={score:.6g}; {params}')
        detectors.append({'name': name, 'status': 'NOT_COMPUTABLE' if reason else 'COMPLETED',
                          'parameters': params, 'reason': reason, 'hit_count': len(matches), 'matches': matches})

    if mad:
        register('MAD', {'median': median, 'mad': mad, 'absolute_robust_z_threshold': 3.5},
                 lambda: [(k, v, .6745 * (v - median) / mad, abs(.6745 * (v - median) / mad) >= 3.5)
                  for k, v in zip(labels, values)])
    else:
        floor = max(abs(median) * .5, 1e-9)
        register('MAD', {'median': median, 'mad': 0}, lambda: [], 'MAD为0，标准稳健Z分数不可计算；另执行相对偏差规则')
        register('MEDIAN_RELATIVE_DEVIATION', {'median': median, 'absolute_deviation_threshold': floor},
                 lambda: [(k, v, abs(v - median), abs(v - median) >= floor) for k, v in zip(labels, values)])
    register('IQR', {'q1': q1, 'q3': q3, 'iqr': iqr, 'lower': q1 - 1.5 * iqr, 'upper': q3 + 1.5 * iqr},
             lambda: [(k, v, v, v < q1 - 1.5 * iqr or v > q3 + 1.5 * iqr) for k, v in zip(labels, values)] if iqr else [],
             '' if iqr else 'IQR为0，四分位围栏退化；不能把任意微小差异当作异常')
    register('Z_SCORE', {'mean': mean, 'population_std': sigma, 'absolute_z_threshold': 3},
             lambda: [(k, v, (v - mean) / sigma, abs((v - mean) / sigma) >= 3) for k, v in zip(labels, values)] if sigma else [],
             '' if sigma else '标准差为0，Z分数不可计算')
    grain, regular = assess_time_axis(labels)
    deltas = [b - a for a, b in zip(values, values[1:])]
    center = st.median(deltas)
    dispersion = st.median(abs(v - center) for v in deltas)
    floor = max(st.median(abs(v) for v in values) * .5, 1e-9)
    threshold = max(3.5 * dispersion / .6745, floor)
    register('ADJACENT_JUMP', {'median_delta': center, 'delta_mad': dispersion, 'absolute_delta_deviation_threshold': threshold},
             lambda: [(labels[i+1], values[i+1], d - center, abs(d - center) >= threshold) for i, d in enumerate(deltas)]
             if regular and len(deltas) >= 5 else [],
             '' if regular and len(deltas) >= 5 else '需至少6个连续、同粒度时间点；缺期不跨期判定单期跳变')
    return {'median': median, 'mad': mad, 'point_count': len(values),
            'time_axis': {'grain': grain, 'regular': regular}, 'detectors': detectors,
            'candidate_count': len(hits), 'candidates': list(hits.values()),
            'decision_rule': '任一已完成检测器命中即列为候选；多方法命中数不是异常概率',
            'causality_established': False}
