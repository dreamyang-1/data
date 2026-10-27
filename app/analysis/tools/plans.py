"""Internal model proposals; never an API, SQL or authorization contract."""
from __future__ import annotations

from datetime import date
import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .registry import CATALOG_VERSION, CHAINS, DEFAULTS, SPECIAL_PROFILES, TOOLS, ChainId

PLAN_KEY = '_analysis_tool_plan'


class AnalysisToolPlanRequired(ValueError):
    """A model plan is required; execution must never guess from question text."""


class ToolWindow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: date
    end_exclusive: date

    @model_validator(mode='after')
    def ordered(self):
        if self.start >= self.end_exclusive:raise ValueError('comparison period start must precede end')
        return self


class ComparisonPeriod(ToolWindow):
    role: Literal['BASE', 'CURRENT']


class ComparisonObject(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role: Literal['BASE','CURRENT']
    entity: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: str = Field(min_length=1)


class AnalysisToolPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    catalog_version: Literal[CATALOG_VERSION] = CATALOG_VERSION
    chain: ChainId
    coverage_mode: Literal['full_profile', 'explicit']
    capabilities: list[str] = Field(default_factory=list, description='仅填本链路登记的能力ID；full_profile由系统补齐默认组合。')
    excluded: list[str] = Field(default_factory=list)
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    grain: Literal['day','week','month','quarter','half_year','year'] | None = None
    target_window: ToolWindow | None = Field(default=None,
        description='用户目标窗口，不能含辅助基期；预测时为用户允许的历史窗口。结束日不包含。')
    comparison_kind: Literal['periods','objects'] | None = Field(default=None,
        description='对比链路必填：两期比较为periods并填两期日期；对象间比较为objects并明确两个比较角色。')
    comparison_objects: list[ComparisonObject] = Field(default_factory=list)
    comparison_periods: list[ComparisonPeriod] = Field(default_factory=list,
        description='两期比较/归因/结构变化时明确BASE和CURRENT起止；结束日期不包含。不按提及顺序猜基期。')
    auxiliary_fetch_allowed: bool = True
    unavailable: list[str] = Field(default_factory=list, description='不能满足的条件能力及原因，不伪造数据。')
    user_evidence: str = Field(min_length=1, max_length=500)

    @model_validator(mode='after')
    def registered(self):
        if self.chain in {'trend','anomaly','forecast'} and (self.grain is None or self.target_window is None):
            raise ValueError('TIME_SERIES_REQUIRES_TARGET_WINDOW_AND_GRAIN')
        if self.chain=='comparison' and self.comparison_kind is None:
            raise ValueError('COMPARISON_KIND_AND_EXPLICIT_ROLES_REQUIRED')
        if self.comparison_kind=='periods' or self.chain=='attribution':
            if sorted(item.role for item in self.comparison_periods)!=['BASE','CURRENT']:
                raise ValueError('PERIOD_ANALYSIS_REQUIRES_TWO_EXPLICIT_PERIODS')
        if self.comparison_kind=='objects' and sorted(item.role for item in self.comparison_objects)!=['BASE','CURRENT']:
            raise ValueError('OBJECT_COMPARISON_REQUIRES_TWO_EXPLICIT_OBJECTS')
        allowed = {tool['capability_id'] for tool in CHAINS[self.chain][2]}
        for value in [*self.capabilities, *self.excluded, *self.parameters]:
            if value not in allowed:raise ValueError('UNREGISTERED_OR_WRONG_CHAIN_CAPABILITY:' + value)
        if len(self.capabilities) != len(set(self.capabilities)):raise ValueError('DUPLICATE_CAPABILITY')
        special=set(self.capabilities).intersection(SPECIAL_PROFILES)
        if len(special)>1:
            raise ValueError('INCOMPATIBLE_INPUT_SHAPES_SPLIT_SUBQUESTIONS')
        if special and not set(self.capabilities).issubset(SPECIAL_PROFILES[next(iter(special))]):
            raise ValueError('INCOMPATIBLE_INPUT_SHAPES_SPLIT_SUBQUESTIONS')
        if self.coverage_mode == 'explicit' and not set(self.capabilities).difference(self.excluded):
            raise ValueError('EMPTY_EXPLICIT_CAPABILITIES')
        for capability, parameters in self.parameters.items():
            try:
                json.dumps(parameters, allow_nan=False)
                Draft202012Validator(TOOLS[capability]['parameter_schema']).validate(parameters)
            except Exception as exc:
                raise ValueError('INVALID_TOOL_PARAMETERS:' + capability) from exc
        if self.comparison_periods and sorted(item.role for item in self.comparison_periods) != ['BASE', 'CURRENT']:
            raise ValueError('COMPARISON_REQUIRES_UNIQUE_BASE_AND_CURRENT')
        self.selection()  # Validate exclusions/parameter bindings before any query is planned.
        return self

    def selection(self):
        special=next((key for key in self.capabilities if key in SPECIAL_PROFILES),None)
        defaults=SPECIAL_PROFILES[special] if special else DEFAULTS[self.chain]
        keys = list(dict.fromkeys([*(defaults if self.coverage_mode == 'full_profile' else ()), *self.capabilities]))
        keys = [key for key in keys if key not in self.excluded]
        if not keys:raise ValueError('EMPTY_TOOL_SELECTION')
        if not set(self.parameters).issubset(keys):raise ValueError('UNBOUND_TOOL_PARAMETERS')
        return {'mode': 'SELECTED', 'capabilities': keys, 'source': 'MODEL_CATALOG',
                'coverage_mode': self.coverage_mode, 'catalog_version': CATALOG_VERSION,
                'parameters': self.parameters, 'unavailable': self.unavailable}


def plan_from_structured(structured):
    value = (structured or {}).get(PLAN_KEY)
    return AnalysisToolPlan.model_validate(value) if value is not None else None


def plan_schema(chain):
    schema = AnalysisToolPlan.model_json_schema()
    schema['properties']['chain'] = {'const': chain, 'type': 'string'}
    allowed = [tool['capability_id'] for tool in CHAINS[chain][2]]
    for key in ['capabilities', 'excluded']:
        schema['properties'][key]['items'] = {'type': 'string', 'enum': allowed}
    if chain=='comparison':
        schema['properties']['comparison_kind']={'type':'string','enum':['periods','objects']}
        schema.setdefault('required',[]).append('comparison_kind')
        schema['allOf']=[
            {'if':{'properties':{'comparison_kind':{'const':'periods'}}},
             'then':{'properties':{'comparison_periods':{'minItems':2,'maxItems':2}},'required':['comparison_periods']}},
            {'if':{'properties':{'comparison_kind':{'const':'objects'}}},
             'then':{'properties':{'comparison_objects':{'minItems':2,'maxItems':2}},'required':['comparison_objects']}}]
    if chain=='attribution':
        schema['properties']['comparison_periods'].update(minItems=2,maxItems=2)
        schema.setdefault('required',[]).append('comparison_periods')
    return schema
