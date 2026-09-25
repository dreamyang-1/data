from app.adapters.asl_notices import binding_notices
from app.presentation.intent_recognition import render_asl_extraction_json


def test_related_scope_is_disclosed_in_asl_and_final_notice_channel():
    note='按共同适用科室关联，不证明实际成交科室。'
    ast={'metrics':[{'name':'sales'}],'dimensions':[], 'related_filters':[{'scope_note':note}]}
    assert note in render_asl_extraction_json(ast)
    assert binding_notices([{'type':'SHARED_ATTRIBUTE_SCOPE','scope_note':note}])==[note]
