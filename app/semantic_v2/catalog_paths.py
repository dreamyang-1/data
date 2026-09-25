"""Ordered, declared relation paths and occurrence ownership over a current pin."""
from . import models as m
from .authorized_contract import contract_digest, contract_objects
from .catalog_plans import _row, relationship, same_entity
from .recognition_client import RecognitionFailure
from .slot_reducer import semantic_fingerprint


def relationship_path(session, directed_refs):
    if not 1 <= len(directed_refs) <= 8:
        raise RecognitionFailure('V2_RELATION_PATH_LENGTH_INVALID')
    nodes, hops, prefix = [], [], []
    for ref, direction in directed_refs:
        edge = relationship(session, ref, direction)
        if nodes and not same_entity(nodes[-1].entity_ref, edge.source_ref):
            raise RecognitionFailure('CATALOG_RELATION_PATH_DISCONNECTED')
        if not nodes:
            prefix = [session.context.catalog_pin.model_dump(mode='json'), edge.source_ref.canonical_id]
            nodes.append(m.RelationshipPathNode(entity_alias='occurrence:' + contract_digest(prefix), entity_ref=edge.source_ref))
        prefix.append([ref.canonical_id, direction, edge.target_ref.canonical_id])
        target = m.RelationshipPathNode(entity_alias='occurrence:' + contract_digest(prefix), entity_ref=edge.target_ref)
        hops.append(m.RelationshipPathHop(relation_ref=ref, source_alias=nodes[-1].entity_alias,
            target_alias=target.entity_alias, direction=direction, cardinality=edge.cardinality))
        nodes.append(target)
    return m.RelationshipPathSpec(nodes=nodes, hops=hops)


def validate_path(session, path):
    expected = relationship_path(session, [(h.relation_ref, h.direction) for h in path.hops])
    if semantic_fingerprint(path) != semantic_fingerprint(expected):
        raise RecognitionFailure('CATALOG_RELATION_PATH_MISMATCH')


def resolve_alias(selector, path):
    if not isinstance(path, m.RelationshipPathSpec):
        raise RecognitionFailure('V2_ENTITY_ALIAS_REQUIRES_PATH')
    offered = {n.entity_alias: n.entity_alias for n in path.nodes}
    offered.update({'node:' + str(i): n.entity_alias for i, n in enumerate(path.nodes)})
    if not isinstance(selector, str) or selector not in offered:
        raise RecognitionFailure('V2_ENTITY_ALIAS_NOT_IN_CURRENT_PATH')
    return offered[selector]


def path_projection(projection, path):
    """A governed display refers to the terminal occurrence, including self paths."""
    alias = path.nodes[-1].entity_alias
    return m.ProjectionSpec(mode=projection.mode,
        default_display_policy_id=projection.default_display_policy_id,
        items=[m.AliasedProjectionItem.model_validate(dict(item.model_dump(), entity_alias=alias,
            output_field_id='projection:' + contract_digest([item.ref.canonical_id, alias]))) for item in projection.items])


def validate_occurrence_fields(session, payload):
    path = getattr(payload, 'relationship_spec', None)
    aliases = [v for v in contract_objects(payload) if isinstance(v, (m.AliasedPredicate, m.AliasedProjectionItem))]
    if not isinstance(path, m.RelationshipPathSpec):
        if aliases:
            raise RecognitionFailure('V2_ENTITY_ALIAS_REQUIRES_PATH')
        return
    owners = {}
    for node in path.nodes:
        meta = _row(session, node.entity_ref)
        owners[node.entity_alias] = (meta['entity_code'], meta['business_domain_id'])
    for field in contract_objects([payload.filters, payload.projection_spec]):
        if not isinstance(field, (m.Predicate, m.ProjectionItem)):
            continue
        ref = field.field_ref if isinstance(field, m.Predicate) else field.ref
        if not isinstance(ref, m.BoundSemanticRef) or ref.catalog_type != 'ATTRIBUTE':
            raise RecognitionFailure('CATALOG_PATH_FIELD_OWNERSHIP_UNSUPPORTED')
        meta = _row(session, ref)
        matches = [alias for alias, owner in owners.items() if owner == (meta.get('parent'), meta['business_domain_id'])]
        alias = getattr(field, 'entity_alias', None)
        if alias is not None:
            if alias not in matches:
                raise RecognitionFailure('CATALOG_PATH_FIELD_OCCURRENCE_MISMATCH')
        elif len(matches) != 1:
            raise RecognitionFailure('CATALOG_PATH_FIELD_OCCURRENCE_UNRESOLVED')
