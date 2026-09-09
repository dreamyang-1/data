# Evaluation gate reclassification and current-source identity audit

Current Stage: **frozen axis Gold / deterministic evaluator / measured parser baseline**.
Audit and gate-reclassification acceptance: **PASS**. V1 Replacement Readiness:
**NOT_READY**. Production-acceptance blockers remain **8 P0 / 4 P1**; these are
not global prohibitions on offline evaluation. V1 routing and public I/O are unchanged.

The user's 2026-09-09 decision supersedes the former requirement to finish all
Catalog/Redis production work before any evaluation. This delivery completes the
requested audits, establishes runnable axis evaluation, and measures the existing
current-turn model. It does not claim whole-pipeline/model or shadow acceptance.

## 1. Actual catalog, physical fields and identity evidence

Scope: model **81**, explicit domain **[205]**. Authoritative catalog capture:
2026-09-09T01:16:52.806110Z, version
`3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`.
The [frozen catalog](frozen_catalog.json) records the full source snapshot SHA-256,
capture source identity, exact scope and a separate hash for its 127-fact semantic
projection. It contains no credentials, hostnames or business rows. Certification
here proves the listed capture/projection checks; it does not certify native publication.

The authorized source-data audit used one InnoDB REPEATABLE READ / WITH CONSISTENT
SNAPSHOT / READ ONLY transaction, 01:20:36–01:20:41Z, followed by rollback. Before
opening the source connection, all 89 current mapping/route definitions matched
the captured scope. Catalog identity was checked again after the data read and
remained unchanged. The data observation ID is recorded in [identity_matrix.json](identity_matrix.json).
This ephemeral MVCC observation is not a persisted copy of every source row and
does not prove permanent uniqueness. Exact later execution needs applicable revalidation.

All **14 physical tables** have verified constraints and nonempty unique candidate
IDs/codes. Of **29 candidate tuples** tested, **28** are snapshot-unique; the other
is hospital_code with one null. No duplicate values or raw rows were exported.
The [aggregate evidence](identity_aggregate_evidence.json) records null rate,
row count, distinct count, duplicate groups/excess rows, indexes, column nullability,
collation, catalog flags and declared relation usage for every candidate.

| Entity | Selected provisional identity | Rows = distinct | Scope/qualification |
| --- | --- | ---: | --- |
| salesperson | salesperson_code | 89 | Declared relation endpoint; unique physical index |
| project | project_code | 138 | Declared relation endpoint; unique physical index |
| sales_company / guoyao_company | guoyao_code | 1 | Unique in this small snapshot; no broad statistical claim |
| product_line | product_line_id | 92 | Declared relation endpoint; unique physical index |
| sales_order | id | 60000 | Source-row identity; exact business-order grain remains separate |
| manufacturer | manufacturer_code | 1286 | Catalog code is main_data_domain_ent_manufacturer |
| hospital | hospital_id | 139 | hospital_code has one null and is not selected |
| department | dept_code | 78 | No composite key needed for this observed table |
| product_category | category_id | 413 | Declared relation endpoint; unique physical index |
| dealer | dealer_code | 2154 | Declared relation endpoint; unique physical index |
| city | city_id | 396 | Physical primary key and declared relation endpoint |
| province | province_id | 34 | Physical primary key and declared relation endpoint |
| product | product_code | 7211 | Keep different codes with the same display name |
| product_dept_relation | product_code + dept_code + relation_type | 47872 | Full physical unique tuple; partial joins alone do not prove tuple identity |

For each selected candidate: non-null rate 100%, blank count 0, duplicate groups 0,
duplicate excess rows 0. **PROVEN facts** are the observed physical constraints and
aggregate uniqueness, not newly invented catalog declarations. **14 provisional
identities**, **0 NAME_FALLBACK-only entities**. The current source still has **0
formal identity declarations** in the scoped metadata capture.

### The three user-reported attribute declarations

The fresh scoped raw catalog read still returns 0 for all 89 is_primary_key and
is_unique flags; the serialized attributes agree. The source physical schema does
prove PRIMARY(id) for salesperson, project and guoyao_company, and all three id
columns passed the aggregate audit. These three entities are therefore not being
classified as having no usable identity.

The reported Attribute.is_primary_key=true versus current-source false discrepancy
is retained as a source/version inconsistency. No catalog row is overwritten to
make the evidence agree. The offline policy now explicitly recognizes a single
true attribute PK when the entity-level PK is empty; tests cover all three named
entities, malformed string flags, conflicts and ambiguous composite declarations.
This discrepancy does not block role/mention/turn evaluation.

