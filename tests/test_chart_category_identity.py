"""No overlapping categories, implicit aggregation or precision loss in data."""
from copy import deepcopy
from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest

from app.analysis.visualization import build_chart_specs, requested_chart_specs, render_chart_svg
from app.domain.models import PrimaryIntent
from app.services.extension_dispatcher import ExtensionDispatcher


NS = {'s': 'http://www.w3.org/2000/svg'}


@pytest.mark.parametrize('name,code', [
    ('商品名称', '商品编码'), ('产品名称', '产品编号'),
    ('经销商名称', '经销商（编码）'), ('医院名称', '医院ID'),
    ('省份名称', '省份代码'), ('名称', '编码'),
    ('product_name', 'product_code'), ('dealer.name', 'dealer.id'),
    ('hospitalName', 'hospitalId'), ('region_name', 'region_key'),
])
@pytest.mark.parametrize('automatic', [False, True])
def test_ten_identity_pairs_keep_two_rows_and_values(name, code, automatic):
    rows = [{name: '同名', code: 'A', '销量': 24},
            {name: '同名', code: 'B', '销量': 13}]
    before = deepcopy(rows)
    if automatic:
        specs = build_chart_specs(PrimaryIntent.COMPARISON_ANALYSIS, list(rows[0]), rows,
                                  {'metric_column': '销量', 'label_column': name})
    else:
        specs, notes = requested_chart_specs('柱状图展示销量', list(rows[0]), rows, {})
        assert not notes
    assert len(specs) == 1
    spec = specs[0].model_dump(mode='json')
    assert len(spec['data']) == 2
    assert [row['销量'] for row in spec['data']] == [24, 13]
    assert [row[name] for row in spec['data']] == ['同名（编码：A）', '同名（编码：B）']
    assert rows == before
    tool, arguments = ExtensionDispatcher._visualization_call(spec, {'generate_bar_chart', 'generate_column_chart'})
    assert len({row['category'] for row in arguments['data']}) == 2
    assert [row['value'] for row in arguments['data']] == [24, 13]
    svg = ET.fromstring(render_chart_svg(spec))
    assert len(svg.findall('s:rect/s:title', NS)) == 2


@pytest.mark.parametrize('identity', [None, '', 'A'])
def test_missing_or_repeated_identity_does_not_create_or_sum_categories(identity):
    rows = [{'商品名称': '同名', '商品编码': 'A', '销量': 24},
            {'商品名称': '同名', '商品编码': identity, '销量': 13}]
    specs, notes = requested_chart_specs('柱状图展示销量', list(rows[0]), rows, {})
    assert not specs and any('未合并或累加' in note for note in notes)


def test_unrelated_code_cannot_identify_a_product():
    rows = [{'商品名称': '同名', '医院编码': 'A', '销量': 24},
            {'商品名称': '同名', '医院编码': 'B', '销量': 13}]
    specs, notes = requested_chart_specs('柱状图展示销量', list(rows[0]), rows, {})
    assert not specs and notes


def test_multiple_possible_identity_columns_are_not_guessed():
    rows = [{'商品名称': '同名', '商品编码': 'A', '商品ID': 1, '销量': 24},
            {'商品名称': '同名', '商品编码': 'B', '商品ID': 2, '销量': 13}]
    specs, notes = requested_chart_specs('柱状图展示销量', list(rows[0]), rows, {})
    assert not specs and notes


def test_unique_categories_remain_unchanged_and_codes_are_not_sent():
    rows = [{'商品名称': '甲', '商品编码': 'A', '销量': 24},
            {'商品名称': '乙', '商品编码': 'B', '销量': 13}]
    specs, notes = requested_chart_specs('柱状图展示销量', list(rows[0]), rows, {})
    assert specs[0].data == [{'商品名称': '甲', '销量': 24}, {'商品名称': '乙', '销量': 13}]
    assert not notes and not specs[0].horizontal


@pytest.mark.parametrize('kind', ['BAR', 'PIE'])
def test_renderers_reject_unresolved_category_collisions(kind):
    spec = dict(chart_type=kind, title='test', x_field='名称', y_fields=['销量'],
                data=[{'名称': '同名', '销量': 24}, {'名称': '同名', '销量': 13}])
    assert render_chart_svg(spec) is None
    assert ExtensionDispatcher._visualization_call(spec, {'generate_column_chart', 'generate_pie_chart'}) is None


def test_long_monetary_bar_preserves_exact_data_hover_and_identity():
    name = '超长名称测试商品一次性医用设备及附件'
    values = [Decimal('23456789.1234'), Decimal('12345678.9876')]
    rows = [{'商品名称': name, '商品编码': code, '含税销售总额': value}
            for code, value in zip(['TEST-A001', 'TEST-B002'], values)]
    specs, notes = requested_chart_specs('柱状图展示含税销售总额', list(rows[0]), rows, {})
    spec = specs[0]
    assert spec.horizontal and not notes
    assert [row['含税销售总额'] for row in spec.data] == values
    root = ET.fromstring(render_chart_svg(spec))
    assert [node.text for node in root.findall("s:text[@class='data-label']", NS)] == ['2,345.68万', '1,234.57万']
    titles = [node.text for node in root.findall('s:rect/s:title', NS)]
    assert any('23,456,789.1234' in title and 'TEST-A001' in title for title in titles)
    assert any('12,345,678.9876' in title and 'TEST-B002' in title for title in titles)
    category_text = [node.text or '' for node in root.findall('s:text', NS)]
    assert any('TEST-A001' in text for text in category_text)
    assert any('TEST-B002' in text for text in category_text)
    assert ExtensionDispatcher._visualization_call(spec.model_dump(), {'generate_bar_chart'}) is None


def test_long_count_labels_prefer_horizontal_mcp_or_local_fallback():
    rows = [{'商品名称': '测试用长名称商品及配套附件用于展示校验', '订单笔数': 35206}]
    specs, notes = requested_chart_specs('柱状图展示订单笔数', list(rows[0]), rows, {})
    spec = specs[0].model_dump()
    assert spec['horizontal'] and not notes
    assert ExtensionDispatcher._visualization_call(spec, {'generate_bar_chart', 'generate_column_chart'})[0] == 'generate_bar_chart'
    assert ExtensionDispatcher._visualization_call(spec, {'generate_column_chart'}) is None
    root = ET.fromstring(render_chart_svg(spec))
    assert root.find("s:text[@class='data-label']", NS).text == '35,206'


@pytest.mark.parametrize('value,display', [(0, '0'), (-123456.789, '-12.35万'),
    (123456789.1234, '1.23亿'), (Decimal('9999.1234'), '9,999.1234')])
def test_money_formatting_never_changes_the_underlying_value(value, display):
    rows = [{'名称': '甲', '金额': value}]
    specs, _ = requested_chart_specs('条形图展示金额', list(rows[0]), rows, {})
    root = ET.fromstring(render_chart_svg(specs[0]))
    assert root.find("s:text[@class='data-label']", NS).text == display
    assert specs[0].data[0]['金额'] == value
