"""Private, scoped source observations for an isolated evaluation, never runtime authority.

A digest detects fixture drift; it is not a signature or a production freshness
proof. The caller supplies the trusted read-only capture and its expected hash.
Native pin/field/policy/binding/finish checks still execute against this fixture.
"""
from copy import deepcopy
from datetime import datetime

from tools.cutover.evaluation_contract import digest


FORMAT = 'FROZEN_SOURCE_VALUE_OBSERVATIONS_V1'


class SourceValuesUnavailable(RuntimeError):
    pass


def seal(entries, *, catalog, capture_mode):
    material = dict(format=FORMAT, scope=catalog['scope'], catalog_ref=catalog['artifact_hash'],
        catalog_version=catalog['catalog_version'], snapshot_sha256=catalog['source_snapshot_sha256'],
        capture_mode=capture_mode, entries=entries)
    return {**material, 'artifact_hash':digest(material)}


class FrozenSourceValues:
    def __init__(self, bundle, *, catalog, snapshot, expected_hash, allow_synthetic=False):
        from mysql_tool import normalize_catalog_text
        self.normalize = normalize_catalog_text
        self.calls = []
        self.entries = {}
        required = {'format','scope','catalog_ref','catalog_version','snapshot_sha256','capture_mode','entries','artifact_hash'}
        if not isinstance(bundle, dict) or set(bundle) != required:
            raise ValueError('FROZEN_VALUES_FORMAT_INVALID')
        material = {k:v for k,v in bundle.items() if k != 'artifact_hash'}
        if not expected_hash or bundle['artifact_hash'] != expected_hash or digest(material) != expected_hash:
            raise ValueError('FROZEN_VALUES_HASH_MISMATCH')
        if (bundle['format'] != FORMAT or bundle['scope'] != catalog['scope']
                or bundle['catalog_ref'] != catalog['artifact_hash']
                or bundle['catalog_version'] != catalog['catalog_version']
                or bundle['snapshot_sha256'] != catalog['source_snapshot_sha256']):
            raise ValueError('FROZEN_VALUES_CATALOG_MISMATCH')
        if bundle['capture_mode'] not in ({'READ_ONLY_SOURCE','SYNTHETIC_TEST'} if allow_synthetic else {'READ_ONLY_SOURCE'}):
            raise ValueError('FROZEN_VALUES_PROVENANCE_INVALID')
        entries = bundle['entries']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
            raise ValueError('FROZEN_VALUES_ENTRIES_INVALID')
        fields = snapshot.get('physical_catalog', {}).get('entity_value_sources', {}).get('fields', [])
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'query','limit','observation'}:
                raise ValueError('FROZEN_VALUES_ENTRY_INVALID')
            value, limit, observation = entry['query'], entry['limit'], entry['observation']
            if (not isinstance(value, str) or not 1 <= len(value) <= 256 or value != self.normalize(value)
                    or type(limit) is not int or not 1 <= limit <= 32):
                raise ValueError('FROZEN_VALUES_QUERY_INVALID')
            expected_keys = {'source','scope','field','match_mode','query_hash','values','complete','observation_hash','observed_at'}
            if not isinstance(observation, dict) or set(observation) != expected_keys:
                raise ValueError('FROZEN_VALUES_OBSERVATION_INVALID')
            values = observation['values']
            if (observation['source'] != 'VERIFIED_SOURCE_EXACT_LOOKUP'
                    or observation['scope'] != catalog['scope'] or observation['field'] not in fields
                    or observation['match_mode'] != 'EXACT_NORMALIZED' or observation['query_hash'] != digest(value)
                    or not isinstance(values, list) or len(values) > limit
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 1024 or self.normalize(v) != value for v in values)
                    or values != sorted(set(values)) or type(observation['complete']) is not bool
                    or (not observation['complete'] and len(values) != limit)):
                raise ValueError('FROZEN_VALUES_OBSERVATION_INVALID')
            observed = datetime.fromisoformat(observation['observed_at'])
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError('FROZEN_VALUES_TIMESTAMP_INVALID')
            proof = {k:v for k,v in observation.items() if k not in {'observation_hash','observed_at'}}
            if digest(proof) != observation['observation_hash']:
                raise ValueError('FROZEN_VALUES_OBSERVATION_HASH_MISMATCH')
            key = self.key(observation['scope'], observation['field'], value, limit)
            if key in self.entries:
                raise ValueError('FROZEN_VALUES_DUPLICATE_QUERY')
            self.entries[key] = deepcopy(observation)
        self.artifact_hash = expected_hash
        self.capture_mode = bundle['capture_mode']

    def key(self, scope, field, value, limit):
        return digest([scope, field, self.normalize(value), limit])

    def observe(self, scope, field, value, limit):
        # No fallback to a live source, another field, a wider scope or a guessed value.
        key = self.key(scope, field, value, limit)
        observation = self.entries.get(key)
        self.calls.append({'query_fingerprint':key, 'status':'REPLAYED' if observation is not None else 'MISSING'})
        if observation is None:
            raise SourceValuesUnavailable('FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED')
        return deepcopy(observation)
