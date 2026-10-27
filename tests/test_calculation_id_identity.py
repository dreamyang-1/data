"""已执行查询的同一物理 id 可对齐计算，不凭展示名或行位置猜测。"""
from types import SimpleNamespace

import pytest

from app.services.computation_delivery import aligned_input, has_group_identity, identifier_field


def reference(identity_label, metric, *, physical="salesperson.id"):
    return SimpleNamespace(
        columns=(identity_label, "销售员姓名", metric),
        transformation_log=({"type": "query_provenance", "column_bindings": {
            identity_label: physical,
            "销售员姓名": "salesperson.salesperson_name",
        }},),
    )


@pytest.mark.parametrize("physical", [
    "id", "salesperson.id", "schema.salesperson.ID", '`salesperson`.`id`',
    '"salesperson"."ID"', '[salesperson].[id]', ' salesperson.id ',
    "salesperson.salesperson_code", "salesperson.person_id",
])
def test_physical_identifier_supports_exact_id_leaf(physical):
    assert identifier_field(physical)


@pytest.mark.parametrize("physical", [
    None, 1, "业务员 ID", "grid", "salesperson.identity", "SUM(salesperson.id)",
    "salesperson.id_amount", "salesperson.salesperson_name",
])
def test_identifier_does_not_guess_display_name_or_expression(physical):
    assert not identifier_field(physical)


@pytest.mark.parametrize("right_alias", ["业务员 ID", "人员身份"])
def test_same_physical_id_aligns_reordered_same_name_people(right_alias):
    left = reference("业务员 ID", "含税销售总额")
    right = reference(right_alias, "订单笔数")
    assert has_group_identity(left)
    columns, rows = aligned_input([
        (left, [
            {"业务员 ID": 1, "销售员姓名": "同名", "含税销售总额": 60},
            {"业务员 ID": 2, "销售员姓名": "同名", "含税销售总额": 30},
        ]),
        (right, [
            {right_alias: 2, "销售员姓名": "同名", "订单笔数": 3},
            {right_alias: 1, "销售员姓名": "同名", "订单笔数": 2},
        ]),
    ])
    assert columns == ["业务员 ID", "销售员姓名", "含税销售总额", "订单笔数"]
    assert [(row["业务员 ID"], row["含税销售总额"] / row["订单笔数"]) for row in rows] == [(1, 30), (2, 10)]


def test_identical_id_display_values_from_different_tables_are_not_aligned():
    left = reference("对象身份", "金额", physical="salesperson.id")
    right = reference("对象身份", "笔数", physical="company.id")
    with pytest.raises(ValueError, match="缺少共同的唯一编号"):
        aligned_input([
            (left, [{"对象身份": n, "销售员姓名": "同名", "金额": 10} for n in (1, 2)]),
            (right, [{"对象身份": n, "销售员姓名": "同名", "笔数": 1} for n in (1, 2)]),
        ])


@pytest.mark.parametrize('row_count', [1, 2])
@pytest.mark.parametrize('left_field,right_field', [
    ('salesperson.id', 'salesperson.salesperson_code'),
    ('salesperson.salesperson_code', 'company.company_code'),
    ('salesperson.salesperson_code', None),
    (None, None),
])
def test_code_label_and_equal_values_never_override_different_field_bindings(row_count, left_field, right_field):
    left = reference('人员编码', '金额', physical=left_field)
    right = reference('人员编码', '笔数', physical=right_field)
    with pytest.raises(ValueError, match='缺少共同的唯一编号'):
        aligned_input([
            (left, [{'人员编码': n, '销售员姓名': '同名', '金额': 10} for n in range(row_count)]),
            (right, [{'人员编码': n, '销售员姓名': '同名', '笔数': 1} for n in range(row_count)]),
        ])


@pytest.mark.parametrize("ids", [(1, 1), (1, None), (1, 3)])
def test_id_support_keeps_uniqueness_nonnull_and_same_cohort_checks(ids):
    left = reference("业务员 ID", "金额")
    right = reference("业务员 ID", "笔数")
    with pytest.raises(ValueError, match="唯一编号分组|对象范围不一致"):
        aligned_input([
            (left, [{"业务员 ID": n, "销售员姓名": "同名", "金额": 10} for n in (1, 2)]),
            (right, [{"业务员 ID": n, "销售员姓名": "同名", "笔数": 1} for n in ids]),
        ])


@pytest.mark.asyncio
@pytest.mark.parametrize('physical', ['salesperson.id', 'salesperson.salesperson_code'])
async def test_ranked_calculation_keeps_identity_for_dependent_company_lookup(physical):
    from app.domain.models import AtomicTask
    from test_complex_execution_correctness import service, context, persist, IDENTITY

    agent = service()
    chat, _ = context()
    chat.question = '给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司'
    amounts = [{'业务员 ID': i, '销售员姓名': '同名', '含税销售总额': i * i * 100}
               for i in range(1, 9)]
    orders = [{'人员身份': i, '销售员姓名': '同名', '订单笔数': i}
              for i in range(8, 0, -1)]
    for conversation, rows, label in [('amount', amounts, '业务员 ID'), ('orders', orders, '人员身份')]:
        await persist(agent, chat, list(rows[0]), rows, conversation=conversation,
                      column_bindings={label: physical, '销售员姓名': 'salesperson.salesperson_name'})
    task = AtomicTask(task_id='task-3', question='计算2025年各业务员平均每单销售金额，按金额降序取前5名，不再查询数据库',
                      depends_on=['task-1', 'task-2'], extraction={
        '是否查询数据库': '否', '计算绑定': {
            'operation': 'divide', 'result_field': '平均每单销售金额', 'format': 'number',
            'numerator': {'source_task_id': 'task-1', 'column': '含税销售总额'},
            'denominator': {'source_task_id': 'task-2', 'column': '订单笔数'}},
        '排序': [{'field': '平均每单销售金额', 'order': 'desc'}], '限制': 5})
    result = await agent._respond_pure_computation(chat, IDENTITY, task,
                                                {'task-1': 'amount', 'task-2': 'orders'})
    assert result.status == 'COMPLETED'
    preview = result.evidence[0].payload['preview']
    assert [(row['业务员 ID'], float(row['平均每单销售金额'])) for row in preview] == [
        (8, 800), (7, 700), (6, 600), (5, 500), (4, 400)]
    output = await agent._latest_task_dataset_reference(
        IDENTITY, chat, chat.conversation_id, dataset_id=result.dataset_id)
    provenance = next(item for item in output['transformation_log']
                      if item.get('type') == 'query_provenance')
    assert provenance['column_bindings']['业务员 ID'] == physical
