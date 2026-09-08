"""A source-proven exact entity binding cannot silently broaden to substring matching."""
import pytest

from agent import _normalize_relation_name_filters
from test_relational_semantic_scope import _asl, _bridge_fixture


@pytest.mark.parametrize("operator,value,expected", [
    ("=", "示例医疗产品", "="), ("LIKE", "%示例医疗产品%", "="),
    ("!=", "示例医疗产品", "!="),
    ("IN", ["示例医疗产品", "另一医疗产品"], "IN"),
    ("NOT IN", ["示例医疗产品", "另一医疗产品"], "NOT IN"),
])
def test_source_exact_proof_preserves_literal_and_negative_set_semantics(operator, value, expected):
    _, builder = _bridge_fixture(product_is_direct=False)
    question="示例医疗产品和另一医疗产品适用于哪些科室"
    builder.build(question)
    ast=_asl("department.dept_name")
    ast['filters']=[{'field':'product_dept_relation.product_code','operator':operator,'value':value}]
    calls=[]
    def exact(model, domain, candidates, literal):
        calls.append((model,domain,literal))
        return ['product.product_name']
    _normalize_relation_name_filters(ast,builder.last_knowledge,question,semantic_model_id=81,business_domain_id=205,
        exact_value_resolver=exact,exact_attribute_value_resolver=lambda *args:[],catalog_value_resolver=lambda *args:[])
    assert ast['filters']==[{'field':'product.product_name','operator':expected,
                            'value':value if isinstance(value,list) else '示例医疗产品'}]
    assert calls and all(model==81 and domain==205 for model,domain,_ in calls)


def test_without_source_exact_proof_a_relationship_name_is_not_silently_accepted():
    _, builder = _bridge_fixture(product_is_direct=False)
    question='示例医疗产品适用于哪些科室'; builder.build(question)
    ast=_asl('department.dept_name')
    ast['filters']=[{'field':'product_dept_relation.product_code','operator':'=','value':'示例医疗产品'}]
    _normalize_relation_name_filters(ast,builder.last_knowledge,question,semantic_model_id=81,business_domain_id=205,
        exact_value_resolver=lambda *args:[],exact_attribute_value_resolver=lambda *args:[],catalog_value_resolver=lambda *args:[])
    assert ast['ambiguity']
