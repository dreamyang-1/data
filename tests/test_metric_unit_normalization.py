"""结构化提取按语义目录单位归一：金额数量级与时间型指标单位。"""
from datetime import date, timedelta

import pytest

from app.analysis.chain_tools.temporal_window import (
    _rolling_window_spec,
    resolve_target_window,
)
from app.planning.metric_thresholds import (
    normalize_metric_units,
    numeric_threshold,
)


class TestNumericThresholdUnits:
    def test_amount_magnitude_converts_to_base_unit(self) -> None:
        # 含税销售总额目录单位为元
        assert numeric_threshold('100万', '元') == 1_000_000
        assert numeric_threshold('100万元', '元') == 1_000_000
        assert numeric_threshold('1.5亿', '元') == 150_000_000
        assert numeric_threshold('3千', '元') == 3_000
        assert numeric_threshold('2千万', '元') == 20_000_000

    def test_amount_already_in_base_unit_untouched(self) -> None:
        assert numeric_threshold('1000000', '元') == 1_000_000
        assert numeric_threshold(1000000, '元') == 1_000_000

    def test_time_unit_threshold_converts(self) -> None:
        # 合作时长目录单位为天
        assert numeric_threshold('3个月', '天') == 90
        assert numeric_threshold('6个月', '天') == 180
        assert numeric_threshold('1年', '天') == 365

    def test_unregistered_unit_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            numeric_threshold('100万', '')


class TestNormalizeMetricUnits:
    def test_amount_filter_value_normalized(self) -> None:
        structured = {
            '指标': [{'name': '含税销售总额'}],
            '过滤条件': [{
                'entity': None, 'field': '含税销售总额', 'op': '>',
                'value': ['100万'],
            }],
            '时间粒度': {'unit': None, 'time_range': None, 'anchor': None},
        }
        normalize_metric_units(structured, {'含税销售总额': '元'})
        assert structured['过滤条件'][0]['value'] == [1_000_000]

    def test_non_metric_filter_untouched(self) -> None:
        structured = {
            '指标': [],
            '过滤条件': [{
                'entity': '经销商', 'field': '省份', 'op': '=',
                'value': ['上海'],
            }],
            '时间粒度': None,
        }
        normalize_metric_units(structured, {'含税销售总额': '元'})
        assert structured['过滤条件'][0]['value'] == ['上海']

    def test_relative_window_follows_metric_unit(self) -> None:
        structured = {
            '指标': [{'name': '合作时长'}],
            '过滤条件': [],
            '时间粒度': {'unit': '天', 'time_range': '近三个月', 'anchor': None},
        }
        normalize_metric_units(structured, {'合作时长': '天'})
        assert structured['时间粒度']['time_range'] == '近90天'

    def test_relative_point_follows_metric_unit(self) -> None:
        structured = {
            '指标': [{'name': '合作时长'}],
            '过滤条件': [],
            '时间粒度': {'unit': '天', 'time_range': '六个月前', 'anchor': None},
        }
        normalize_metric_units(structured, {'合作时长': '天'})
        assert structured['时间粒度']['time_range'] == '180天之前'

    def test_same_unit_is_not_rewritten(self) -> None:
        structured = {
            '指标': [{'name': '合作时长'}],
            '过滤条件': [],
            '时间粒度': {'unit': '月', 'time_range': '近三个月', 'anchor': None},
        }
        normalize_metric_units(structured, {'合作时长': '月'})
        assert structured['时间粒度']['time_range'] == '近三个月'

    def test_non_time_metric_keeps_time_text(self) -> None:
        structured = {
            '指标': [{'name': '含税销售总额'}],
            '过滤条件': [],
            '时间粒度': {'unit': '月', 'time_range': '近三个月', 'anchor': None},
        }
        normalize_metric_units(structured, {'含税销售总额': '元'})
        assert structured['时间粒度']['time_range'] == '近三个月'

    def test_absolute_period_is_not_rewritten(self) -> None:
        structured = {
            '指标': [{'name': '合作时长'}],
            '过滤条件': [],
            '时间粒度': {
                'unit': '月', 'time_range': '2025年10月至12月', 'anchor': None,
            },
        }
        normalize_metric_units(structured, {'合作时长': '天'})
        assert structured['时间粒度']['time_range'] == '2025年10月至12月'


class TestDayRollingWindow:
    def test_day_rolling_token_parsed(self) -> None:
        assert _rolling_window_spec('近90天') == (90, 'day')
        assert _rolling_window_spec('最近3日') == (3, 'day')

    def test_day_window_resolved(self) -> None:
        today = date(2026, 10, 26)
        assert resolve_target_window('近90天', 'day', today=today) == (
            today - timedelta(days=90), today,
        )
