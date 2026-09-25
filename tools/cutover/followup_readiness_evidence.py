"""Round 5.1 accounting helpers: blocked history is not live follow-up success."""
from collections import Counter
from statistics import mean


def statistics(values):
    if not values:
        return {'n':0,'mean':None,'p95':None,'status':'NOT_OBSERVED'}
    values=sorted(values);at=.95*(len(values)-1);lo=int(at)
    value=values[lo]+(values[min(lo+1,len(values)-1)]-values[lo])*(at-lo)
    return {'n':len(values),'mean':mean(values),'p95':value,
            'method':'Linear interpolated sample percentile; descriptive, not a population guarantee'}


def case_status(case,records,judgments):
    """One primary divergence; retain an actual failed prefix as local blocker."""
    keys=case['turns']
    reached=len(keys)==case['required_turns']
    if case['blocked_by']=='MODEL_BUDGET_EXHAUSTED' or case['status']=='NOT_RUN_BUDGET':
        return dict(status='NOT_RUN',first_divergence=None,reason='MODEL_BUDGET_EXHAUSTED')
    if not keys:
        return dict(status='NOT_RUN',first_divergence=None,reason='NO_EXECUTED_TURN')
    first=next((key for key in keys if judgments[key]['first_divergence'] is not None),None)
    if first:
        return dict(status='FAIL' if reached else 'BLOCKED',first_divergence=judgments[first]['first_divergence'],
                    first_failed_turn=first,reason=judgments[first]['reason'],current_turn_executed=reached)
    if not reached or any(records[key]['error'] or not records[key]['result'] for key in keys):
        return dict(status='BLOCKED',first_divergence=None,reason='PRECONDITION_NOT_ESTABLISHED',current_turn_executed=reached)
    if any(judgments[key].get('declared_axes_pass') is not True for key in keys):
        return dict(status='BLOCKED',first_divergence=None,reason='INCOMPLETE_DECLARED_AXES',current_turn_executed=reached)
    return dict(status='PASS',first_divergence=None,reason='DECLARED_AXES_ONLY',current_turn_executed=True)


def summarize_cases(cases,records,judgments):
    results=[dict(case_id=case['case_id'],**case_status(case,records,judgments)) for case in cases]
    distribution={}
    for result in results:
        stage=result['first_divergence']
        if stage is not None:distribution.setdefault(stage,[]).append(result['case_id'])
    return dict(cases=results,counts=dict(Counter(row['status'] for row in results)),
                first_divergence_distribution=distribution,
                unique_executed_turns=len({key for case in cases for key in case['turns']}),
                required_turn_appearances=sum(c['required_turns'] for c in cases),
                executed_turn_appearances=sum(len(c['turns']) for c in cases),
                current_turns_executed=sum(row.get('current_turn_executed',False) for row in results))
