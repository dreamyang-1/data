# Phase 0C enterprise trust boundary

| Finding | Classification | Implementation status | Remaining integration |
| --- | --- | --- | --- |
| Fixed default-tenant/default-user can share conversation state | STATE_ISOLATION_P0 | Fixed in agent endpoints: verified backend tenant/user headers required; separate users tested with identical conversation/message IDs | Backend must forward stable identifiers; application header required in production |
| Client can supply arbitrary model/domain without a verified backend boundary | UPSTREAM_SCOPE_TRUST_P0 | Agent routes now require the configured service Bearer token; missing token configuration fails closed | Provision the backend's existing authKey securely; keep it server-side and validate the deployed gateway/calling chain |
| Oagnet explicit domain retrieval includes shared domain -1 | EXTERNAL_SERVICE_CONTRACT_BUG / P0 blocker | Agent blocks the unsupported query/discovery path before network access | Oagnet owner must implement and prove exact-set filtering |
| Metadata/display interfaces cannot prove explicit-domain scope | EXTERNAL_SERVICE_CONTRACT_BUG | Agent rejects unsupported metadata calls and omits unverified display retrieval | Add strict scope and candidate provenance in the external services |

Tenant/user/roles are not data authorization inputs. DataAnalysis does not introduce an ACL lookup, infer permissions or grant extra domains. The business backend remains authoritative. The Phase 0B fixed-identity finding is reclassified here without editing its frozen historical reports.

The code has not been deployed or used to validate a production gateway session. This phase does not claim production readiness. No real secret, production-data write, OAuth/SSO replacement or cross-repository patch was introduced.
