from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner


def compound(name='rate'):
    return {'指标': [{'name': name}], '维度': ['经销商'], '过滤条件': [],
            '是否查询数据库': '是', '时间粒度': {'time_range': '2025年'}}


def test_expansion_remaps_incoming_and_outgoing_dependencies_and_handoff():
    data = QuestionDecomposition(tasks=(
        DecomposedTask(question='先查询入选对象'),
        DecomposedTask(question='查询其复合指标', depends_on=(0,), structured=compound(), parameters=('old-rate',)),
        DecomposedTask(question='查询该结果涉及的医院', depends_on=(1,))))
    result = SingleQuestionPlanner._split_compound_metric_tasks(data, [('rate', ('numerator', 'denominator'))])
    assert len(result.tasks) == 5
    assert [task.depends_on for task in result.tasks] == [(), (0,), (0,), (1, 2), (3,)]
    for index, metric in [(1, 'numerator'), (2, 'denominator')]:
        task = result.tasks[index]
        assert task.structured['是否查询数据库'] == '是'
        assert task.question.startswith('根据上述任务的查询结果')
        assert metric in ' '.join(task.parameters) and 'old-rate' not in task.parameters
    assert result.tasks[3].structured['是否查询数据库'] == '否'
    assert data.tasks[1].structured['指标'][0]['name'] == 'rate'


def test_multiple_expansions_do_not_bind_to_old_task_offsets():
    data = QuestionDecomposition(tasks=(
        DecomposedTask(question='查询复合指标甲', structured=compound('a')),
        DecomposedTask(question='根据甲查询复合指标乙', depends_on=(0,), structured=compound('b'))))
    result = SingleQuestionPlanner._split_compound_metric_tasks(data,
        [('a', ('a_num', 'a_den')), ('b', ('b_num', 'b_den'))])
    assert [task.depends_on for task in result.tasks] == [(), (), (0, 1), (2,), (2,), (3, 4)]


def test_declared_local_compound_result_is_not_split_into_new_queries():
    structured = compound();structured['是否查询数据库'] = '否'
    data = QuestionDecomposition(tasks=(DecomposedTask(question='使用已有结果计算覆盖率', structured=structured),),
        metric_catalog=(frozenset({'rate','numerator','denominator'}), {}),
        metric_dependencies={'rate': ('numerator', 'denominator')})
    assert SingleQuestionPlanner._unsplit_compound_metrics(data) == []
