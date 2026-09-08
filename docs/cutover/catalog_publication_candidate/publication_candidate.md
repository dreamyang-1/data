# Current reviewed publication candidate — not executed

Scope: **semantic_model_id 81 / business_domain_ids [205]**.
Status: **BLOCKED_REGISTRY_DURABILITY_AND_PENDING_OPERATIONAL_AUTHORIZATION**.
This replaces the historical PR #20 candidate. The source/target/embedding
identities and planned write inventory are in `publication_candidate.json`.

Fresh metadata/native reads at 2026-09-08 16:43–16:45 UTC confirm:

- Catalog version `b3106dea38f62738f5f2551e4667ceee6a0d3ba96f5e88ddd90e7ef746b88506`.
- Source identity `88863043a8190d222028c7597133f73fce6afdd84f830836da2532e05cedb5e1`.
- Target identity `763d0302d6278371ea2cf3c5372103cf8d823ea40411d9d27f191a511a010ef5`.
- Configured embedding contract `0ed46eed21febebf0dd778a619add0f75c3e376e1eef9056f7867925bf205ed5`, dimension **1024**.
- **290** supported static records and **89/89** governed attribute/source mappings.
- Both isolated catalog collections and the scoped Redis active marker are absent.
- Redis reports `aof_enabled=0`, `appendonly=no` and `save=""`.

The last finding proves neither AOF nor scheduled RDB persistence is enabled.
It does not establish whether manual snapshots, host backups or durable volumes
exist. `appendfsync=everysec` has no AOF durability effect while AOF is disabled.
The deployment/volume location and maintenance owner are not present in the
project's publication configuration. A request for that information is pending.
No shared Redis configuration, restart or backup operation is approved here.

## Preconditions still requiring operational evidence

1. Identify the actual Redis deployment and persistent-storage owner. Prepare a
   maintenance plan appropriate to that deployment, including capacity, storage
   survival and recovery. Do not assume a runtime `CONFIG SET` is a durable
   deployment change, or that an unknown local directory is a persistent volume.
2. Obtain approval for the concrete Redis deployment operation if one is needed,
   and verify the resulting persistence/recovery evidence. This candidate does
   not choose an AOF/RDB loss policy for the owner or authorize a service restart.
3. Obtain the already pending authorization for the isolated native catalog
   publication. Selection of 81/205 identifies read-only validation scope; source
   commits and Draft PRs do not grant index/configuration write permission.
4. Verify source files match `git_commit_manifest.json:implementation_commit`,
   which is the tested producer revision for the commands below. Recheck the
   configured Redis target hash, collection absence and active marker. An
   unexpected existing generation requires inspection before initial publication.

No placeholder vector or saved snapshot may be published. The publisher uses
its own fresh capture. Changed source, target or embedding identity causes the
guarded command to refuse the operation; regenerate the review if necessary.

## Exact guarded commands after the preconditions are satisfied

Run in `E:/YouoAgent/Oagnet`. These are proposed commands, not an authorization
or a record of execution. The source commit in the referenced manifest contains
the actual tested operator guards; it is not a caller-supplied model identity.

```powershell
$catalogCandidate = Get-Content -LiteralPath 'E:/YouoAgent/DataAnalysis_Agent/docs/cutover/catalog_publication_candidate/publication_candidate.json' -Raw | ConvertFrom-Json
$catalogRevision = (Get-Content -LiteralPath 'E:/YouoAgent/DataAnalysis_Agent/docs/cutover/catalog_publication_candidate/git_commit_manifest.json' -Raw | ConvertFrom-Json).implementation_commit
python -X utf8 scripts/manage_catalog_publication.py initialize --semantic-model-id 81 --business-domain-id 205 --expected-target-identity-hash $catalogCandidate.expected_target_identity_hash
if ($LASTEXITCODE -ne 0) { throw 'Catalog initialization refused; inspect before continuing.' }
python -X utf8 scripts/manage_catalog_publication.py publish --semantic-model-id 81 --business-domain-id 205 --publication-id $catalogCandidate.publication_id --producer-revision $catalogRevision --expected-target-identity-hash $catalogCandidate.expected_target_identity_hash --expected-catalog-version $catalogCandidate.expected_catalog_version --expected-embedding-contract $catalogCandidate.expected_embedding_contract
if ($LASTEXITCODE -ne 0) { throw 'Catalog publication refused or incomplete; inspect before retrying.' }
python -X utf8 scripts/manage_catalog_publication.py verify --semantic-model-id 81 --business-domain-id 205 --expected-target-identity-hash $catalogCandidate.expected_target_identity_hash
```

The operator CLI now requires target preconditions for `initialize`, `publish`
and `reactivate`. `publish` also requires reviewed source and embedding identity.
The target is checked before constructing a Milvus client. Embedding identity
is checked before opening a store. The reviewed source is checked against the
actual publisher capture before embedding, Redis reservation or row writes.
These checks bind a review to an operation; they do not provide authentication
or prove deployment durability. Existing read-only `verify` remains compatible.

## Write inventory, acceptance and failure handling

Approved execution would initialize the two configured semantic/physical names
with `_catalog` appended, embed 290 records using the existing configured
transport, write a new immutable generation, and write Redis reservation,
manifest and scoped activation keys. The current publication ID is
`cutover-81-205-20260909-r2`; never retry a reserved ID after a partial operation.

After row writes, the existing publisher verifies full strongly consistent
inventory and vectors against fresh MySQL authority, then performs scoped Redis
CAS activation. A new pin must read and finish successfully. Preserve exact
receipts and the tested producer revision. Registry persistence/recovery and
deployed reader trust remain separate evidence requirements.

Before activation failure, retain the isolated incomplete generation for
inspection. After an unknown Redis acknowledgement, inspect the actual marker;
do not assume rollback. No previous active release exists in the current
observation. Git revert cannot establish a production rollback. Keep V2 disabled
if initial acceptance is unsuitable.

This candidate includes no Redis reconfiguration/restart, V1 index rewrite,
entity-value rebuild, business SQL, permission change, default-model change,
deletion, merge or V2 traffic switch.

The operational boundary comes from [project AGENTS.md](../../../AGENTS.md):
“默认只运行离线测试及 Mock；禁止未经明确授权的生产写入、索引重建、权限修改、数据库不可逆变更和生产切流。”
No native publication or Redis write was performed during candidate preparation.
