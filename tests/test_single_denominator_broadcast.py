from types import SimpleNamespace

import pytest

from app.services.calculation_execution import _arithmetic


def source(dataset, column):
    return SimpleNamespace(dataset_id=dataset, columns=('province_id', column),
        transformation_log=({'type': 'query_provenance',
            'column_bindings': {'province_id': 'dim_province.province_id'}},))


def binding(operator='divide'):
    return {'operation': operator,
        'left': {'source_task_id': 'a', 'column': 'covered'},
        'right': {'source_task_id': 'b', 'column': 'total'},
        'result_field': 'result', 'format': 'number'}


@pytest.mark.parametrize('count', [1, 31])
def test_single_denominator_applies_despite_different_region_keys(count):
    inputs = {'a': (source('a', 'covered'), [
        {'province_id': str(i), 'covered': i+1} for i in range(count)]),
        'b': (source('b', 'total'), [{'province_id': '310000', 'total': 100}])}
    columns, rows, operation, evidence = _arithmetic(binding(), inputs)
    assert len(rows) == count
    assert [float(row['result']) for row in rows] == [(i+1)/100 for i in range(count)]
    assert [row['province_id'] for row in rows] == [str(i) for i in range(count)]
    assert evidence['right']['dataset_id'] == 'b'


def test_non_division_still_uses_identity_alignment():
    inputs = {'a': (source('a', 'covered'), [{'province_id': '1', 'covered': 5}]),
        'b': (source('b', 'total'), [{'province_id': '2', 'total': 10}])}
    with pytest.raises(ValueError, match='对象范围不一致'):
        _arithmetic(binding('add'), inputs)
