import sqlite3

import pytest
from test_sql_translator_hardening import translator


@pytest.mark.parametrize('formula', ['cooperation=TIMESTAMPDIFF(MONTH,dealer.first_order_date,CURRENT_DATE)',
    'TIMESTAMPDIFF(MONTH, dealer.first_order_date, CURRENT_DATE)'])
def test_registered_scalar_metric_having_uses_its_selected_alias(formula):
    value = translator()
    value._get_metric = lambda *args: {'calculation_rule': {'calc_formula': formula}}
    metrics = [{'name': 'cooperation', 'alias': '合作时长'}]
    conditions = ['TIMESTAMPDIFF(MONTH, dealer.first_order_date, CURRENT_DATE) > 12']
    assert value._projected_metric_having(conditions, metrics, '81') == ['`合作时长` > 12']
    assert conditions[0].startswith('TIMESTAMPDIFF')


def test_projected_having_is_executable_and_preserves_threshold():
    value = translator()
    value._get_metric = lambda *args: {'calculation_rule': {'calc_formula': 'elapsed=dealer.months'}}
    having = value._projected_metric_having(['dealer.months > 12'], [{'name': 'elapsed', 'alias': '合作月数'}])
    sql = 'SELECT dealer.name, dealer.months AS `合作月数` FROM dealer GROUP BY dealer.name ' + value._build_having_clause(having)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE dealer (name TEXT, months INTEGER)')
        db.executemany('INSERT INTO dealer VALUES (?,?)', [('甲', 12), ('乙', 13)])
        assert db.execute(sql).fetchall() == [('乙', 13)]


def test_unrelated_having_and_non_mysql_dialects_are_preserved():
    import sql_dialects
    value = translator()
    value._get_metric = lambda *args: {'calculation_rule': {'calc_formula': 'x=SUM(dealer.amount)'}}
    metrics = [{'name': 'x', 'alias': '销售额'}]
    assert value._projected_metric_having(['COUNT(dealer.id)>2'], metrics) == ['COUNT(dealer.id)>2']
    condition = ['SUM(dealer.amount)>12']
    assert value._projected_metric_having(condition, metrics, dialect=sql_dialects.PostgreSqlDialect()) == condition
