> Superseded by [the current guarded candidate](../catalog_publication_candidate/publication_candidate.md). The original candidate below is historical and must not be executed; current Redis durability is not proven.

# Prepared operational candidate — not executed

Scope: **model 81 / business_domain_ids [205]**, current configured Oagnet target.
Target hash: `763d0302d6278371ea2cf3c5372103cf8d823ea40411d9d27f191a511a010ef5`.
Expected source version:
`74de30abbbc4b3022e69fafcfee4bea036d8e49d6780f9fa232bfbd7c82e737c`.
Inventory: 290 records, including 13 owned dimensions and 15 scoped enums.
Saved snapshots or placeholder vectors may not be published.

Candidate actions after explicit operational authorization:

1. Verify tested code revision, target hash, current scope and fresh source
   version still match. Changed source requires a new reviewed inventory.
2. Initialize only configured semantic/physical collection names with `_catalog`
   appended. Both are absent. Validate schema/dimension and V1 non-overlap.
3. Reuse the existing configured embedding model/endpoint/dimension. Build the
   approved scoped inventory from a fresh capture and write a new immutable
   publication ID, e.g. `cutover-81-205-20260908-r1`. Record tested implementation
   commit and actual embedding contract.
4. Verify complete native strong inventory, vectors and fresh MySQL authority;
   activate Redis through CAS. Verify a new pin can read and finish the release.

Expected writes: two isolated Milvus collections, one scoped vector generation,
and Redis reservation/manifest/active marker. Embedding calls use the existing
external service. MySQL stays read-only. No business SQL, entity-value rebuild,
V1 collection rewrite, V2 switch, default-model change or deletion is included.

Failure before activation: retain isolated incomplete generation for inspection;
do not retry its reserved ID or modify V1. Unknown Redis acknowledgement: inspect
the marker; do not assume rollback. There is no previous active catalog release
for this target/scope. If initial activation is unsuitable, keep V2 disabled and
stop acceptance; do not fabricate a prior release. Source revert is not runtime
rollback. Durability, concurrent native behavior, deployed trust and V2/SQL
integration still require evidence; publication alone cannot justify V1 replacement.

Authorization is required by [project AGENTS.md](../../../AGENTS.md):
“默认只运行离线测试及 Mock；禁止未经明确授权的生产写入、索引重建、权限修改、数据库不可逆变更和生产切流。”
The user's 81/205 selection authorizes read-only acceptance scope; it does not
explicitly authorize these external writes. Source commits/push are authorized.
