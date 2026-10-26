import pytest
from app.domain.models import MetricRef
from app.intent.classifier import RuleBasedIntentClassifier
from test_dependent_time_scope import apply_child


@pytest.mark.parametrize('period', [None, {}, {'unit':None,'time_range':None,'anchor':None}])
def test_dependent_company_projection_passes_actual_completeness_gate(period):
    request=apply_child(period)
    assert RuleBasedIntentClassifier().required_missing_slots(request)==[]


def test_explicit_period_remains_on_dependent_query():
    request=apply_child({'time_range':'2024年'})
    assert RuleBasedIntentClassifier().required_missing_slots(request)==[]
    assert request.time_range.start.year==2024


@pytest.mark.parametrize('kind', ['missing_extraction','missing_time_slot','metric','grain','entity','fields'])
def test_existing_requirements_are_preserved(kind):
    request=apply_child(None)
    expected='time_range'
    if kind=='missing_extraction': request._planner_extraction=None
    if kind=='missing_time_slot': request._planner_extraction.structured.pop('时间粒度')
    if kind=='metric': request.metrics=[MetricRef(input='含税销售总额')]
    if kind=='grain': request._planner_extraction.structured['时间粒度']={'unit':'月'}
    if kind=='entity': request.entity=None; expected='entity'
    if kind=='fields': request.fields=[]; expected='fields'
    assert expected in RuleBasedIntentClassifier().required_missing_slots(request)
