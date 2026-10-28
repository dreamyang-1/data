"""structured_time 区间解析的年份继承与既有行为回归。"""
from datetime import date

import pytest

from structured_time import bounds


TODAY = date(2026, 10, 19)


def test_range_tail_without_year_inherits_head_year():
    assert bounds('2025年10月到12月', TODAY) == ('2025-10-01', '2025-12-31')
    assert bounds('2025年10月至12月', TODAY) == ('2025-10-01', '2025-12-31')


def test_range_tail_with_full_date_inherits_head_year():
    assert bounds('2025年10月20日到12月15日', TODAY) == ('2025-10-20', '2025-12-15')


def test_range_tail_quarter_inherits_head_year():
    assert bounds('2025年10月到第4季度', TODAY) == ('2025-10-01', '2025-12-31')


def test_both_sides_with_year_unchanged():
    assert bounds('2025年10月到2025年12月', TODAY) == ('2025-10-01', '2025-12-31')


def test_tail_with_own_year_not_overwritten():
    assert bounds('2025年10月到2026年1月', TODAY) == ('2025-10-01', '2026-01-31')


def test_no_year_on_either_side_still_rejected():
    # 两端都没有年份时不猜测，保持原有的明确日期要求
    with pytest.raises(ValueError):
        bounds('10月到12月', TODAY)
    with pytest.raises(ValueError):
        bounds('11月和12月', TODAY)


def test_single_period_unchanged():
    assert bounds('2025年10月', TODAY) == ('2025-10-01', '2025-10-31')
    assert bounds('2025年', TODAY) == ('2025-01-01', '2025-12-31')
