"""Validate and align input roles declared in the model plan."""
from datetime import date
from app.analysis.chain_tools.temporal_window import bucket_bounds


def align_period_roles(labels,declared_periods,grain):
    """Explicit BASE/CURRENT wins over chronological order; no text inference."""
    if not declared_periods:return labels
    from app.analysis.engine import AnalysisError
    aligned={}
    for declared in declared_periods:
        start=date.fromisoformat(declared['start']);end=date.fromisoformat(declared['end_exclusive'])
        matches=[label for label in labels if (bounds:=bucket_bounds(label,grain))
            and start<=bounds[0] and bounds[1]<=end]
        if len(matches)!=1:raise AnalysisError('归因时间桶不能唯一对应模型声明的基期或本期')
        aligned[declared['role']]=matches[0]
    if set(aligned)!={'BASE','CURRENT'} or aligned['BASE']==aligned['CURRENT']:
        raise AnalysisError('归因基期与本期角色不完整或重复')
    return aligned['BASE'],aligned['CURRENT']


def require_observed_pair(member,values,base,current):
    if base not in values or current not in values:
        from app.analysis.engine import AnalysisError
        raise AnalysisError(f'归因分组{member}缺少基期或本期记录；未将缺失值补成0')
