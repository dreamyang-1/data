"""Compile only explicit model analysis plans; no question keyword fallback."""
from __future__ import annotations
from datetime import date
from typing import Any
from app.domain.models import PrimaryIntent
from app.analysis.tools.plans import plan_from_structured, AnalysisToolPlanRequired
from app.analysis.tools.registry import intent_for_chain
from app.analysis.tools.requirements import build_model_requirements


def analysis_intent(intent: PrimaryIntent, question=None) -> PrimaryIntent:
    return intent


def trend_comparison_modes(question=None, *, selection=None) -> list[str]:
    if selection is None:
        raise AnalysisToolPlanRequired('ANALYSIS_TOOL_PLAN_REQUIRED')
    ids=set(selection.get('capabilities') or [])
    return [label for label,key in [('环比','period_over_period_change'),('同比','year_over_year_change')] if key in ids]


def build_requirements(intent: PrimaryIntent, question: str, structured: dict[str,Any], *, today: date) -> dict[str,Any]:
    proposal=plan_from_structured(structured)
    if proposal is None:
        raise AnalysisToolPlanRequired('ANALYSIS_TOOL_PLAN_REQUIRED: 先由模型规划分析工具与所需数据')
    if intent!=intent_for_chain(proposal.chain):
        raise ValueError('ANALYSIS_PLAN_INTENT_MISMATCH')
    return build_model_requirements(proposal,structured,today=today)
