"""MySQL expressions use the same grain in SELECT and GROUP BY."""
import pytest
from sql_translator_prod import SQLTranslatorProd
from test_sql_translator_hardening import translator


@pytest.mark.parametrize('grain,expr', [
    ('second', "DATE_FORMAT(order_info.pay_time, '%Y-%m-%d %H:%i:%s')"),
    ('minute', "DATE_FORMAT(order_info.pay_time, '%Y-%m-%d %H:%i:00')"),
    ('hour', "DATE_FORMAT(order_info.pay_time, '%Y-%m-%d %H:00:00')"),
    ('day', 'DATE(order_info.pay_time)'),
    ('week', "DATE_FORMAT(order_info.pay_time, '%x-W%v')"),
    ('month', "DATE_FORMAT(order_info.pay_time, '%Y-%m')"),
    ('quarter', "CONCAT(YEAR(order_info.pay_time), '-Q', QUARTER(order_info.pay_time))"),
    ('half_year', "CONCAT(YEAR(order_info.pay_time), '-H', IF(MONTH(order_info.pay_time) <= 6, 1, 2))"),
    ('year', 'YEAR(order_info.pay_time)'),
])
def test_every_grain_select_and_group_match(grain, expr):
    tr = translator()
    dims = [{'name': 'order_info.pay_time', 'granularity': grain}]
    assert tr._apply_time_granularity('order_info.pay_time', grain) == expr
    assert expr in tr._build_select_clause([], dims, 'ent_order', '81')
    assert tr._build_group_by_clause(dims, 'ent_order', '81') == 'GROUP BY ' + expr


@pytest.mark.parametrize('start,end,upper', [
    ('2025-08-01', '2025-08-31', '2025-09-01'),
    ('2024-02-01', '2024-02-29', '2024-03-01'),
    ('2026-10-13 12:00:00', '2026-10-13 12:59:59', '2026-10-13 13:00:00'),
    ('2026-10-13 12:30:00', '2026-10-13 12:30:59', '2026-10-13 12:31:00'),
    ('2026-10-13 12:30:45', '2026-10-13 12:30:45', '2026-10-13 12:30:46'),
    ('2026-12-31 23:59:59', '2026-12-31 23:59:59', '2027-01-01 00:00:00'),
    ('2026-10-13T12:30:45', '2026-10-13T12:30:45', '2026-10-13 12:30:46'),
])
def test_closed_precision_end_becomes_correct_exclusive_sql_upper(start, end, upper):
    result = translator()._parse_time_context({'type': 'range', 'start': start, 'end': end, 'anchor': 'order_info.pay_time'}, 'order_info')
    assert result == f"order_info.pay_time >= '{start.replace('T', ' ')}' AND order_info.pay_time < '{upper}'"


@pytest.mark.parametrize('start,end', [
    ('2026-10-13', '2026-10-13 12:30:45'),
    ('2026-10-13 12:30:46', '2026-10-13 12:30:45'),
    ('2025-02-29', '2025-03-01'),
    ('2026-10-13T12:30:45Z', '2026-10-13T12:30:45Z'),
    ('2026-10-13 12:30:45.12', '2026-10-13 12:30:45.12'),
    ("2026-10-13' OR 1=1 --", '2026-10-14'),
])
def test_invalid_precision_ranges_fail_closed(start, end):
    with pytest.raises(ValueError):
        translator()._parse_time_context({'type': 'range', 'start': start, 'end': end}, 'order_info')