### Actual same-name collisions and fallback rules

Snapshot queries found **832 product-name groups**, **40 manufacturer-name groups**
(149 standard-name groups), and **1 salesperson-name group** with different verified
IDs/codes. Preserve their separate identity and render name plus code, with:

> 检测到同名实体，已按实体编码区分。

NAME_FALLBACK is permitted only for display, name lists, explicitly name-based
counts and low-risk non-relational queries. It never silently authorizes exact
entity counts/ranking, relationships, joins or attribution. The policy preserves:

> 当前语义目录尚未声明该实体的唯一身份字段，本结果暂按名称去重。同名但实际为不同实体的记录可能被合并；如需精确区分，可同时返回实体编码/ID或地区等辅助标识。

The implementation in tools/cutover/evaluation_contract.py is an **offline policy
and acceptance oracle**, not a production result-renderer replacement. No production
response format or V1 behavior changed. Runtime adoption of provisional identities
and warnings must retain the existing response envelope and its result contracts.

### Remaining decisions are scoped, not fourteen missing-key requests

- sales_order.id proves row identity. order_key and its tested composites are also
  unique in this snapshot (order_key has a unique index), but this does not decide
  business order versus detail-line grain for every future exact order-entity query.
- Of 24 relation declarations, 22 resolve within declared owners. Hospital→project
  still has ambiguous unqualified hospital_id across base/subtable; hospital→department
  still points at a source-owned bridge without the complete target endpoint. Names
  are never substituted for these join keys.
- The two previously recorded metric-subject ambiguities remain scoped to their
  execution shapes. The temporal source runtime reports SYSTEM/UTC; that does not
  independently establish the business interpretation of every DATETIME column.

## 2. What Redis code and runtime prove

The actual dependency path imports RedisSessionStore from **app/stores/session.py**,
not the separate older app/stores/redis.py implementation. app/config.py and
app/dependencies.py establish redis mode, DB3 default, prefix youo:data-analysis:v2,
session TTL 7200 and response TTL 7200. Response records enforce at least session
TTL. The store implements Pending, response fingerprints, last request, task frames,
dataset references, DAG checkpoints/Pending, execution locks and report references.
This is state-store capability evidence; it is not Redis durability proof.

The **current workspace's effective Settings** resolve redis mode, DB3, port 6379,
the stated prefix/TTLs and a credentialed connection. Host/target identities are
hashed; passwords and full URLs are not emitted. This checks the configured target,
not another already running process's inherited environment or deployed revision.

Read-only INFO persistence / INFO replication and every requested CONFIG GET were
successful. Results: appendonly=no, appendfsync=everysec, save="", role=master,
connected_slaves=0, dbfilename=dump.rdb; dir is present and recorded as a hash.
The RDB last-save status being ok does not prove an automatic schedule, usable backup
or restore drill. No SAVE/BGSAVE, CONFIG SET, restart, deployment or production key write occurred.

The repository/workspace deployment audit inspected 217 configuration/script paths.
Redis client configs exist; none prove this nonlocal target's mounted volume,
restart policy or backup/restore process. Unrelated compose files do not establish
the target deployment. These remaining production recovery/trust facts belong to
**Canary/Cutover**, not Offline Gold, Semantic Evaluator, Model Benchmark, or isolated
plan-only shadow. See [runtime evidence](redis_runtime_evidence.json) and
[deployment evidence](redis_deployment_evidence.json).

## 3. Gates and work that actually continued

| Work | Actual gate after the user decision |
| --- | --- |
| Turn / mention / semantic-role Gold | Frozen scoped facts and reviewed labels; no global entity-identity/Redis/publication gate |
| Semantic evaluator | Hash/scope integrity, correct per-axis denominators and failure handling |
| Model benchmark | Same frozen input, prompt/schema/config; usable model credentials and service capability |
| Exact entity count/rank/relation/join/attribution | Per-case identity, grain, canonical binding and declared relation prerequisites |
| Plan-only shadow | Relevant evaluation acceptance and isolated state; no SQL, production state writes or response takeover |
| Production canary / cutover | Native catalog acceptance, deployed trust, Redis recovery, full evaluation/shadow and rollback; explicit approval before formal replacement |

[gate_matrix.json](gate_matrix.json) and the master readiness matrices reflect this
split. Original overall production blockers are retained rather than renamed PASS.

