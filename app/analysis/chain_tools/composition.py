"""Validate model-produced composition parameters; never parse user wording."""
import math


def _ratio(value, *, allow_zero=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('MODEL_RATIO_PARAMETER_REQUIRED')
    if not (0<=value<=1 if allow_zero else 0<value<1):
        raise ValueError('RATIO_PARAMETER_OUT_OF_RANGE')
    return float(value)


def cumulative_contribution_threshold(parameters):
    if parameters is None:return None
    if not isinstance(parameters,dict):raise ValueError('MODEL_TOOL_PARAMETERS_REQUIRED')
    value=parameters.get('threshold')
    return None if value is None else _ratio(value)


def group_share_threshold(parameters):
    if parameters is None:return None
    if not isinstance(parameters,dict):raise ValueError('MODEL_TOOL_PARAMETERS_REQUIRED')
    value=parameters.get('minimum_share')
    if value is None:return None
    inclusive=parameters.get('inclusive',True)
    if type(inclusive) is not bool:raise ValueError('BOOLEAN_INCLUSIVE_PARAMETER_REQUIRED')
    return _ratio(value,allow_zero=True),inclusive
