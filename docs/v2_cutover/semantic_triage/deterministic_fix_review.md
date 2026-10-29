# Deterministic fix and overfit review

Audited runtime HEAD: `0e408509126aae0a3a324aff02aef848fd0d3c56` (Draft PR #51).
This checkpoint changes no production source or existing Gold expectations.

| Change | Classification | Evidence and scope | Remaining risk |
| --- | --- | --- | --- |
| Historical Task Context restriction | GENERIC CONTRACT FIX | `recognition.py::_task_context` uses resolved task pointers and explicit HISTORICAL/NEW_TASK signals; opaque offered handles and current-scope restore remain authoritative. No business-name branch. | A bad history signal can still make a model choose the wrong target; parser-only relation projections do not establish this runtime behavior. S81-017 selects the right historical task but misinterprets its identifying mention as an edit. |
| REMOVE/CLEAR dependency | GENERIC CONTRACT FIX | `pipeline.py::TurnResolver.resolve` recognizes one destructive dialogue act only when all operation hints agree. Mixed acts/query assignment do not acquire dependency. Actual state/reducer tests cover barriers. | The parser evaluator's `normalize()` omits this condition; its NEW_TASK result cannot be used as the final TurnResolver verdict. Operation semantics still need model/whole-state Gold. |
| Singleton REPLACE | GENERIC CONTRACT FIX | `_patch` converts a single already offered collection binding into one-element collection form only on the supported existing-target operation. Identity, mention/role, current base, operation and strict hydration guards remain. | It repairs representation, not a wrong REPLACE decision. Neither a correct JSON shape nor a passing scripted fixture proves semantic selection accuracy. |
| Time Contract | GENERIC CONTRACT FIX, OVERFIT_RISK for unmeasured grammar coverage | `explicit_time.py` derives dates/grain from current complete expressions and the business clock; anchors use catalog time caliber. CLEAR and comparison dependencies remain. Uses the existing calendar parser, not a model-authoritative date. | 24 Chinese time/format tokens and the exact literal interval format are finite grammar choices; a format such as `（不含结束时刻）` is not broad natural-language coverage. The same exposed Gold drove development. Keep code; establish contrast/holdout coverage before expanding grammar. |
| Compound Metric Merge | GENERIC CONTRACT FIX, OVERFIT_RISK for narrow evaluated coverage | `catalog_mentions.py::recover_metric_spans` uses current pinned full metric names, not fixed words such as order_count/hospital. It checks unique whole term, contiguous pieces, roles, markers, collisions and modifiers before changing spans; it does not bind an identity. | Only one MEASURE piece plus compatible source/subject pieces is supported. The four remaining compound-metric captures contain filter/group/relation hypotheses or gaps; they are not safely covered. A successful order-count example does not certify all compound metrics. |
| Value Probe | GENERIC CONTRACT FIX, semantic model choice requiring evaluation | `catalog_publication.py` and `catalog_value_sources.py` enforce the exact-empty prerequisite, pinned field/scope, governed implicit lookup, at most 64 short values, at most 3 probes, readonly/timeouts and final revalidation. Agent validates offered choices and re-reads the canonical value exactly. | A valid value is not proof that the chosen field or alias meaning is correct. The three live positives used a province-field Oracle. The latest 20-case run never reached the third model stage; city cardinality exceeded the limit. No business alias was learned or published. |

No reviewed fix directly branches on a Gold sentence or a fixed business metric,
entity or region. No CASE_SPECIFIC_PATCH was proved. Tests containing concrete
business examples are not counted as runtime keyword rules. The IANA timezone
Asia/Shanghai is a clock contract, not a region-filter special case.

OVERFIT_RISK nevertheless exists: the same public 100 + 20 records have been used
repeatedly for diagnosis, prompts have grown, and no independently protected blind
holdout manifest was found. Do not report this as “no overfitting risk” merely
because direct Regex and business-word branches are zero. No fix is removed or
broadened during this checkpoint.

## Reproducible rule volume

| Count | Initial V2 `cddded7` | First raw-input V2 `ab3c7fe` | Audited HEAD | Change from first raw V2 |
| --- | ---: | ---: | ---: | ---: |
| Direct semantic Regex calls in `app/semantic_v2` | 0 | 0 | 0 | 0 |
| Schema validation pattern declarations | 0 | 2 | 2 | 0 |
| Chinese language/format tokens | 0 | 0 | 33 | +33 |
| Business-word special-case branches | 0 | 0 | 0 | 0 |
| Prompt templates | 0 | 2 | 3 | +1 |
| Prompt normative sentence units | 0 | 34 | 92 | +58 |

The initial architecture commit did not yet contain a live recognition prompt;
therefore the first raw-input baseline is also shown to avoid a misleading
comparison. Current prompt units are 19 parse + 63 draft + 10 probe. The unit is a
whitespace-normalized sentence, split after `. ! ?`; it measures text volume,
not the number of independent semantic decisions. Every unit, source location
and hash is listed in `rule_growth_audit.json`.

The 33 tokens comprise 24 time/format tokens and 9 option-answer tokens. A separate
ten-character Chinese numeral alphabet is also present. UI messages, enum values,
contract IDs and catalog-provided names are excluded from linguistic-rule counts.
The two schema patterns validate hexadecimal digests, not business meaning.

The time implementation additionally calls the existing Legacy calendar helper:
25 Regex invocation sites and13 referenced global pattern definitions. The same
sites and definitions existed at the initial V2 baseline; no new pattern definition
was added. V2's use of this helper is a new dependency and is explicitly recorded,
so “zero direct Regex” must not be interpreted as “no regex-based date parsing.”
