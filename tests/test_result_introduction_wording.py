"""Regression for natural leads; question-specific facts belong in the summary."""
import pytest

from app.presentation.summary import result_introduction
from app.presentation.root_report import render_root_report


@pytest.mark.parametrize('question,expected', [
    ('分析下销售员王婷婷2025年为直销、分销的订单总金额分别多少?', '分析结果如下：'),
    ('销售额最高的商品分析，结果以柱状图展示', '分析结果如下：'),
    ('请帮我分析一下各经销商的区域医院覆盖率，用树状图展示。', '分析结果如下：'),
    ('对比2025年第四季度和2026年第一季度订单金额，画折线图', '分析结果如下：'),
    ('查询销售额最低的商品是什么？', '分析结果如下：'),
    ('各医院销量排名，展示柱状图和饼图', '分析结果如下：'),
    ('请问2025年上海市所有医院的销售订单总数量是多少？', '查询结果如下：'),
    ('帮我查下直销、分销的订单金额分别多少', '查询结果如下：'),
    ('列出合作时长大于3个月的经销商名单。', '查询结果如下：'),
    ('用树状图展示医院名单', '查询结果如下：'),
    ('\n查询销量\t，结果以柱状图展示\n', '查询结果如下：'),
    ('换2026年第一季度', '查询结果如下：'),
    ('', '查询结果如下：'),
    ('   ', '查询结果如下：'),
])
def test_lead_does_not_echo_or_rewrite_question(question, expected):
    assert result_introduction(question) == expected


@pytest.mark.parametrize('question', ['查询销量', '换2025年第四季度', '', '用树状图展示'])
def test_explicit_analysis_mode_still_takes_precedence(question):
    assert result_introduction(question, analysis=True) == '分析结果如下：'


def test_root_lead_keeps_completed_question_summary_table_and_chart():
    question = '分析各渠道2025年订单金额，结果以柱状图展示'
    summary = '直销订单金额为100元，分销为80元。'
    table = '| 渠道 | 金额 |\n| --- | --- |\n| 直销 | 100 |\n| 分销 | 80 |'
    chart = '<svg>validated-chart</svg>'
    material = {'task_id': 'channels', 'status': 'COMPLETED', 'summary': summary,
                'presentation': {'table': table, 'chart': chart}}
    report, selected = render_root_report(question, [material], {'overview': summary})
    assert report.startswith('分析结果如下：\n\n')
    assert '本次分析：' + question in report
    assert summary in report and table in report and chart in report
    assert report.count('结果如下：') == 1
    assert selected == ['channels']
