# Source-verified textual code filters

## Root cause (PROVEN)

An advisory product mention resolved through the scoped, parameterized source
lookup to a real material-code value containing spaces. The final validator
discarded that provenance and rejected the literal as a natural-language name.
The original wildcard product-name predicate could also survive normalization
because its percent delimiters prevented matching the advisory mention.

## Changes

- Retain request-local exact field/value evidence from the source resolver.
- Exempt only those pairs from the natural-name-on-key heuristic; scope and
  vector-field checks remain unchanged. Evidence for another field/value or
  a wildcard does not authorize a predicate.
- Replace a matching wildcard draft with its source-resolved equality.
- Return structured ASL_FILTER_INVALID diagnostics for unverified key literals.
- No DataAnalysis, SQL Translator, prompt, catalog, or SSE changes.

## Verification

- Existing 754 Oagnet tests pass; five new regression cases pass (759 total).
- Negative cases cover absent/mismatched evidence, wildcard expansion and
  mixed verified/unverified IN values. No existing assertions changed.
- Isolated runtime reproduction: old code rejects a real material-code value;
  patched code returns city and canonical material-code filters, without the
  stale product-name wildcard. No sales SQL was executed by that probe.

## Limits

This patch does not change advisory composite-mention selection or independently
establish a brand predicate from an unmatched prefix. ASL success is not proof
of final sales totals or full pipeline correctness. Production business records
and diagnostic logs are not included in this change.
