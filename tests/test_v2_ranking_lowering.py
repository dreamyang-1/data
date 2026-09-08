"""Execute generated SQL only against synthetic, private in-memory rows."""
import sqlite3

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.asl2 import ASL2Unsupported, prove_asl2_result
from test_v2_asl2_lowering import provider, current, bind, args, sql, payload


def ranked(session, direction='DESC', nulls='EXCLUDE', limit=2, tiebreaker=True):
    measure = bind(session, 'METRIC', 'total', 'MEASURE')
    group = bind(session, 'DIMENSION', 'customer_name', 'GROUP_BY')
    return m.RankingPayload(measures=[measure], group_by=[group], ranking_target=group,
        ranking=m.RankingSpec(rank_by=measure, direction=direction, limit=limit, nulls_policy=nulls,
            stable_tiebreakers=[bind(session, 'DIMENSION', 'customer_name', 'ORDER_BY')] if tiebreaker else []))


def execute_fixture(query, reverse=False):
    rows = [(1, 10, 'A'), (2, 20, 'A'), (3, 20, 'B'), (4, 20, 'C'),
            (5, None, 'D'), (6, 10, 'E'), (7, 100, None)]
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE orders (id INTEGER, amount INTEGER, name TEXT, ordered_at TEXT)')
        db.executemany('INSERT INTO orders(id, amount, name) VALUES (?, ?, ?)', rows[::-1] if reverse else rows)
        cursor = db.execute(query)
        columns = [d[0] for d in cursor.description]
        return columns, [dict(zip(columns, row)) for row in cursor.fetchall()]


@pytest.mark.parametrize('direction,nulls,expected', [
    ('DESC', 'EXCLUDE', ['A', 'B']), ('DESC', 'FIRST', ['D', 'A']), ('DESC', 'LAST', ['A', 'B']),
    ('ASC', 'EXCLUDE', ['E', 'B']), ('ASC', 'FIRST', ['D', 'E']), ('ASC', 'LAST', ['E', 'B'])])
def test_topn_preserves_null_direction_and_stable_ties_in_executed_sql(provider, direction, nulls, expected):
    session = current(provider)
    lowering, result = session.compile_asl2(sql_planner=sql, **args(session, ranked(session, direction, nulls)))
    assert result['success'] and not lowering.can_execute_safely
    assert lowering.implicit_row_cap is None and lowering.asl['limit'] == 2
    columns, rows = execute_fixture(result['sql'])
    group_column = lowering.output_bindings[0].sql_alias
    assert [r[group_column] for r in rows] == expected
    assert execute_fixture(result['sql'], reverse=True) == (columns, rows)
    assert prove_asl2_result(lowering, columns=columns, rows=rows, truncated=False).status == 'PASS'
    assert prove_asl2_result(lowering, columns=columns, rows=rows[::-1], truncated=False).status == 'FAIL'


def test_rank_is_global_aggregate_while_display_limit_is_plain_rows(provider):
    session = current(provider)
    lower, result = session.compile_asl2(sql_planner=sql, **args(session, ranked(session, tiebreaker=False)))
    _, rows = execute_fixture(result['sql'])
    assert [r[lower.output_bindings[0].sql_alias] for r in rows] == [None, 'A']
    session = current(provider); detail = payload(session, 'DETAIL_ROWS'); detail.limit = m.LimitSpec(limit=2)
    lower, result = session.compile_asl2(sql_planner=sql, **args(session, detail))
    _, rows = execute_fixture(result['sql'])
    assert [r[lower.output_bindings[0].sql_alias] for r in rows] == ['A', 'A']
    assert lower.ordering_contract is None


def test_metric_global_filters_apply_before_rank_and_null_exclusion_after_aggregate(provider):
    from test_pinned_catalog import seal
    data = provider[1]
    data['documents'][0]['metrics'][0]['calculation_rule']['global_filters'] = [dict(condition='orders.id != 7')]
    seal(data)
    from test_catalog_publication import embed
    provider[0].publish(81, [205], embed_fn=embed, publication_id='filtered-rank', producer_revision='fixture', embedding_contract='fixture')
    session = current(provider); p = ranked(session, tiebreaker=False)
    p.filters = m.Predicate(field_ref=bind(session,'ATTRIBUTE','name','FILTER_FIELD'), operator='NE',
        value=m.StringValue(value='A'), source='CURRENT_EXPLICIT', scope='ROW')
    lower, result = session.compile_asl2(sql_planner=sql, **args(session, p))
    _, rows = execute_fixture(result['sql'])
    assert {r[lower.output_bindings[0].sql_alias] for r in rows} == {'B', 'C'}
    assert 'WHERE' in result['sql'] and 'HAVING' in result['sql']


@pytest.mark.parametrize('fault', ['omit', 'modify', 'authority', 'scope'])
def test_ranking_requires_policy_receipt_and_original_scope_pin(provider, fault):
    session = current(provider)
    def planner(pin, scope, asl, **kwargs):
        if fault == 'authority':
            from test_pinned_catalog import seal
            provider[1]['documents'][0]['metrics'][0]['calculation_rule']['calc_formula'] = 'MAX(orders.amount)'
            seal(provider[1])
        result = sql(pin, scope, asl, **kwargs)
        if fault == 'omit': result.pop('ordering_contract_hash')
        if fault == 'modify': result['ordering_contract_hash'] = 'modified'
        if fault == 'scope': result['authorized_scope_fingerprint'] = 'other'
        return result
    with pytest.raises(ValueError, match='ASL2_'):
        session.compile_asl2(sql_planner=planner, **args(session, ranked(session)))


@pytest.mark.parametrize('fault', ['target', 'tiebreaker'])
def test_ranking_cannot_add_a_group_and_change_population(provider, fault):
    session = current(provider); p = ranked(session)
    field = bind(session, 'ATTRIBUTE', 'id', 'ORDER_BY')
    if fault == 'target': p.ranking_target = field
    else: p.ranking.stable_tiebreakers = [field]
    lowering, result = session.compile_asl2(sql_planner=lambda *a, **k: pytest.fail('Changed grouping'), **args(session, p))
    assert lowering.status == 'UNSUPPORTED' and result is None
    assert lowering.blockers == (('ASL2_RANK_TARGET_GRAIN_UNPROVEN' if fault == 'target' else 'ASL2_RANK_ORDER_GRAIN_UNPROVEN'),)


def test_private_ranking_policy_is_immutable(provider):
    session = current(provider)
    lowering, _ = session.compile_asl2(sql_planner=sql, **args(session, ranked(session)))
    with pytest.raises(TypeError): lowering.ordering_contract['order_by'][0]['nulls_policy'] = 'FIRST'
    with pytest.raises(TypeError): lowering.ordering_contract['limit'] = 100


def test_ranking_result_cannot_prove_truncated_or_excess_rows(provider):
    session = current(provider)
    lowering, result = session.compile_asl2(sql_planner=sql, **args(session, ranked(session)))
    columns, rows = execute_fixture(result['sql'])
    with pytest.raises(ASL2Unsupported, match='COMPLETENESS'):
        prove_asl2_result(lowering, columns=columns, rows=rows, truncated=True)
    assert prove_asl2_result(lowering, columns=columns, rows=[*rows, rows[-1]], truncated=False).status == 'FAIL'
