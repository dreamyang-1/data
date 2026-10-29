"""Observe the existing lower_asl2 -> pinned translator seam on accepted plans.

This is explicitly a supplemental diagnostic: RawTurnPlanner does not call this
seam. Never claim it was part of the original runtime execution or execute SQL.
"""
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
import json
import sys
from unittest.mock import patch

from app.domain.models import TrustedIdentity
from app.semantic_v2.authorized_contract import ScopedArtifact
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from app.semantic_v2.asl2 import lower_asl2
from tools.cutover.harness_fixtures import request_for
from tools.cutover.run_raw_transition_benchmark import frozen_publication
from tools.cutover.harness_replay import deny_external_calls
from tools.cutover.frozen_source_values import FrozenSourceValues
from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_observation import json_value


def observe_dry_plan(capture,case,catalog,raw_snapshot,*,source_observations=None,source_observations_hash=None):
    if not capture.get('result') or not capture['result'].get('plan'):
        return {'status':'NOT_APPLICABLE_NO_ACCEPTED_PLAN','same_raw_runtime_entry':False}
    result=capture['result'];logical=result['plan']['logical_plan']
    root=Path(__file__).resolve().parents[2]
    sql=root/'sql-translator' if (root/'sql-translator').is_dir() else root.parent/'sql-translator'
    oagnet=root/'Oagnet' if (root/'Oagnet').is_dir() else root.parent/'Oagnet'
    with deny_external_calls() as counters,patch.object(sys,'path',[*sys.path,str(sql),str(oagnet)]),ExitStack() as stack:
        publication=frozen_publication(raw_snapshot,catalog,[],native_source_values=source_observations is not None,
            recorded_activation_id=result['next_state']['context']['catalog_pin']['activation_id'])
        if source_observations is not None:
            values=FrozenSourceValues(source_observations,catalog=catalog,snapshot=json.loads(raw_snapshot),
                expected_hash=source_observations_hash,allow_synthetic=capture['mode']=='SCRIPTED_MODEL_PIPELINE')
            import catalog_value_sources
            stack.enter_context(patch.object(catalog_value_sources,'observe',values.observe))
            stack.enter_context(patch.object(catalog_value_sources,'observe_probe',values.observe_probe))
        request=request_for(case,message_id='turn-'+str(capture['turn_index']))
        session=ScopedPlanSession(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
        session.restore(ScopedArtifact.model_validate(result['next_state']),kind='CONVERSATION')
        restored=session.restore(ScopedArtifact.model_validate(result['plan_state']),kind='LAST_REQUEST')
        if digest(restored)!=digest(logical):raise ValueError('DRY_PLAN_INPUT_IDENTITY_MISMATCH')
        lowering=lower_asl2(session,AuthorizedLogicalPlan.model_validate(restored))
        native=None
        if lowering.asl is not None:
            from pinned_catalog import translate_pinned_catalog
            from semantic_scope import RequestScope
            policy={}
            if lowering.ordering_contract:policy['ordering_contract']=lowering.ordering_contract
            if lowering.filter_contract:policy['filter_contract']=lowering.filter_contract
            if lowering.asl.get('filters') or lowering.filter_contract:policy['parameterized']=True
            native=translate_pinned_catalog(session._pin,RequestScope.from_request(
                {'authorized_semantic_scope':request.authorized_semantic_scope.model_dump(mode='json')}),lowering.asl,**policy)
        if native and native.get('success'):
            # Native translation owns finish() on success. Validate its exact
            # receipt instead of attempting to finish the consumed pin again.
            expected=session.context.catalog_pin.model_dump(mode='json')
            receipt=native.get('catalog_pin') or {}
            if any(receipt.get(k)!=v for k,v in expected.items()):
                raise ValueError('DRY_PLAN_CATALOG_ACCEPTANCE_IDENTITY_MISMATCH')
        else:
            session.accept_catalog()
    lowered=json_value(asdict(lowering))
    return {'status':'OBSERVED_SUPPLEMENTAL_NATIVE_SEAM','same_raw_runtime_entry':False,
        'dry_plan_input_hash':digest(logical),'lowering':lowered,
        'native_sql_planning':native,'external_call_attempts':counters,'SQL_executed':False,
        'public_receipt':{'case_id':case['case_id'],'turn_index':capture['turn_index'],
            'logical_input_hash':digest(logical),'lowering_status':lowering.status,
            'lowering_hash':digest(lowered),'native_result_hash':digest(native),
            'native_success':native.get('success') if native else None,'same_raw_runtime_entry':False,'SQL_executed':False}}
