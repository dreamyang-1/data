"""Declared range precision and grouping grain remain independent contracts."""
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace as Obj

import pytest
from structured_binding import bind
from structured_time import UNITS, bounds, grouping_grain, normalize_unit, validate_boundaries
from test_structured_binding import fixture

CLOCK = datetime(2026, 10, 13, 12, 30, 45)


@pytest.mark.parametrize('value,start,end', [
    ('2025年8月份', '2025-08-01', '2025-08-31'),
    ('2024年2月份', '2024-02-01', '2024-02-29'),
    ('2026年度', '2026-01-01', '2026-12-31'),
    ('今年8月份', '2026-08-01', '2026-08-31'),
    ('去年8月份', '2025-08-01', '2025-08-31'),
    ('2026年8月1号', '2026-08-01', '2026-08-01'),
    ('2026H1', '2026-01-01', '2026-06-30'),
    ('2026-H2', '2026-07-01', '2026-12-31'),
    ('本半年', '2026-07-01', '2026-12-31'),
    ('上半年', '2026-01-01', '2026-06-30'),
    ('下半年', '2026-07-01', '2026-12-31'),
    ('上一半年', '2026-01-01', '2026-06-30'),
    ('下一半年', '2027-01-01', '2027-06-30'),
    ('2026年第1周', '2025-12-29', '2026-01-04'),
    ('2020-W53', '2020-12-28', '2021-01-03'),
    ('明天', '2026-10-14', '2026-10-14'),
    ('前天', '2026-10-11', '2026-10-11'),
    ('2026-10-13 12', '2026-10-13 12:00:00', '2026-10-13 12:59:59'),
    ('2026年10月13日12时', '2026-10-13 12:00:00', '2026-10-13 12:59:59'),
    ('2026-10-13T12:30', '2026-10-13 12:30:00', '2026-10-13 12:30:59'),
    ('2026年10月份13日12时30分', '2026-10-13 12:30:00', '2026-10-13 12:30:59'),
    ('2026年10月13日12时30分45秒', '2026-10-13 12:30:45', '2026-10-13 12:30:45'),
    ('今天12时30分', '2026-10-13 12:30:00', '2026-10-13 12:30:59'),
    ('本小时', '2026-10-13 12:00:00', '2026-10-13 12:59:59'),
    ('上一小时', '2026-10-13 11:00:00', '2026-10-13 11:59:59'),
    ('本分钟', '2026-10-13 12:30:00', '2026-10-13 12:30:59'),
    ('上一分钟', '2026-10-13 12:29:00', '2026-10-13 12:29:59'),
    ('当前秒', '2026-10-13 12:30:45', '2026-10-13 12:30:45'),
    ('last_second', '2026-10-13 12:30:44', '2026-10-13 12:30:44'),
    ('下个分钟', '2026-10-13 12:31:00', '2026-10-13 12:31:59'),
    ('最近两个小时', '2026-10-13 10:30:45', '2026-10-13 12:30:44'),
    ('近120分钟', '2026-10-13 10:30:45', '2026-10-13 12:30:44'),
    ('过去三十秒', '2026-10-13 12:30:15', '2026-10-13 12:30:44'),
    (['2026-10-13', '2026-10-13 12:30'], '2026-10-13 00:00:00', '2026-10-13 12:30:59'),
    ({'start': '2026-10-13 12:30', 'end': '2026-10-13'}, '2026-10-13 12:30:00', '2026-10-13 23:59:59'),
    ({'start': '2026-10-13', 'end_exclusive': '2026-10-13 12:30'}, '2026-10-13 00:00:00', '2026-10-13 12:29:59'),
    ({'start': '2026-10-13 23:59:59', 'end_exclusive': '2026-10-14'}, '2026-10-13 23:59:59', '2026-10-13 23:59:59'),
    ('2026-10-13 12:30:00至2026-10-13 12:30:45', '2026-10-13 12:30:00', '2026-10-13 12:30:45'),
])
def test_period_precision(value, start, end):
    assert bounds(value, CLOCK) == (start, end)
    validate_boundaries(start, end)


@pytest.mark.parametrize('label,canonical', list(UNITS.items()))
def test_all_declared_grain_aliases(label, canonical):
    catalog = {'dimensions': {}, 'fields': {'event.time': {'data_type': 'DATETIME'}}}
    assert normalize_unit(label.upper()) == canonical
    assert grouping_grain({'unit': label}, {}, 'event.time', 'dimensions', {'metrics': ['count']}, catalog) == canonical


@pytest.mark.parametrize('value', ['2026年13月份', '2025年2月29日', '2026-W54',
    '2026-10-13 24:00', '2026-10-13 12:60', '2026-10-13 12:30:60',
    '2026-10-13T12:30:45Z', '2026-10-13 12:30:45+08:00', '2026-10-13 12:30:45.123',
    '2026财年', '最近3个工作日', '近0秒', '最近4000分钟',
    ['2026-10-13 12:31', '2026-10-13 12:30'],
    {'start': '2026-10-13 12:30:00', 'end_exclusive': '2026-10-13 12:30:00'}])
