import pytest
from types import SimpleNamespace as Obj
from test_structured_binding import run


@pytest.mark.parametrize('card,valid',[('N:1',True),('1:1',True),('1:N',False),('N:M',False)])
def test_catalog_related_attribute_is_display_not_group(card,valid):
    attrs=[{'attr_code':'staff_code','attr_name':'人员编号','field_mapping':'staff.staff_code'},
           {'attr_code':'staff_name','attr_name':'人员姓名','field_mapping':'staff.staff_name'}]
    knowledge={'entities':[Obj(metadata={'entity_code':'staff','attributes':attrs}),
                           Obj(metadata={'entity_code':'company','attributes':[{'attr_name':'所属公司','field_mapping':'company.name'}]})],
        'metrics':[Obj(metadata={'metric_code':'sales','metric_name':'销售额','entity_code':'staff'})],
        'dimensions':[Obj(metadata={'dim_code':'staff','bind_entities':[{'mappingTable':'staff','mappingColumn':'staff_code'}]})],
        'relations':[Obj(metadata={'relation_type':card,'join_key':{'source_field':'staff.company_code','target_field':'company.code'}})],
        '_vector_authorized_fields':['staff.staff_code','staff.staff_name','company.name']}
    extraction={'实体':['人员','公司'],'指标':[{'name':'销售额'}],'维度':['staff'],
        '展示字段':[{'entity':'公司','field':'所属公司'}],'过滤条件':[],'排序':[],
        '时间粒度':{'unit':None,'time_range':None},'限制':5}
    ast,_=run(extraction,knowledge,{'subject':'staff','metrics':[{'index':0,'key':'sales'}],
        'dimensions':[{'index':0,'key':'staff'}],'display_fields':[{'index':0,'key':'company.name'}]})
    assert bool(ast['ambiguity']) is not valid
    assert ast['dimensions']==[{'name':'staff','attr':None,'level':None,'granularity':None}]
    if valid:assert ast['display_fields']==[{'name':'company.name','alias':'所属公司'}]
