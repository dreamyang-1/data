from prompt_build import canonicalize_structured_mentions
from vector_store import SearchResult


def test_exact_scoped_dimension_precedes_higher_scoring_attribute():
    class Store:
        def count(self): return 2
        def search(self, vector, *, top_k, where):
            assert where == {'$and': [{'semantic_model_id': 130},
                                     {'business_domain_id': {'$in': [296]}}]}
            return [
                SearchResult(id='attribute', score=.7318, text='', metadata={
                    'type': 'attribute', 'attr_code': 'product_name', 'attr_name': '商品名称'}),
                SearchResult(id='dimension', score=.7162, text='', metadata={
                    'type': 'scoped_dimension', 'dim_code': 'product_brand', 'dim_name': '商品品牌'}),
            ]
    extraction = {'维度': ['商品品牌']}
    repairs = canonicalize_structured_mentions(extraction, Store(), lambda value: [1], 130, [296])
    assert extraction['维度'] == ['商品品牌']
    assert repairs == []


def test_scoped_dimension_synonym_is_not_rewritten_to_an_attribute():
    class Store:
        def count(self): return 2
        def search(self, vector, *, top_k, where):
            return [
                SearchResult(id='a', score=.9, text='', metadata={
                    'type': 'attribute', 'attr_code': 'product_name', 'attr_name': '商品名称'}),
                SearchResult(id='d', score=.7, text='', metadata={
                    'type': 'scoped_dimension', 'dim_code': 'product_brand', 'dim_name': '商品品牌',
                    'synonyms': ['厂家品牌']}),
            ]
    extraction = {'维度': ['厂家品牌']}
    canonicalize_structured_mentions(extraction, Store(), lambda value: [1], 130, [296])
    assert extraction['维度'] == ['厂家品牌']
