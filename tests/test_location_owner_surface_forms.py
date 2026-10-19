"""Geographic descriptors do not move a condition to the metric's base table."""
import pytest

from app.planning.task_dag import _ground_location_owners


@pytest.mark.parametrize('place,descriptor,owner', [
    ('成都','地区','组织'),('合肥','区域','客户'),('华东','地区','合作机构')])
def test_region_descriptor_keeps_the_explicit_modified_business_object(place,descriptor,owner):
    extraction={'实体':[owner,'合同'],'过滤条件':[{'entity':'合同','field':'地区名称','value':[place]}]}
    _ground_location_owners(extraction,f'统计{place}{descriptor}每家{owner}的合同覆盖率')
    assert extraction['过滤条件'][0]['entity']==owner
