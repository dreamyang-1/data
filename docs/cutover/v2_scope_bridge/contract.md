# Current-scope V2 plan contract 0.2.2

This is an internal plan-only service boundary. It changes no ChatRequest,
ChatResponse, SSE event or public endpoint format. The trusted business backend
remains the sole authentication/authorization authority.

`ScopedPlanSession` copies and revalidates the current `ChatRequest` and
`TrustedIdentity`. It takes `AuthorizedSemanticScope` only from that request.
Model ID remains a required strict positive integer; zero domains means
MODEL_WIDE, one means the exact explicit domain, and more than one fails before
opening the catalog provider. Roles create no grants. No default model/domain
is introduced; 81 / 205 is the user's acceptance target only.

The session pins Oagnet's published catalog for that model/domain. It carries
catalog version, vector index version, publication ID, activation ID and target
hash, together with current database/KB scope and the hashed conversation
namespace. The namespace uses the existing application/conversation identity and
tenant/user metadata; these are state isolation, not data authorization.

Catalog candidates come from the pin's scoped reads. Only returned candidate
handles can become bound references. A deterministic type/role table covers
metrics, entities, attributes, dimensions, enum values, relationships and
physical metadata. A binding receipt records the full reference and pinned
record identity/hash. It is catalog membership evidence, not an invented ACL.
Unknown types or incompatible roles fail closed. Requested database IDs cannot
be equated with physical data-source IDs: physical bindings with a database
constraint remain closed until the governed database-load mapping is connected.

Model-owned global records have an empty *owned-domain* tuple. This does not
add a shared domain to an explicit request: such records are usable only in
MODEL_WIDE scope. The frozen 0.2.1 plan branch still rejects empty-domain bound
references and retains its existing ACL-shaped requirements. The new 0.2.2
branch accepts the current upstream contract with an optional database and
without synthetic row/column/metric permission hashes. Mixing versions or proof
families fails validation. `semantic_model_version` in this profile is the
captured catalog content version alias, not a separately verified model-release
number. The new schema is exported separately; historical schema files remain
unchanged.

State envelopes bind Pending, Task Frame, Last Request, DAG Resume, Response
Cache, Dataset, Result Artifact, Semantic Bindings and Conversation to the
complete current context. Exact compatibility is required, including catalog
activation. Widening and narrowing both invalidate an older envelope. Digests
detect corruption; they are not signatures or proof of trusted origin. Only
service-owned storage may supply envelopes; accepting arbitrary client/model
JSON at this boundary would violate this contract.

Conversation and task restoration revalidates their existing typed structures;
embedded conversation identity must match the current namespace. Nested semantic
bindings must resolve to the same pinned record again. DatasetState must be
VALID, and Dataset/task baselines require restored membership and exact owning
task/version. DatasetState has no executed snapshot field: a SourceDatasetRef
requiring a snapshot therefore fails closed without a matching trusted execution
receipt. The structural snapshot-receipt contrast test does not establish a
runtime execution path. Dataset completeness, ranking safety, executed snapshot
receipt creation and state-store integration remain V-03.

The existing CurrentTurnParser and TurnResolver are called inside the session.
TaskPatch values are revalidated through the slot registry; writes cannot insert
unbound semantic references or unrestored Dataset/task references. Compilation
accepts only a parse/resolution pair issued for that session, uses the 0.2.2
LogicalPlan compiler and derives the existing ResultContract. Plan identity is
bound to context, current text, message, task and version.

Before returning a SHADOW_ONLY artifact, `pin.finish()` must recheck current
authority, complete stored inventory and active publication. State sealing is
allowed only after that acceptance. A plan-only session cannot create executed
Dataset or Result Artifact envelopes. Catalog errors remain system failures.

This source integration is exercised with the actual Oagnet publication code
and offline stores. Tests still supply parse, candidate selection and TaskPatch
stage outputs. Autonomous recognition (V-01), actual publication/deployed trust
(C/X blockers), downstream ASL/SQL mapping (S-01), production storage wiring,
Gold metrics and real shadow acceptance remain required before V1 replacement.
