"""Internal planner API compatibility; extraction and public I/O stay intact."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from app.domain.models import PlannerExtraction, TaskPlan


@dataclass(frozen=True)
class PlanningResult:
    plan: TaskPlan | None
    single_extraction: PlannerExtraction | None
    judgment: Any = None


async def plan_question(planner: Any, question: str, *, semantic_context: str,
                        semantic_model_id: int, prior_judgment: Any = None) -> PlanningResult:
    """Support legacy plan() and the deployed router without judging twice."""
    route_method = getattr(planner, 'route_and_plan', None)
    reuse = getattr(planner, 'plan_with_judgment', None)
    routed = callable(route_method)
    reuse_judgment = routed and prior_judgment is not None and callable(reuse)
    method = reuse if reuse_judgment else (route_method if routed else planner.plan)
    parameters = inspect.signature(method).parameters
    values = {'semantic_context': semantic_context, 'semantic_model_id': semantic_model_id}
    kwargs = {name: value for name, value in values.items() if name in parameters}
    args = (prior_judgment, question) if reuse_judgment else (question,)
    outcome = await method(*args, **kwargs)
    single = outcome.single if routed else outcome
    return PlanningResult(plan=outcome.plan,
                          single_extraction=single.single_extraction if single is not None else None,
                          judgment=outcome.judgment if routed else None)


def prepare_execution(planner: Any, plan: TaskPlan):
    """DAG helpers moved from instance methods to module functions on 49."""
    deduplicate = getattr(planner, 'deduplicate', None)
    layers = getattr(planner, 'execution_layers', None)
    if not callable(deduplicate) or not callable(layers):
        from app.planning.compound_question import deduplicate_plan, execution_layers
        deduplicate, layers = deduplicate_plan, execution_layers
    execution_plan, aliases = deduplicate(plan)
    return execution_plan, aliases, layers(execution_plan)
