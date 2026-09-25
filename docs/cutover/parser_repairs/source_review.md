# Current-turn representation repair review

Baseline: `f15969dcc0f90e75a399a3d80d4a489d58139dec` / Draft PR #38.
First divergence: RULE_PARSE, specifically typed model output entering the strict
CurrentTurnParser span/reference checks. This is an actual V2 cutover issue:
the 100-case model baseline had 35 validation failures despite 100 HTTP200 replies.

Ten failing current-model outputs were captured from the existing prompt/schema and
replayed without model calls. The initial six include four incorrect Unicode
code-point spans and two literal negation tokens in mention-ID lists; four further
cases include time words in temporal mention-ID lists.
Examples include the start of 销售总成本 being 3 instead of 4 in 请告诉我销售总成本,
and negations=[不要] despite an existing unique mention explicitly marked negated.

## Deterministic eligibility

- A span is repaired only if the same surface occurs exactly once in the current
  raw text. Matching is exact, including Unicode code points; no fuzzy matching,
  normalization, synonym expansion or nearest-occurrence selection is allowed.
- A correct original span is retained even when the same surface occurs twice.
  An invalid span with repeated or absent surface stays invalid. Overlapping
  occurrences count as multiple occurrences.
- If any mention is from another turn, no repair is performed. Current identity
  cannot be obtained merely by finding historical text in the current message.
- A missing negation reference is repaired only when the exact token occurs once
  in current text, and exactly one already-declared negated mention covers that
  occurrence with a valid span. The existing negated flag is evidence; the repair
  does not infer negation or select among competing target mentions.
- A temporal surface token is handled analogously only when exactly one already
  declared time-role mention covers its unique occurrence. No time role is inferred,
  no date/default is created and linguistic historical reference remains distinct
  from selecting a data period. Unknown temporal references still fail.
- Missing coordination/operation references are not dropped or guessed. Unknown
  slots are not mapped to convenient aliases: `limit` could mean display or ranking,
  so its destination cannot be safely inferred from the slot name alone.

The input object is copied. Only eligible coordinates and negation/temporal-reference
representations change; surfaces, normalized surfaces, roles, operation markers,
semantic slots, current scope and historical state are preserved. In particular,
this does not correct a wrong role, query shape, ADD/REPLACE choice or invented edit.

The **frozen pipeline models and strict CurrentTurnParser remain unchanged**.
The opt-in RawTurnPlanner invokes the private repair before validation; the
benchmark follows that same path. The production V1 router and public request,
response and SSE contracts are untouched. Prompt text and model defaults do not change.

The private **generation schema view** now enumerates the existing allowed slot
names for OperationMarker.slot_name and explicit_slot_mentions object keys. The
previous unconstrained Identifier/dictionary schema admitted names that the strict
registry later rejected. This narrows generation to the existing contract; it does
not add a slot, weaken the frozen model or modify its schema. The benchmark records
the changed generation-schema hash rather than falsely reporting an unchanged schema.

## Trace and validation

Repairs have bounded reason codes, field paths, mention IDs and old/new coordinates.
No raw business surface or full utterance is forced into this trace. RawTurnPlanner
uses its existing logging boundary; benchmark receipts record the same repair facts.
The ten recorded outputs are newly curated evaluation utterances, not private
production conversations or model reasoning traces.

Tests cover exact and Unicode shifts, correct repeated spans, overlapping/ambiguous
surfaces, absent/fuzzy text, foreign turn IDs, explicit negation evidence, absent and
multiple negation candidates, references that must remain rejected, input immutability,
ten actual failure replays and a two-turn scoped plan retaining ADD semantics.

The live before/after studies use the same frozen 100 cases, prompt text, clock,
model and temperature. The final run changes deterministic representation repair
and the private generation-schema constraint together. Fresh model generations may
vary: the captured replay proves deterministic effects; live metric differences
alone do not identify a single cause. Unchanged Gold, prompt-text and changed schema
hashes must be checked explicitly in the final evidence.
