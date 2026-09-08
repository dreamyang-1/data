"""Current-request V2 plan-only boundary over Oagnet's pinned catalog protocol.

The trusted service constructs this session; model outputs only select candidate
handles. No HTTP format, production route, SQL execution or catalog write here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Protocol

from pydantic import BaseModel, TypeAdapter

from app.domain.models import ChatRequest, TrustedIdentity
from app.domain.state_identity import conversation_namespace
from .authorized_contract import (AuthorizedScopeContext, AuthorizedVersionMetadata,
    CatalogBindingEvidence, CatalogPinIdentity, DatasetBindingEvidence, ScopedArtifact, TaskBindingEvidence,
    SourceValueBindingEvidence, scoped_artifact_material,
    contract_digest, referenced_datasets, referenced_tasks, validate_authorized_refs)
from .enums import CatalogType, SemanticRole
from .models import BoundSemanticRef, SnapshotContext
from .pipeline import (CurrentTurnParser, LogicalPlanCompiler, collect_bound_refs,
    TurnResolver, compile_executable_plan)


class PinnedCatalogProtocol(Protocol):
    @property
    def identity(self) -> dict: ...
    def get_by_where(self, where: dict) -> list: ...
    def entity_value_lookup_fields(self) -> list[str]: ...
    def entity_value_source(self, attribute_record_id: str, **kwargs) -> dict: ...
    def lookup_entity_values(self, attribute_record_id: str, value: str, **kwargs) -> dict: ...
    def finish(self) -> dict: ...


class CatalogProvider(Protocol):
    def pin(self, semantic_model_id: int, business_domain_ids=()) -> PinnedCatalogProtocol: ...


# Type/role compatibility is deterministic catalog interpretation, not a lexical
# classifier or a user grant. Unknown record families/roles remain unsupported.
RECORD_TYPES = {
    'metric': (CatalogType.METRIC, 'metric_code', 'metric_name', {'MEASURE', 'ORDER_BY', 'COMPARISON_BASELINE'}),
    'entity': (CatalogType.ENTITY, 'entity_code', 'entity_name', {'SOURCE_ENTITY', 'TARGET_ENTITY', 'SUBJECT_ENTITY', 'RELATION_TARGET'}),
    'attribute': (CatalogType.ATTRIBUTE, 'attr_code', 'attr_name', {'FILTER_FIELD', 'PROJECTION_FIELD', 'TIME_FIELD', 'ORDER_BY'}),
    'dimension': (CatalogType.DIMENSION, 'dim_code', 'dim_name', {'GROUP_BY', 'FILTER_FIELD', 'PROJECTION_FIELD', 'TIME_FIELD', 'ORDER_BY'}),
    'scoped_dimension': (CatalogType.DIMENSION, 'dim_code', 'dim_name', {'GROUP_BY', 'FILTER_FIELD', 'PROJECTION_FIELD', 'TIME_FIELD', 'ORDER_BY'}),
    'enum': (CatalogType.ENTITY_VALUE, 'code', 'name', {'FILTER_VALUE'}),
    'scoped_enum': (CatalogType.ENTITY_VALUE, 'code', 'name', {'FILTER_VALUE'}),
    'relation': (CatalogType.RELATION, 'relation_code', 'relation_name', {'RELATIONSHIP', 'RELATION_TARGET'}),
    'table': (CatalogType.PHYSICAL_TABLE, 'table_name', 'table_name', {'SOURCE_ENTITY', 'TARGET_ENTITY', 'SUBJECT_ENTITY'}),
    'field': (CatalogType.PHYSICAL_COLUMN, 'field_name', 'field_name', {'FILTER_FIELD', 'PROJECTION_FIELD', 'TIME_FIELD', 'ORDER_BY'}),
}


class ScopedPlanSession:
    def __init__(self, request: ChatRequest, identity: TrustedIdentity, catalog: CatalogProvider):
        # Revalidate copies: mutated request objects/history cannot mint scope.
        self._request = ChatRequest.model_validate(request.model_dump())
        self._identity = TrustedIdentity.model_validate(identity.model_dump())
        scope = self._request.authorized_semantic_scope
        if len(scope.business_domain_ids) > 1:
            raise ValueError('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED')
        self._pin = catalog.pin(scope.semantic_model_id, scope.business_domain_ids)
        receipt = self._pin.identity
        expected = {'semantic_model_id': scope.semantic_model_id,
            'business_domain_ids': list(scope.business_domain_ids), 'scope_mode': scope.scope_mode}
        if receipt.get('scope') != expected:
            raise ValueError('CATALOG_SCOPE_MISMATCH')
        self._context = AuthorizedScopeContext(authorized_scope=scope,
            state_namespace=contract_digest({'conversation': conversation_namespace(self._request.application_id, self._request.conversation_id),
                'tenant': self._identity.tenant_id, 'user': self._identity.user_id}),
            catalog_pin=CatalogPinIdentity.model_validate({k: receipt[k] for k in CatalogPinIdentity.model_fields}))
        self._snapshot = SnapshotContext(semantic_model_id=str(scope.semantic_model_id),
            business_domain_ids=[str(d) for d in scope.business_domain_ids],
            database_id=str(scope.database_id) if scope.database_id is not None else None,
            knowledge_base_names=list(scope.knowledge_base_names),
            catalog_version=receipt['catalog_version'], semantic_model_version=receipt['catalog_version'],
            catalog_publish_id=receipt['catalog_publish_id'], vector_index_version=receipt['vector_index_version'])
        self._rows = {}
        self._bindings = {}
        self._value_candidates = {}
        self._source_observations = {}
        self._archived_source_bindings = {}
        self._datasets = {}
        self._tasks = {}
        self._finished = False
        self._resolutions = set()

    @property
    def context(self):
        return self._context

    def _check(self):
        if self._finished:
            raise ValueError('CATALOG_PIN_ALREADY_FINISHED')
        current = self._pin.identity
        if any(current.get(k) != getattr(self._context.catalog_pin, k) for k in CatalogPinIdentity.model_fields):
            raise ValueError('CATALOG_PUBLICATION_CHANGED_DURING_READ')

    def candidates(self, catalog_type: CatalogType):
        self._check()
        kinds = [k for k, definition in RECORD_TYPES.items() if definition[0] == catalog_type]
        rows = self._pin.get_by_where({'type': {'$in': kinds}})
        available = []
        for row in rows:
            metadata = row.metadata
            scope = self._context.authorized_scope
            domain = metadata.get('business_domain_id')
            if (type(metadata.get('semantic_model_id')) is not int or metadata.get('semantic_model_id') != scope.semantic_model_id or type(domain) is not int
                    or domain != -1 and domain <= 0 or not scope.contains_domain(domain)):
                raise ValueError('CATALOG_SCOPE_MISMATCH')
            if metadata.get('type') not in kinds or not metadata.get('catalog_logical_id') or not metadata.get('catalog_record_hash'):
                raise ValueError('CATALOG_BINDING_EVIDENCE_MISSING')
            self._rows[row.id] = deepcopy(row)
            _, code_key, name_key, roles = RECORD_TYPES[metadata['type']]
            available.append({'candidate_id': row.id, 'catalog_type': catalog_type.value,
                'canonical_code': metadata.get(code_key), 'display_name': metadata.get(name_key),
                'supported_roles': sorted(roles)})
        return available

    def bind(self, candidate_id: str, role: SemanticRole, source_mention_ids=()):
        self._check()
        if candidate_id in self._value_candidates:
            from .source_value_binding import bind_value
            return bind_value(self, candidate_id, role, source_mention_ids)
        row = self._rows.get(candidate_id)
        if row is None:
            raise ValueError('SEMANTIC_RESOLUTION_FAILURE: unknown pinned candidate')
        meta = row.metadata
        kind, code_key, name_key, roles = RECORD_TYPES[meta['type']]
        if role not in roles:
            raise ValueError('SEMANTIC_RESOLUTION_FAILURE: incompatible catalog role')
        # A platform database ID needs the existing database-load mapping before
        # it can constrain physical data-source IDs. Never equate the two IDs.
        if kind in {CatalogType.PHYSICAL_TABLE, CatalogType.PHYSICAL_COLUMN} and self.context.authorized_scope.database_id is not None:
            raise ValueError('CATALOG_DATABASE_SCOPE_EVIDENCE_REQUIRED')
        domain = meta['business_domain_id']
        ref = BoundSemanticRef(catalog_type=kind, semantic_role=role,
            canonical_id=meta['catalog_logical_id'], canonical_code=meta[code_key], display_name=meta[name_key],
            catalog_version=self.context.catalog_pin.catalog_version,
            semantic_model_id=str(self.context.authorized_scope.semantic_model_id),
            business_domain_ids=() if domain == -1 else (str(domain),),
            source_mention_ids=tuple(source_mention_ids), resolution_source='PINNED_CATALOG')
        proof = CatalogBindingEvidence(ref=ref, record_id=row.id, record_hash=meta['catalog_record_hash'],
            context_fingerprint=self.context.fingerprint())
        self._bindings[contract_digest(ref.model_dump(mode='json'))] = proof
        return ref

    def lookup_source_values(self, attribute_id, value, *, implicit=False):
        from .source_value_binding import lookup_values
        return lookup_values(self, attribute_id, value, implicit=implicit)

    def restore(self, artifact: ScopedArtifact, *, kind, defer_source_values=False):
        self._check()
        artifact = ScopedArtifact.model_validate(artifact.model_dump(mode='json'))
        if artifact.kind != kind or artifact.context != self.context:
            raise ValueError('SCOPED_STATE_REUSE_REJECTED')
        from .source_value_binding import restore_values
        if defer_source_values and kind not in {'CONVERSATION', 'TASK_FRAME', 'LAST_REQUEST'}:
            raise ValueError('V2_SOURCE_VALUE_DEFER_NOT_ALLOWED')
        restore_values(self, artifact.source_value_bindings, defer=defer_source_values)
        if kind == 'CONVERSATION':
            value=artifact.payload
            if not isinstance(value,dict) or any(value.get(k)!=v for k,v in self._state_identity().items()):
                raise ValueError('SCOPED_STATE_REUSE_REJECTED')
        for ref in self._decoded_refs(artifact.payload):
            if ref.resolution_source == 'VERIFIED_SOURCE_EXACT_LOOKUP':
                proof = self._bindings.get(contract_digest(ref.model_dump(mode='json')))
                if proof is None and defer_source_values:
                    proof = self._archived_source_bindings.get(contract_digest(ref.model_dump(mode='json')))
                if not isinstance(proof, SourceValueBindingEvidence):
                    raise ValueError('V2_SOURCE_VALUE_RESTORE_EVIDENCE_REQUIRED')
                continue
            candidates = self.candidates(ref.catalog_type)
            candidate = next((c for c in candidates if self._rows[c['candidate_id']].metadata['catalog_logical_id']==ref.canonical_id),None)
            if candidate is None or self.bind(candidate['candidate_id'],ref.semantic_role,ref.source_mention_ids)!=ref:
                raise ValueError('SCOPED_STATE_BINDING_MISMATCH')
        if kind == 'DATASET':
            from .state_machine import DatasetState
            dataset = DatasetState.model_validate(artifact.payload)
            if dataset.status != 'VALID':
                raise ValueError('SCOPED_DATASET_INVALIDATED')
            dataset_id = dataset.dataset_id
            self._datasets[dataset_id] = DatasetBindingEvidence(dataset_id=dataset_id,
                source_task_id=dataset.task_id,source_task_version=dataset.task_version,
                artifact_digest=artifact.payload_digest, context_fingerprint=self.context.fingerprint())
        if kind in {'TASK_FRAME', 'CONVERSATION'}:
            from .state_machine import TaskState, ConversationState
            tasks = [TaskState.model_validate(artifact.payload)] if kind=='TASK_FRAME' else list(ConversationState.model_validate(artifact.payload).tasks.values())
            for task in tasks:
                for version in task.versions:
                    self._tasks[(task.task_id,version.version)] = TaskBindingEvidence(task_id=task.task_id,
                        task_version=version.version,artifact_digest=artifact.payload_digest,
                        context_fingerprint=self.context.fingerprint())
        return deepcopy(artifact.model_dump(mode='json')['payload'])

    def _state_identity(self):
        return dict(conversation_id=self._request.conversation_id,application_id=self._request.application_id,
                    tenant_id=self._identity.tenant_id,user_id=self._identity.user_id)

    def seal(self, *, kind, payload):
        if not self._finished:
            raise ValueError('CATALOG_ACCEPTANCE_REQUIRED_BEFORE_STATE_WRITE')
        if kind in {'DATASET', 'RESULT_ARTIFACT'}:
            raise ValueError('PLAN_ONLY_CANNOT_CREATE_EXECUTED_RESULT')
        value = payload.model_dump(mode='json') if isinstance(payload, BaseModel) else deepcopy(payload)
        if kind=='CONVERSATION' and (not isinstance(value,dict) or any(value.get(k)!=v for k,v in self._state_identity().items())):
            raise ValueError('SCOPED_STATE_REUSE_REJECTED')
        refs = self._decoded_refs(value)
        self._require_refs(refs, allow_archive=kind in {'CONVERSATION','TASK_FRAME'})
        from .source_value_binding import source_proofs_for
        source_values = source_proofs_for(self, refs)
        return ScopedArtifact(kind=kind, context=self.context, payload=value, source_value_bindings=source_values,
            payload_digest=contract_digest(scoped_artifact_material(value, source_values)))

    @staticmethod
    def _decoded_refs(value):
        if isinstance(value, dict):
            if {'canonical_id','catalog_type','semantic_role','semantic_model_id','catalog_version','business_domain_ids'} <= value.keys():
                return (BoundSemanticRef.model_validate(value),)
            return tuple(r for item in value.values() for r in ScopedPlanSession._decoded_refs(item))
        if isinstance(value, (list,tuple)):
            return tuple(r for item in value for r in ScopedPlanSession._decoded_refs(item))
        return ()

    def _require_refs(self, refs, *, allow_archive=False):
        proofs = []
        for ref in refs:
            proof = self._bindings.get(contract_digest(ref.model_dump(mode='json')))
            key = contract_digest(ref.model_dump(mode='json'))
            if proof is None and key in self._archived_source_bindings:
                proof = self._archived_source_bindings[key]
                if not allow_archive:
                    from .source_value_binding import restore_values
                    restore_values(self, (proof,))
                    proof = self._bindings.get(key)
            if proof is None:
                raise ValueError('PINNED_CATALOG_BINDING_REQUIRED')
            proofs.append(proof)
            if isinstance(proof, SourceValueBindingEvidence):
                field = self._bindings.get(contract_digest(proof.field_ref.model_dump(mode='json')))
                if not isinstance(field, CatalogBindingEvidence):
                    raise ValueError('SOURCE_VALUE_FIELD_CATALOG_EVIDENCE_REQUIRED')
                proofs.append(field)
        return proofs

    def cache_key(self, semantic_fingerprint):
        self._check()
        return contract_digest({'context':self.context.fingerprint(), 'semantics':semantic_fingerprint})

    def resolve_turn(self, *, parsed, task_patch, semantic_resolution, state=None, historical_task_id=None, pending_option_id=None):
        """All state reaching this new resolver path must pass current scope."""
        from .state_machine import ConversationState
        self._check()
        parse = CurrentTurnParser.parse(text=self._request.question, turn_id=self._request.message_id,
            text_ref=self._request.message_id, parsed=parsed)
        if state is None:
            current = ConversationState(conversation_id=self._request.conversation_id,
                application_id=self._request.application_id,tenant_id=self._identity.tenant_id,
                user_id=self._identity.user_id,state_version=0)
        else:
            current = ConversationState.model_validate(self.restore(state, kind='CONVERSATION', defer_source_values=True))
            if (current.conversation_id != self._request.conversation_id or current.application_id != self._request.application_id
                    or current.tenant_id != self._identity.tenant_id or current.user_id != self._identity.user_id):
                raise ValueError('SCOPED_STATE_REUSE_REJECTED')
        from .registries import SlotDefinitionRegistry
        from .slot_reducer import TaskPatch
        task_patch = TaskPatch.model_validate(task_patch.model_dump(mode='json'))
        for group in ('sets','adds','replacements','inherit_requests'):
            for operation in getattr(task_patch,group):
                value = operation.new_value
                if group=='adds' and not isinstance(value,list):
                    value=[value]
                typed = TypeAdapter(SlotDefinitionRegistry.get(operation.slot_path).value_type).validate_python(value)
                self._require_refs(collect_bound_refs(typed))
                if any(d not in self._datasets for d in referenced_datasets(typed)):
                    raise ValueError('SCOPED_DATASET_RESTORE_REQUIRED')
                if any(t not in self._tasks for t in referenced_tasks(typed)):
                    raise ValueError('SCOPED_TASK_RESTORE_REQUIRED')
                validate_authorized_refs(typed,self._snapshot,self.context,
                    tuple([*self._bindings.values(),*self._datasets.values(),*self._tasks.values()]))
        if pending_option_id is not None:
            from .pending_recognition import selected_option
            from .pipeline import TurnResolutionResult, TurnReferentialCompleteness
            from .models import ProceedDecision
            from .slot_reducer import apply_task_patch, semantic_fingerprint
            pending = current.pending
            option = selected_option(pending, self._request.question)
            if parse.topic_shift_signals or option is None or option.option_id != pending_option_id:
                raise ValueError('V2_PENDING_ANSWER_NOT_ADMISSIBLE')
            task = current.tasks[pending.task_id]
            active = next(v for v in task.versions if v.version == task.active_version)
            if task_patch.base_task_version != task.active_version:
                raise ValueError('V2_PENDING_TASK_VERSION_MISMATCH')
            reduced = apply_task_patch(active.semantics, task_patch, clear_barriers=task.clear_barriers)
            blocker = next(b for b in pending.blockers if b.blocker_id == pending.active_blocker_id)
            actual = getattr(reduced.semantics, blocker.plan_path)
            values = actual if isinstance(actual,(list,tuple)) else [actual]
            from .models import clarification_option_value
            if option.filter_choice is not None and blocker.plan_path != 'filter_expression':
                raise ValueError('V2_PENDING_FILTER_CHOICE_SLOT_MISMATCH')
            expected = clarification_option_value(option)
            if not any(semantic_fingerprint(v)==semantic_fingerprint(expected) for v in values):
                raise ValueError('V2_PENDING_OPTION_NOT_APPLIED')
            resolution = TurnResolutionResult(dialogue_act='ANSWER_CLARIFICATION',
                target_task_id=task.task_id,target_topic_id=task.topic_id,task_patch=task_patch,
                semantic_resolution=semantic_resolution,decision=ProceedDecision(),
                referential_completeness=TurnReferentialCompleteness(relation='CURRENT_TASK',depends_on_history=True))
        else:
            resolution = TurnResolver.resolve(parse, state=current, task_patch=task_patch,
                semantic_resolution=semantic_resolution, historical_task_id=historical_task_id)
        self._resolutions.add(contract_digest({'parse':parse.model_dump(mode='json'),
                                               'resolution':resolution.model_dump(mode='json')}))
        return resolution

    def compile(self, *, parsed, resolution, payload, service_route, analysis_goals,
                task_version, versions: AuthorizedVersionMetadata, delivery_spec=None):
        self._check()
        parse = CurrentTurnParser.parse(text=self._request.question, turn_id=self._request.message_id,
            text_ref=self._request.message_id, parsed=parsed)
        if contract_digest({'parse':parse.model_dump(mode='json'),
                            'resolution':resolution.model_dump(mode='json')}) not in self._resolutions:
            raise ValueError('CURRENT_SCOPED_TURN_RESOLUTION_REQUIRED')
        from .catalog_plans import validate_catalog_payload
        validate_catalog_payload(self, payload)
        proofs = self._require_refs(collect_bound_refs(payload))
        for dataset_id in referenced_datasets(payload):
            proof = self._datasets.get(dataset_id)
            if proof is None:
                raise ValueError('SCOPED_DATASET_RESTORE_REQUIRED')
            proofs.append(proof)
        for task in referenced_tasks(payload):
            proof = self._tasks.get(task)
            if proof is None:
                raise ValueError('SCOPED_TASK_RESTORE_REQUIRED')
            proofs.append(proof)
        plan = LogicalPlanCompiler.compile(parse=parse, resolution=resolution, payload=payload,
            service_route=service_route, analysis_goals=analysis_goals, task_version=task_version,
            permission=self.context, snapshot=self._snapshot, authorizations=tuple(proofs),
            version_metadata=versions, delivery_spec=delivery_spec)
        executable = compile_executable_plan(plan)
        # No plan/state can leave this session until full source + inventory +
        # activation acceptance succeeds. Errors are system failures, not asks.
        self.accept_catalog()
        return executable

    def accept_catalog(self):
        """Accept plan or clarification artifacts only after full pinned read-back."""
        self._check()
        receipt = self._pin.finish()
        if any(receipt.get(k) != getattr(self.context.catalog_pin, k) for k in CatalogPinIdentity.model_fields):
            raise ValueError('CATALOG_ACCEPTANCE_IDENTITY_MISMATCH')
        self._finished = True
        return receipt
