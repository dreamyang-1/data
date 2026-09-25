"""The two core registries. Policy tables are data, never model output."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .enums import AnalysisGoal, ExecutionBackend, QueryShape, ServiceRoute, SlotOperationType, TimeGrain
from .models import AlgorithmRef, TaskSemanticState


@dataclass(frozen=True)
class PayloadDefinition:
    payload_type: str
    resolved_query_shape: QueryShape
    allowed_service_routes: tuple[ServiceRoute, ...]
    required_analysis_goals: frozenset[AnalysisGoal]
    allowed_analysis_goals: frozenset[AnalysisGoal]
    execution_backend: ExecutionBackend
    result_contract_compiler_id: str
    legacy_adapter_policy: str
    supports_dataset_reuse: bool
    required_capabilities: tuple[str, ...]


def _definition(payload, shape, route, goals, backend, *, reuse=False, extra_goals=()):
    required = frozenset(AnalysisGoal(g) for g in goals)
    return PayloadDefinition(payload, QueryShape(shape), (ServiceRoute(route),), required,
                             required | frozenset(AnalysisGoal(g) for g in extra_goals),
                             ExecutionBackend(backend), 'result-compiler-v1',
                             'UNSUPPORTED' if backend not in {'CHAT_RESPONSE', 'CONTROL'} else 'LOSSLESS',
                             reuse, (payload.lower(),))


class PayloadContractRegistry:
    definitions = MappingProxyType({d.payload_type: d for d in (
        _definition('CHAT', 'CHAT_ONLY', 'CHAT', (), 'CHAT_RESPONSE'),
        _definition('CONTROL', 'CONTROL_ONLY', 'CONTROL', (), 'CONTROL'),
        _definition('CAPABILITY_HELP', 'CAPABILITY_HELP', 'CAPABILITY_HELP', (), 'CHAT_RESPONSE'),
        _definition('OUT_OF_SCOPE', 'OUT_OF_SCOPE', 'OUT_OF_SCOPE', (), 'CHAT_RESPONSE'),
        _definition('SCALAR_AGGREGATE', 'SCALAR_AGGREGATE', 'DATA_QUERY', ('AGGREGATE',), 'SEMANTIC_QUERY', extra_goals=('LOOKUP',)),
        _definition('GROUPED_AGGREGATE', 'GROUPED_AGGREGATE', 'DATA_QUERY', ('AGGREGATE',), 'SEMANTIC_QUERY', extra_goals=('COMPOSITION',)),
        _definition('TIME_SERIES', 'TIME_SERIES', 'DATA_QUERY', ('TREND',), 'SEMANTIC_QUERY'),
        _definition('RANKING', 'RANKING', 'DATA_QUERY', ('RANK',), 'SEMANTIC_QUERY'),
        _definition('COMPARISON', 'COMPARISON_SET', 'ANALYTICS', ('COMPARE',), 'ANALYTICS'),
        _definition('RELATION_LIST', 'RELATION_LIST', 'DATA_QUERY', ('LOOKUP',), 'SEMANTIC_QUERY'),
        _definition('DETAIL_ROWS', 'DETAIL_ROWS', 'DATA_QUERY', ('LOOKUP',), 'SEMANTIC_QUERY'),
        _definition('DATASET_TRANSFORM', 'DATASET_TRANSFORM', 'DATASET_OPERATION', (), 'DATASET_LOCAL', reuse=True),
        _definition('METRIC_DEFINITION', 'METADATA_LOOKUP', 'METRIC_CATALOG', ('EXPLAIN',), 'SEMANTIC_METADATA'),
        _definition('METADATA', 'METADATA_LOOKUP', 'METADATA', (), 'SEMANTIC_METADATA'),
        _definition('LINEAGE', 'LINEAGE_GRAPH', 'LINEAGE', (), 'SEMANTIC_METADATA'),
        _definition('DATA_QUALITY', 'QUALITY_CHECK', 'DATA_QUALITY', (), 'ANALYTICS'),
        _definition('ANOMALY', 'ANOMALY_SERIES', 'ANALYTICS', ('ANOMALY',), 'ANALYTICS'),
        _definition('ROOT_CAUSE', 'ROOT_CAUSE_PIPELINE', 'ANALYTICS', ('ROOT_CAUSE',), 'ANALYTICS'),
        _definition('FORECAST', 'FORECAST_SERIES', 'FORECAST', ('FORECAST',), 'ANALYTICS'),
        _definition('REPORT_COMPOSITION', 'REPORT_PIPELINE', 'REPORTING', (), 'REPORT_COMPOSITION'),
    )})
    # Backed by current source; this is a contract allowlist, not production routing.
    algorithms = MappingProxyType({
        'deterministic_forecast_selector': ('forecast-selector-v1', 'FORECAST', frozenset(TimeGrain) - {TimeGrain.NONE}),
        'robust_anomaly': ('analysis-contract-v2', 'ANOMALY', frozenset(TimeGrain) - {TimeGrain.NONE}),
    })
    unsupported_capabilities = frozenset({'METRIC_CATALOG_BROWSE', 'METADATA_BROWSE'})

    @classmethod
    def get(cls, payload_type):
        try:
            return cls.definitions[payload_type]
        except KeyError as exc:
            raise ValueError('PLAN_VALIDATION_FAILURE: unknown payload type') from exc

    @classmethod
    def resolve_query_shape(cls, payload_type):
        return cls.get(payload_type).resolved_query_shape

    @classmethod
    def validate(cls, payload, service_route, analysis_goals):
        definition = cls.get(payload.payload_type)
        goals = set(analysis_goals)
        if (service_route not in definition.allowed_service_routes or
                not definition.required_analysis_goals <= goals or not goals <= definition.allowed_analysis_goals):
            raise ValueError('PLAN_VALIDATION_FAILURE: incompatible payload/route/goals')
        if payload.payload_type == 'ANOMALY':
            cls.validate_algorithm(payload.algorithm, payload_type='ANOMALY', grain=payload.time.grain)
        return definition

    @classmethod
    def validate_algorithm(cls, algorithm: AlgorithmRef, *, payload_type=None, grain=None):
        rule = cls.algorithms.get(algorithm.algorithm_id)
        if rule is None or algorithm.algorithm_version != rule[0] or algorithm.parameter_profile_id is not None:
            raise ValueError('algorithm identity/version/profile is not governed')
        if payload_type is not None and payload_type != rule[1]:
            raise ValueError('algorithm incompatible with payload')
        if grain is not None and grain not in rule[2]:
            raise ValueError('algorithm incompatible with grain')


@dataclass(frozen=True)
class SlotDefinition:
    slot_name: str
    value_type: object
    cardinality: str
    allowed_operations: frozenset[SlotOperationType]
    identity_policy: str
    semantic_equality_policy: str
    invalidation_policy: str


class SlotDefinitionRegistry:
    definitions = MappingProxyType({
        name: SlotDefinition(name, field.annotation,
                             'SET' if name in {'metrics', 'dimensions', 'analysis_goals', 'policy_decisions'} else 'ORDERED_LIST' if name == 'projection_spec' else 'STRUCTURED',
                             frozenset(SlotOperationType) if name in {'metrics', 'dimensions', 'analysis_goals', 'policy_decisions'} else frozenset(SlotOperationType) - {SlotOperationType.ADD, SlotOperationType.REMOVE},
                             'CANONICAL_IDENTITY', 'BUSINESS_SEMANTICS',
                             'KEEP_DATASET' if name in {'projection_spec', 'delivery_spec'} else 'INVALIDATE_DATASET')
        for name, field in TaskSemanticState.model_fields.items()
    })

    @classmethod
    def get(cls, slot_name):
        try:
            return cls.definitions[slot_name]
        except KeyError as exc:
            raise ValueError(f'unknown task slot: {slot_name}') from exc
