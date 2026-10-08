"""Offline evaluation gates and identity policy; never changes production routing.

Business authority: user decision of 2026-09-09. Evidence is scoped and frozen;
an aggregate observation proves uniqueness only within its recorded snapshot.
"""
from __future__ import annotations

import hashlib
import json


NAME_WARNING = ('当前语义目录尚未声明该实体的唯一身份字段，本结果暂按名称去重。'
    '同名但实际为不同实体的记录可能被合并；'
    '如需精确区分，可同时返回实体编码/ID或地区等辅助标识。')
COLLISION_WARNING = '检测到同名实体，已按实体编码区分。'
STRICT_SHAPES = frozenset({'DISTINCT_ENTITY_COUNT', 'ENTITY_RANKING', 'RELATIONSHIP', 'JOIN', 'ATTRIBUTION'})
NAME_SHAPES = frozenset({'DISPLAY', 'NAME_LIST', 'EXPLICIT_NAME_COUNT', 'NON_RELATIONAL_LOW_RISK'})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def snapshot_unique(candidate):
    counts = [candidate.get(k) for k in ('total_count', 'non_null_count', 'distinct_count',
        'duplicate_count', 'duplicate_groups', 'blank_count')]
    if any(type(v) is not int or v < 0 for v in counts):
        return False
    total, non_null, distinct, duplicates, groups, blank = counts
    return total > 0 and total == non_null == distinct and duplicates == groups == blank == 0


def declared_identity(entity):
    """Recover an unambiguous attribute PK when the entity-level field is empty.

    Do not mutate the catalog or convert string '0' into True. Multiple attribute
    PK flags need an explicit composite declaration; unique attributes may be
    alternative keys and are not silently concatenated.
    """
    top=entity.get('primary_key')
    attributes=[a['attr_code'] for a in entity.get('attributes',[]) if a.get('is_primary_key') is True]
    if isinstance(top,str) and top:
        key=[top]
    elif isinstance(top,list) and top and all(isinstance(v,str) and v for v in top):
        key=top
    elif top is None and len(attributes)==1:
        return {'key':attributes,'evidence':'ATTRIBUTE_PRIMARY_KEY','metadata_inconsistency':True}
    elif top is None and not attributes:
        return {'key':None,'evidence':'NO_CURRENT_DECLARATION','metadata_inconsistency':False}
    else:
        return {'key':None,'evidence':'AMBIGUOUS_DECLARATION','metadata_inconsistency':True}
    if attributes and set(attributes)!=set(key):
        return {'key':None,'evidence':'CONFLICTING_DECLARATIONS','metadata_inconsistency':True}
    return {'key':key,'evidence':'ENTITY_PRIMARY_KEY','metadata_inconsistency':False}


def identity_decision(*, declared_key, candidate, observation, query_shape,
                      same_name_different_ids=False, grain_verified=True):
    """Evaluate a chosen, reviewed key. Never guess a key from a column name.

    declared_key comes from the scoped catalog (including an unambiguous
    attribute declaration). Candidate selection/physical mapping is audited
    separately; a chosen display name must not be submitted as an ID candidate.
    """
    if query_shape not in STRICT_SHAPES | NAME_SHAPES:
        raise ValueError('UNKNOWN_QUERY_SHAPE')
    stamp = observation.get('catalog_version') and observation.get('snapshot_hash') and observation.get('scope')
    if not stamp:
        raise ValueError('SCOPED_SNAPSHOT_EVIDENCE_REQUIRED')
    candidate = candidate or {}
    fields = candidate.get('identity_candidate') or []
    explicit_bad_data = 'total_count' in candidate and not snapshot_unique(candidate)
    formal = bool(declared_key and list(declared_key) == fields and candidate.get('exists') is True and not explicit_bad_data)
    provisional = bool(fields and candidate.get('candidate_kind') == 'CODE_OR_ID'
        and snapshot_unique(candidate) and candidate.get('exists') is True)
    if formal or provisional:
        mode = 'PROVEN_CATALOG_IDENTITY' if formal else 'PROVISIONAL_VERIFIED_IDENTITY'
        return dict(identity_mode=mode, identity_fields=fields, catalog_declared=formal,
            uniqueness_verified=snapshot_unique(candidate), observation=observation,
            allowed=grain_verified or query_shape not in STRICT_SHAPES,
            warning=COLLISION_WARNING if same_name_different_ids else None,
            reason=None if grain_verified or query_shape not in STRICT_SHAPES else 'ENTITY_GRAIN_NEEDS_DECISION',
            join_rule='CATALOG_RELATION_AND_CANONICAL_BINDING_ONLY')
    allowed = query_shape in NAME_SHAPES and not same_name_different_ids
    return dict(identity_mode='NAME_FALLBACK', identity_fields=[], catalog_declared=False,
        uniqueness_verified=False, observation=observation, allowed=allowed,
        warning=NAME_WARNING, reason=None if allowed else 'EXACT_ENTITY_IDENTITY_REQUIRED',
        join_rule='NAME_JOIN_FORBIDDEN')


