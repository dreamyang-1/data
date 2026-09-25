"""Prevent a shared failed prefix or incomplete plan from inflating readiness."""
from tools.cutover.followup_readiness_evidence import case_status,summarize_cases,statistics


def test_shared_failed_prefix_blocks_each_case_but_is_one_observed_failure():
    records={'prefix':{'error':{'reason':'V2_SLOT_OPERATION_CONFLICT'},'result':None}}
    judgments={'prefix':{'first_divergence':'SLOT_OPERATION','reason':'CONTRACT_DISAGREEMENT'}}
    cases=[dict(case_id=str(i),turns=['prefix'],required_turns=2,status='BLOCKED_HISTORY',blocked_by='prefix') for i in range(3)]
    summary=summarize_cases(cases,records,judgments)
    assert summary['counts']=={'BLOCKED':3}
    assert summary['unique_executed_turns']==1 and summary['current_turns_executed']==0
    assert summary['first_divergence_distribution']=={'SLOT_OPERATION':['0','1','2']}


def test_budget_cannot_make_preceding_plan_a_completed_case():
    case=dict(turns=['prefix'],required_turns=2,status='PLAN',blocked_by='MODEL_BUDGET_EXHAUSTED')
    assert case_status(case,{}, {})['status']=='NOT_RUN'


def test_accepted_plan_without_independent_axes_is_not_pass():
    case=dict(turns=['one'],required_turns=1,status='PLAN',blocked_by=None)
    record={'one':{'error':None,'result':{'plan':{}}}}
    judgment={'one':{'first_divergence':None,'declared_axes_pass':None}}
    assert case_status(case,record,judgment)['status']=='BLOCKED'
    judgment['one']['declared_axes_pass']=True
    assert case_status(case,record,judgment)['status']=='PASS'


def test_no_latency_receipt_remains_unknown():
    assert statistics([])['mean'] is None and statistics([])['n']==0
    assert statistics([1,3])['p95']==2.9