**100 axis-labeled Gold cases** are now frozen against **127 scoped catalog facts**:
14 entity list cases, 22 measure cases, 13 grouping cases, 18 projection cases,
23 multi-turn/slot cases and 10 contrasts. Labels were reviewed against explicit
user rules and the scoped catalog, independently of model predictions. Ambiguous
date-shape/lineage-role labels were left unscored before model execution. No row is
auto-labeled COMPLETE. Cases remain partial for SQL, source result, canonical
binding and complete task-state ground truth; this is not the final complete Gold
or an independent held-out accuracy claim.

The deterministic evaluator rejects wrong scope/catalog hashes, duplicate/unknown
predictions and invalid spans. Missing/failed predictions remain in denominators.
It measures selected critical-mention recall, role recall/purity, turn relation,
slot operation and query shape. It does not report unannotated mention precision
or pretend these partial axes establish end-to-end accuracy.

## 4. Actual configured-model benchmark

Used the existing V2 current-turn prompt/schema/transport and qwen3.6-plus,
temperature 0, fixed business clock, timeout 30s, retries 0 for every probe/run.
Only model/Thinking differs between probes; production defaults remain unchanged.
The actual current-turn stage does not receive history or catalog candidates;
those stay local as label/context evidence. This is explicitly a component study,
not a complete V1/V2/state/SQL comparison.

| 100-case baseline metric | Result |
| --- | ---: |
| HTTP successful responses | 100/100 |
| Parsed outputs accepted by current validation | 65/100 |
| Critical mention recall | 46/89 |
| Critical role recall / purity | 37/89 |
| Query shape accuracy | 22/56 |
| Turn relation accuracy | 50/72 |
| Slot operation accuracy | 9/13 |

The 35 rejected outputs comprise 30 ValueError and 5 ValidationError at current-turn
validation, not provider transport failures. A six-case diagnostic replay reproduced
four invalid surface-span outputs and two missing mention-reference outputs. Other
accepted outputs include entity lists misclassified as RELATION_LIST and wrong roles.
These measured failures are the next parser/evaluation work; Gold was not relaxed.

Candidate probes: qwen3.7-plus returned **403 Model.AccessDenied** twice. Current
qwen3.6-plus Thinking exceeded the unchanged 30s timeout twice, so a 100-case Thinking
run was not manufactured from those probes. Official model availability and actual
account access are different facts. Model sources: [Qwen3.6-Plus](https://help.aliyun.com/zh/model-studio/qwen3-6-plus)
and [official model list](https://help.aliyun.com/zh/model-studio/text-generation-model).

Total real model requests: **112** (100 baseline, six diagnostic, six preflight).
No raw reasoning or headers stored. Benchmark production-state writes and SQL
executions: **0**. The separate user-authorized source aggregate SELECT audit is
explicitly recorded above; it is not falsely counted as zero database reads.

## 5. Validation, review, release and remaining path

**38 new tests pass**: split gates, shadow side effects, cutover approval, null/empty/
duplicate candidates, same-name safety, name fallback, attribute PK recovery, composite
ambiguity, missing fields, hash/scope rejection, metric denominators, frozen corpus
reproduction and a mocked existing-model transport proving labels/history are not sent.

Full offline regression: Agent **2735 passed / 27 failed**, Oagnet **663 / 8**,
SQL Translator **381 / 0**. Critical acceptance **160/160** (the separate 15 critical
scenario cases also pass), clarification trace **89/89**, collection errors **0**,
old-pass→new-fail **0**. Old failures and expectations remain unchanged.

Self-review checked aggregate SQL and current-source bindings, read-only transactions,
snapshot limitations, formal/provisional/name distinctions, no name joins, scoped
gates, corpus leakage, denominator handling and safe model receipts. One review
correction explicitly separated partial composite join usage from proof of the full
unique tuple. No Agent/Oagnet/SQL production runtime source was modified.

Baseline: `bcf7d81b57fce85b018e5dc053368b8a6b870108` / Draft PR #37. Release uses explicit
file manifests, normalized hash checks, feature commits and a stacked Draft PR;
no merge, native publication, Redis mutation or production replacement.

Next shortest blocking path: retain this frozen evaluation baseline, diagnose and
repair current-turn span/reference failures with contrast tests, then extend full
V1/V2 state/semantic evaluation. In parallel, address affected relation/grain facts
and production recovery/publication for eventual canary. **Do not wait for fourteen
catalog-owner key declarations before continuing evaluation.**
