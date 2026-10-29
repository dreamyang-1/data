from test_structured_binding import fixture, run


def test_valid_display_binding_is_retained_even_when_already_grouped():
    extraction, knowledge, plan = fixture()
    extraction.update(维度=['医院名称'],展示字段=[{'entity':'医院','field':'医院名称'}])
    plan.update(dimensions=[{'index':0,'key':'hospital.name'}],
                display_fields=[{'index':0,'key':'hospital.name'}])
    ast, repairs = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert [d['name'] for d in ast['dimensions']] == ['hospital.name']
    assert {'type':'STRUCTURED_PROJECTION_BINDING','source':'STRUCTURED_EXTRACTION',
            'entity':'医院','field':'医院名称','resolved_field':'hospital.name'} in repairs


def test_unresolved_display_field_does_not_claim_a_binding():
    extraction, knowledge, plan = fixture()
    extraction.update(展示字段=[{'entity':'医院','field':'未登记编码'}])
    plan.update(display_fields=[{'index':0,'key':'other.id'}])
    ast, repairs = run(extraction, knowledge, plan)
    assert ast['ambiguity']
    assert not any(item['type']=='STRUCTURED_PROJECTION_BINDING' for item in repairs)
