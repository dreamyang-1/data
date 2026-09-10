"""Versioned targeted source observations for offline replay, never live authority."""
from copy import deepcopy
from datetime import datetime

from tools.cutover.evaluation_contract import digest


class FrozenTargetedCandidates:
    def __init__(self, bundle, *, catalog, snapshot, expected_hash):
        if bundle['artifact_hash'] != expected_hash or digest({k:v for k,v in bundle.items() if k!='artifact_hash'}) != expected_hash:
            raise ValueError('FROZEN_TARGETED_HASH_MISMATCH')
        if (bundle['format']!='TARGETED_SOURCE_OBSERVATIONS_V1' or bundle['scope']!=catalog['scope']
                or bundle['catalog_version']!=catalog['catalog_version'] or bundle['catalog_ref']!=catalog['artifact_hash']
                or bundle['snapshot_sha256']!=catalog['source_snapshot_sha256'] or bundle['capture_mode']!='READ_ONLY_SOURCE'):
            raise ValueError('FROZEN_TARGETED_CATALOG_MISMATCH')
        fields=snapshot['physical_catalog']['entity_value_sources']['fields']
        self.entries={}; self.calls=[]
        for entry in bundle['entries']:
            query,limit,receipt=entry['query'],entry['limit'],entry['observation']
            if (type(limit) is not int or not 1<=limit<=8 or not isinstance(query,str) or not 1<=len(query)<=256
                    or receipt['scope']!=catalog['scope'] or receipt['field'] not in fields
                    or receipt['source']!='VERIFIED_SOURCE_TARGETED_CANDIDATES' or receipt['match_mode']!='PREFIX_CANDIDATE_DISCOVERY'
                    or receipt['query_hash']!=digest(query) or type(receipt['complete']) is not bool
                    or not isinstance(receipt['values'],list) or len(receipt['values'])>limit
                    or any(not isinstance(v,str) or not v.strip() or len(v)>256 for v in receipt['values'])
                    or receipt['values']!=sorted(set(receipt['values'])) or (not receipt['complete'] and receipt['values'])
                    or datetime.fromisoformat(receipt['observed_at']).utcoffset() is None
                    or receipt['observation_hash']!=digest({k:v for k,v in receipt.items() if k not in {'observation_hash','observed_at'}})):
                raise ValueError('FROZEN_TARGETED_RECEIPT_INVALID')
            key=digest([receipt['scope'],receipt['field'],query,limit])
            if key in self.entries: raise ValueError('FROZEN_TARGETED_DUPLICATE_QUERY')
            self.entries[key]=deepcopy(receipt)

    def observe_candidates(self, scope, field, query, limit=8):
        from mysql_tool import normalize_catalog_text
        key=digest([scope,field,normalize_catalog_text(query),limit]); receipt=self.entries.get(key)
        self.calls.append(dict(query_fingerprint=key,status='REPLAYED' if receipt is not None else 'MISSING'))
        if receipt is None: raise RuntimeError('FROZEN_TARGETED_SOURCE_OBSERVATION_REQUIRED')
        return deepcopy(receipt)
