"""Actual typed output contract for registered algorithm adapters."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,JsonValue,Field,model_validator

Status=Literal['COMPLETED','PARTIAL','NOT_APPLICABLE','NOT_COMPUTABLE','FAILED']

class ToolReport(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    method:str
    status:Status
    summary:str
    reason:str
    columns:list[str]
    rows:list[dict[str,JsonValue]]

class ReportOutput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reports:list[ToolReport]

class CandidateOutput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate:str
    score:dict[str,JsonValue]|None
    selected:bool

class ToolExecutionRecord(BaseModel):
    model_config=ConfigDict(extra='forbid')
    capability_id:str
    catalog_version:Literal['analysis-tools-v1']
    executor_id:str
    result_ref:str
    status:Status
    reason:str|None
    methods:list[str]
    effective_parameters:dict[str,JsonValue]
    scope:dict[str,JsonValue]
    input_task_ids:list[str]
    outputs:ReportOutput|CandidateOutput

    @model_validator(mode='after')
    def registered(self):
        from .registry import TOOLS
        if self.capability_id not in TOOLS or TOOLS[self.capability_id]['executor_id']!=self.executor_id:
            raise ValueError('UNREGISTERED_TOOL_RESULT')
        return self


def output_schema(capability_id):
    schema=ToolExecutionRecord.model_json_schema()
    schema['properties']['capability_id']={'const':capability_id,'type':'string'}
    return schema
