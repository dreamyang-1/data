# Platform-owned semantic descriptions

## Evidence and boundary

- PROVEN: the platform catalog records download locations in
  `semantic_model.semantic_desc_file_url`. Current published models include
  both model-ID filenames and Chinese model-name filenames. The old loader
  guessed `/files/semantic_model_<id>.md`, ignoring that authoritative pointer.
- PROVEN: the current platform descriptions are served by the DSL Markdown
  service from its `files/` directory, not MinIO. Real read-only checks of
  models 120, 121, 123 and 105 resolved and downloaded all four files.
- PROVEN: the old loader cached successes and failures and fell back to a
  shared local medical-domain document. The extraction prompt contained a
  second static business catalog. Both can inject stale or wrong-model data.
- Maintenance repair only. No catalog writes, index rebuild, Java/Oagnet/SQL
  release, authorization change, V2 cutover or automatic PR merge.

## Change manifest

| File | Change |
| --- | --- |
| `app/domain/semantic_description.py` | Fresh parameterized model-ID metadata lookup, then fresh bounded HTTP read; no local document or content/negative cache. |
| `app/config.py` | Description URL configuration is a trusted storage origin, not a filename template; replace unused TTL with download size bound. |
| `app/planning/structured_extraction_prompt.txt` | Remove the embedded medical business catalog; retain the generic extraction schema and role/ownership rules. |
| `语义描述文件.md` | Delete the old shared static fallback; recoverable from Git and the server deployment backup. |
| `app/analysis/synthesis.py` | Document the shared live-loader contract; no report or query behavior rewrite. |
| Five semantic/planning/analysis test modules | Freshness, renamed/Chinese files, failure recovery, model separation, invalid IDs/URLs, bounded downloads, metadata SQL/resource closure, shared planner/insight flow. |

The existing `semantic_reference` shape and logical source label
`semantic_model_<id>.md` remain compatible with both consumers. This label is
not used to construct the download URL. Files are reference material, never
authorization or evidence that SQL used a relationship. Current backend Scope
and downstream ASL binding remain authoritative. User-configured contextual
prompts retain their existing behavior; they are not a local catalog fallback.

Download origins must match the configured description-service origin or
configured MinIO endpoint. Filename/path changes within these origins require
no code change. Moving to another origin requires updating trusted storage
configuration. Redirects, HTML/login pages, empty/invalid UTF-8 files and
oversized responses are not used as semantic content. Outages yield an
unavailable reference, not old model content or a missing-user-parameter claim.
Existing analysis of available query data remains non-blocking.

## Validation and review

- Local targeted planning, loader, insight, ownership and architecture tests:
  **183 passed**. Subsequent narrower fixture recheck: **124 passed**.
- Real catalog/storage verification: four models, matching content hashes at
  loader, planning request and insight-reference boundaries; unpublished
  model 126 returns unavailable and does not select a local default. Model
  transport in this check is synthetic capture, not a business-answer gold.
- STALE_TEST: migrate local-file/TTL expectations to the user's explicit
  platform-only, every-use contract; remove expectations for an already
  missing legacy `extraction_user_prompt` function. Preserve test identities
  except expanding the missing-model test into invalid-ID parameters.
- An incomplete planner source-selection test fixture is completed with its
  structured extraction, preserving the plain-question assertion instead of
  accidentally exercising the independently changed server retry path.
- Server compatibility runs preserve independent planner, orchestrator and
  legacy-guard changes in both baseline and candidate; none is shipped or
  overwritten by this repair. The static file differs on the server: the user
  explicitly requested deleting that exact fallback, so its actual bytes are
  retained in the isolated baseline and deployment backup.

- Server Critical Suite: **225 passed**, zero failures/collection errors.
- Full server-isolated comparison (same preserved runtime overlays): baseline
  **4681 passed / 97 failed / 1 collection error**; candidate **4722 passed /
  93 failed / 1 collection error**. **Zero new failures**, four old failures
  become passing. 38 added case identities; one old missing-model identity is
  replaced by its parameterized invalid-ID cases (explicit STALE_TEST).
- The full suite is NOT all green. Existing failures and the missing legacy
  helper import collection error are retained under the user's explicit
  verified-old-failures/no-new-failures deployment waiver.
- Initial isolated packaging omitted shared modules and was rejected; these
  counts are from the complete tracked-source bundle, not that partial run.
- Review: all catalog SQL is read-only and parameterized; model IDs are strict
  positive integers; connections and streams close on both success and error.
  No credentials/signed URLs enter logs. No cross-model fallback, Scope change
  or SSE/response-schema change; current parser rules and public node order are
  covered by the critical suite. Independent remote source changes are kept.

- Rollout completed on server 49: four exact runtime files updated, the static
  document deleted after backup, and only the Agent service restarted. New
  worker and readiness verified; protected configuration and non-target
  source hashes unchanged. The removed server file is recoverable from this
  release's deployment backup; the canonical file is recoverable from Git.
- Post-restart real catalog/storage verification passed again for all four
  models, including planner injection and insight reference capture. The
  current physical location is the DSL service's `files/` directory; the
  application still resolves it from metadata, never from that directory.
- One real native-ingress smoke question (salesperson/channel/2025 amount)
  completed through SQL and final output. Name, enum, metric and year bindings
  and public progress ordering passed. SQL returned a NULL scalar/empty
  effective result, so the existing empty-result path did not invent analysis
  nodes. This verifies chain compatibility, not an independent business-answer
  gold or an assertion of non-empty sales data.

## Readiness

Current Stage: scoped V1 maintenance, not V2 replacement. Catalog/Evaluation/
Shadow gaps and V1 Replacement Readiness remain unchanged. This repair neither
declares V2 ready nor resolves unrelated existing regression failures.