def test_invalid_or_business_dependent_range_is_not_guessed(value):
    with pytest.raises((ValueError, OverflowError)):
        bounds(value, CLOCK)


@pytest.mark.parametrize('clock', [date(2026, 10, 13), CLOCK.replace(tzinfo=timezone.utc)])
def test_fine_relative_range_requires_real_business_clock(clock):
    with pytest.raises(ValueError):
        bounds('最近5分钟', clock)


@pytest.mark.parametrize('grain', sorted(set(UNITS.values())))
@pytest.mark.parametrize('dimensions', [False, True])
def test_binding_preserves_range_grouping_separation_and_real_asl_validator(grain, dimensions):
    from agent import _validate_asl_output
    e, k, p = fixture()
    e['时间粒度'] = {'unit': grain, 'time_range': '2025年8月份', 'anchor': '日期'}
    if dimensions:
        e['维度'] = ['日期']
        p['dimensions'] = [{'index': 0, 'key': 'hospital.date', 'granularity': 'nonsense'}]
    p['time'] = {'start': '1900-01-01', 'end': '1900-01-02', 'anchor': 'not.authorized'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=CLOCK)
    assert not ast['ambiguity']
    assert ast['time_context'] == {'type': 'range', 'start': '2025-08-01', 'end': '2025-08-31', 'unit': 'day', 'anchor': 'hospital.date'}
    assert [d['granularity'] for d in ast['dimensions']] == ([grain] if dimensions else [])
    _validate_asl_output(json.dumps(ast), k)


@pytest.mark.parametrize('grain,period', [('hour', '本小时'), ('minute', '近5分钟'), ('second', '2026-10-13 12:30:45')])
def test_subday_binding_keeps_precision_anchor_and_does_not_invent_date_time(grain, period):
    from agent import _validate_asl_output
    e, k, p = fixture()
    e['时间粒度'] = {'unit': grain, 'time_range': period, 'anchor': '日期'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=CLOCK)
    assert not ast['ambiguity'] and ast['time_context']['unit'] == 'second'
    _validate_asl_output(json.dumps(ast), k)
    k['entities'][0].metadata['attributes'][-1]['data_type'] = 'DATE'
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=CLOCK)
    assert ast['ambiguity'] and ast['time_context'] is None
    assert 'hospital.date' in ast['ambiguity'][0]['question'] and 'DATE' in ast['ambiguity'][0]['question']


def test_explicit_shipping_anchor_survives_other_available_dates():
    e, k, p = fixture()
    attrs = k['entities'][0].metadata['attributes']
    attrs[-1]['attr_name'] = '发货日期'
    attrs.append({'attr_name': '开票日期', 'field_mapping': 'hospital.invoiced_date'})
    k['_vector_authorized_fields'].append('hospital.invoiced_date')
    e['时间粒度'] = {'unit': '年', 'time_range': '2026年', 'anchor': '发货日期'}
    p['time'] = {'anchor': 'hospital.invoiced_date'}
    ast, _ = bind(e, k, Obj(invoke=lambda _: Obj(content=json.dumps(p))), today=CLOCK)
    assert not ast['ambiguity'] and ast['time_context']['anchor'] == 'hospital.date'


@pytest.mark.parametrize('grain', ['hour', 'minute', 'second'])
def test_date_only_field_cannot_acquire_subday_grouping(grain):
    catalog = {'dimensions': {}, 'fields': {'event.date': {'data_type': 'DATE'}}}
    with pytest.raises(ValueError, match='只有日期精度'):
        grouping_grain({'unit': grain}, {}, 'event.date', 'dimensions', {'metrics': ['count']}, catalog)


@pytest.mark.parametrize('clock,period,start,end', [
    (datetime(2026, 1, 1), '上一小时', '2025-12-31 23:00:00', '2025-12-31 23:59:59'),
    (datetime(2026, 1, 1), '上一分钟', '2025-12-31 23:59:00', '2025-12-31 23:59:59'),
    (datetime(2026, 1, 1), '上一秒', '2025-12-31 23:59:59', '2025-12-31 23:59:59'),
    (datetime(2024, 3, 1), '近1小时', '2024-02-29 23:00:00', '2024-02-29 23:59:59'),
])
def test_subday_period_crosses_date_year_and_leap_boundaries(clock, period, start, end):
    assert bounds(period, clock) == (start, end)


def test_named_halves_are_not_confused_with_previous_or_next_period():
    clock = date(2026, 4, 1)
    assert bounds('上半年', clock) == ('2026-01-01', '2026-06-30')
    assert bounds('下半年', clock) == ('2026-07-01', '2026-12-31')
    assert bounds('上一半年', clock) == ('2025-07-01', '2025-12-31')
