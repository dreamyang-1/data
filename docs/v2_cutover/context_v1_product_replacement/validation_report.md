# V2 Context → V1 Product Replacement Closure

## Problem class

A standalone relation query can execute successfully through V1 and return a
Dataset, but a later elliptical value such as `费森尤斯呢` cannot be completed
unless the V2 context task retained the first query's editable product filter.
The observed failure was not entity retrieval: the existing V1 scoped search
uniquely resolves `费森尤斯` to `母厂牌 / parent_brand` in semantic model 81.

## First divergence

After a successful V1 database query, V1 persists the generated output Dataset
ID on its Canonical request. The live Bridge compared that value only with the
input `ChatRequest.dataset_id`, which is empty for an ordinary database query.
It therefore rejected the exact V1 request as unrelated evidence and published
a context task without the product filter. The later value edit then had no
safe slot to replace.

`FIRST_DIVERGENCE = V1_SUCCESSFUL_EXECUTION_EVIDENCE_DATASET_IDENTITY`

There was a second general restriction: filter replacement required the whole
task to contain exactly one editable filter. A valid query containing both a
region and a product therefore could not replace only its product condition.

## General solution

1. Keep the existing exact input-Dataset comparison.
2. When the caller had no input Dataset, accept a generated output Dataset only
   when both the persisted Canonical request ID and Dataset ID equal the exact
   successful `AgentResponse`.
3. Resolve a terse value through V1's existing scoped entity retriever against
   every distinct editable semantic family.
4. Replace only when exactly one family resolves and the prior task has exactly
   one slot in that family. Keep cross-family or duplicate-slot matches blocked.
5. Send the resulting natural-language `completed_question` through the
   unchanged V1 execution entry. Do not copy V1 retrieval, Scope, Oagent, ASL,
   SQL, Dataset, or authentication behavior into V2.

This handles product name, brand, parent brand, manufacturer, and product
category replacements through current Catalog evidence. Region, hospital, and
partner edits use the same family-selection rule. No product value or business
domain is hard-coded.

## Safety properties

- Failed V1 executions still cannot publish successful context evidence.
- A response from another request cannot authorize the output Dataset.
- Region and other unrelated filters remain unchanged during a product edit.
- A value matching more than one semantic family still requires clarification.
- The first execution question is passed to V1 byte-for-byte.
- V1 remains responsible for current entity retrieval and every downstream
  execution step.

## Offline validation

- Focused Bridge and V1 entity-retrieval tests: 95 passed.
- Affected context, rewrite, Scope, Oagent-contract, and orchestrator tests:
  290 passed, 0 failed, 0 collection errors.
- Git HEAD baseline full suite: 3702 passed, 106 existing failed, 0 collection
  errors.
- Product-replacement candidate full suite: 3705 passed, the same 106 existing
  failures, 0 collection errors. The exact failed-node set is unchanged and
  `old-pass -> new-fail = 0`.