def stage_gate(stage, *, frozen_snapshot=False, labels_ready=False, evaluator_verified=False,
               model_configured=False, evaluation_passed=False, state_isolated=False,
               writes_production_state=False, executes_sql=False, takes_over_response=False,
               native_publication=False, redis_recovery=False, deployment_trust=False,
               shadow_passed=False, user_cutover_approval=False):
    """Eligibility to run a stage, not a claim the stage has passed.

    Query-shape identity eligibility is checked separately per case. Redis and
    native publication never globally block offline evaluation or isolated shadow.
    """
    missing = []
    def need(value, reason):
        if not value:
            missing.append(reason)
    if stage not in {'GOLD', 'EVALUATOR', 'MODEL_BENCHMARK', 'PLAN_ONLY_SHADOW', 'CANARY', 'CUTOVER'}:
        raise ValueError('UNKNOWN_STAGE')
    need(frozen_snapshot, 'CERTIFIED_SCOPED_SNAPSHOT_REQUIRED')
    if stage != 'GOLD':
        need(labels_ready, 'REVIEWED_AXIS_LABELS_REQUIRED')
    if stage in {'MODEL_BENCHMARK', 'PLAN_ONLY_SHADOW', 'CANARY', 'CUTOVER'}:
        need(evaluator_verified, 'DETERMINISTIC_EVALUATOR_REQUIRED')
    if stage == 'MODEL_BENCHMARK':
        need(model_configured, 'MODEL_CONFIGURATION_REQUIRED')
    if stage in {'PLAN_ONLY_SHADOW', 'CANARY', 'CUTOVER'}:
        need(evaluation_passed, 'EVALUATION_ACCEPTANCE_REQUIRED')
        need(state_isolated, 'STATE_ISOLATION_REQUIRED')
    if stage == 'PLAN_ONLY_SHADOW':
        need(not writes_production_state and not executes_sql and not takes_over_response,
            'PLAN_ONLY_SIDE_EFFECT_BOUNDARY_REQUIRED')
    if stage in {'CANARY', 'CUTOVER'}:
        need(native_publication, 'NATIVE_CATALOG_PUBLICATION_REQUIRED')
        need(redis_recovery, 'PRODUCTION_REDIS_RECOVERY_REQUIRED')
        need(deployment_trust, 'DEPLOYED_TRUST_BOUNDARY_REQUIRED')
        need(shadow_passed, 'REAL_SHADOW_ACCEPTANCE_REQUIRED')
    if stage == 'CUTOVER':
        need(user_cutover_approval, 'EXPLICIT_USER_REPLACEMENT_APPROVAL_REQUIRED')
    return {'stage': stage, 'eligible': not missing, 'missing': missing,
        'identity_gate': 'PER_QUERY_SHAPE', 'production_routing_changed': False}
