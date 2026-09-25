"""Small declared catalog and scope for contract tests; never production catalog facts."""
from datetime import datetime, timezone

from app.semantic_v2.enums import *
from app.semantic_v2.models import *
from app.semantic_v2.pipeline import *
from app.semantic_v2.state_machine import ConversationState, TaskState, TaskVersion, TopicState

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def ref(code='sales', role=SemanticRole.MEASURE, catalog_type=CatalogType.METRIC):
    return BoundSemanticRef(catalog_type=catalog_type, semantic_role=role, canonical_id='fixture:' + code,
        canonical_code=code, display_name=code, catalog_version='fixture-catalog-v1', semantic_model_id='fixture-model',
        business_domain_ids=['fixture-domain'], resolution_source='DECLARED_TEST_CATALOG')


def time_spec():
    return TimeSpec(anchor=ref('transaction_date', SemanticRole.TIME_FIELD, CatalogType.DIMENSION),
        range=TimeRange(start=datetime(2025, 9, 1, tzinfo=timezone.utc), end_exclusive=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        grain=TimeGrain.MONTH, timezone='Asia/Shanghai', source='USER_EXPLICIT', as_of=NOW)


def permission():
    return PermissionContext(tenant_id='fixture-tenant', user_id='fixture-user', application_id='fixture-app',
        allowed_business_domain_ids=['fixture-domain'], authorization_decision_id='fixture-authz',
        policy_snapshot_id='fixture-policy', row_scope_hash='fixture-row-hash', column_scope_hash='fixture-column-hash', metric_scope_hash='fixture-metric-hash')


def snapshot():
    return SnapshotContext(catalog_publish_id='fixture-publish', catalog_version='fixture-catalog-v1',
        vector_index_version='fixture-index-v1', semantic_model_id='fixture-model', semantic_model_version='fixture-model-v1',
        database_id='fixture-db', business_domain_ids=['fixture-domain'], knowledge_base_names=['fixture-kb'],
        data_snapshot_id='fixture-snapshot', data_watermark=NOW)


def versions():
    return VersionMetadata(prompt_version='NO_MODEL', policy_version='fixture-policy', adapter_version='shadow-adapter-v1')


def authorizations(payload):
    p = permission()
    return tuple(BoundRefAuthorization(canonical_id=r.canonical_id, catalog_type=r.catalog_type,
        catalog_version=r.catalog_version, semantic_model_id=r.semantic_model_id,
        tenant_id=p.tenant_id, user_id=p.user_id, application_id=p.application_id,
        authorization_decision_id=p.authorization_decision_id, policy_snapshot_id=p.policy_snapshot_id, allowed=True)
        for r in collect_bound_refs(payload))


def logical(payload=None, *, delivery=None):
    from app.semantic_v2.registries import PayloadContractRegistry
    payload = payload or ChatPayload()
    definition = PayloadContractRegistry.get(payload.payload_type)
    return LogicalPlan(plan_id='fixture-plan', topic_id='topic:a', task_id='task:a', task_version=1,
        payload=payload, service_route=definition.allowed_service_routes[0], analysis_goals=sorted(definition.required_analysis_goals),
        delivery_spec=delivery or DeliverySpec(), permission_requirement=permission(), snapshot_requirement=snapshot(),
        permission_proofs=authorizations(payload), version_metadata=versions(), current_turn_ref='fixture-turn', current_turn_digest='a' * 64)


def candidate_resolution(payload):
    sets, selected = [], []
    for i, r in enumerate(collect_bound_refs(payload)):
        cid, mid = f'candidate:{i}', f'mention:{i}'
        c = SemanticCandidate(candidate_id=cid, mention_id=mid, candidate_role=r.semantic_role,
            catalog_type=r.catalog_type, canonical_id=r.canonical_id, canonical_code=r.canonical_code,
            display_name=r.display_name, retrieval_method='FIXTURE', raw_score=1, normalized_score=1,
            permission_allowed=True, catalog_version=r.catalog_version, status=ResolutionStatus.ACCEPTED)
        sets.append(SemanticCandidateSet(mention_id=mid, candidates=[c], selected_candidate_id=cid, status=ResolutionStatus.ACCEPTED))
        selected.append(cid)
    if not selected:
        return SemanticResolutionContract(status=ResolutionStatus.UNRESOLVED)
    return SemanticResolutionContract(status=ResolutionStatus.ACCEPTED, candidate_sets=sets,
        plan_candidates=[PlanCandidate(plan_candidate_id='candidate-plan', semantic_candidate_ids=selected,
            score=PlanCandidateScore(retrieval_score=1, constraint_score=1, permission_score=1, executability_score=1), executable=True)],
        selected_plan_candidate_id='candidate-plan')


def conversation():
    return ConversationState(conversation_id='fixture-conversation', tenant_id='fixture-tenant', user_id='fixture-user',
        application_id='fixture-app', state_version=0, active_topic_id='topic:a', topic_stack=['topic:a', 'topic:b'],
        topics={f'topic:{i}': TopicState(topic_id=f'topic:{i}', title=i, active_task_id=f'task:{i}', task_ids=[f'task:{i}'], last_accessed_at=NOW) for i in 'ab'},
        tasks={f'task:{i}': TaskState(task_id=f'task:{i}', topic_id=f'topic:{i}', active_version=1, status=TaskVersionStatus.RESOLVED,
            versions=[TaskVersion(version=1, status=TaskVersionStatus.RESOLVED, semantics=TaskSemanticState(metrics=[ref()]), created_at=NOW)]) for i in 'ab'})


def operation(kind, *, value=None, slot='metrics', base=1, operation_id=None, target=None):
    return SlotOperation(operation_id=operation_id or f'{kind}:{slot}', slot_path=slot, operation=kind,
        new_value=value, target_item_id=target, source='CURRENT_EXPLICIT', reason_code='DECLARED_FIXTURE',
        base_task_version=base, presence=Presence.EXPLICITLY_CLEARED if kind == 'CLEAR' else Presence.PRESENT)
