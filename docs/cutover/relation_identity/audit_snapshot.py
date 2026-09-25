"""Offline declaration inventory, not an entity-identity decision or scope grant.

Usage: python audit_snapshot.py PRIVATE_SNAPSHOT.json RECEIPT.json
Only hashes/counts from the supplied sealed snapshot are written to the receipt.
No network, environment loading, catalog mutation or business data read occurs.
"""
import hashlib
import json
from pathlib import Path
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def inventory(snapshot):
    if (snapshot.get('contract_version') != 'catalog-release-v1'
            or snapshot.get('catalog_version') != digest({k:v for k,v in snapshot.items() if k != 'catalog_version'})):
        raise ValueError('CATALOG_DIGEST_INVALID')
    scope = snapshot['scope']; model = scope['semantic_model_id']; domains = scope['business_domain_ids']
    if (type(model) is not int or model <= 0 or not isinstance(domains, list)
            or len(domains) > 1 or any(type(v) is not int or v <= 0 for v in domains)
            or scope['scope_mode'] != ('EXPLICIT_DOMAINS' if domains else 'MODEL_WIDE')):
        raise ValueError('CATALOG_SCOPE_INVALID')
    rows = []; seen = set()
    for doc in snapshot['documents']:
        domain = (doc.get('business_domain') or {}).get('id')
        if doc['semantic_model']['id'] != model or (domains and domain not in domains):
            raise ValueError('CATALOG_SCOPE_MISMATCH')
        for entity in doc['entities']:
            owner = (domain, entity['entity_code'])
            if owner in seen or entity['business_domain'] != domain:
                raise ValueError('CATALOG_ENTITY_OWNER_INVALID')
            seen.add(owner)
            attributes = entity.get('attributes') or []
            primary = [a for a in attributes if a.get('is_primary_key') is True]
            unique = [a for a in attributes if a.get('is_unique') is True]
            malformed = sum(a.get(k) is not None and type(a[k]) is not bool
                for a in attributes for k in ('is_primary_key','is_unique'))
            declared = entity.get('primary_key') is not None or bool(primary or unique)
            rows.append(dict(entity_definition_hash=digest(entity),
                attribute_count=len(attributes),primary_attribute_count=len(primary),
                unique_attribute_count=len(unique),entity_primary_key_present=entity.get('primary_key') is not None,
                non_boolean_key_flags=malformed,
                status='DECLARATIONS_REQUIRE_CONTRACT_REVIEW' if declared or malformed else 'IDENTITY_DECLARATION_MISSING'))
    return dict(scope=scope,catalog_version=snapshot['catalog_version'],entities=len(rows),
        attributes=sum(r['attribute_count'] for r in rows),
        entities_without_identity_declaration=sum(r['status']=='IDENTITY_DECLARATION_MISSING' for r in rows),
        entities_requiring_declaration_review=sum(r['status']=='DECLARATIONS_REQUIRE_CONTRACT_REVIEW' for r in rows),
        non_boolean_key_flags=sum(r['non_boolean_key_flags'] for r in rows),
        entity_identity_contract_proven=False,rows=rows,
        limitation='Presence is an inventory fact only. Do not choose a key, infer uniqueness from names, merge composite fields, or infer business identity from physical row identity.')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: audit_snapshot.py PRIVATE_SNAPSHOT.json RECEIPT.json')
    source, target = map(Path, sys.argv[1:])
    if target.exists():
        raise SystemExit('Refusing to overwrite existing evidence')
    receipt = inventory(json.loads(source.read_text(encoding='utf-8')))
    target.write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k != 'rows'}))
