# Round 5.6 rollback

Baseline: `564101f7e83964a5b27ff09e00db4199b0b5d847` (PR #64).

Root A commit: `f782d9f7997783658c5c1f68b91a822529eb278c`.
Root B/evidence commit: resolve with `git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_6/results.json`.

On an isolated rollback branch, revert Root B/evidence first, then Root A. Inspect and synchronize only the exact paths in `results.json` (six source/tool/test files and three document files). Preserve unrelated changes. Do not reset or mirror directories. No state, database, Catalog, configuration, identity or V1 routing migration was performed, so none requires rollback. The experimental scalar SET adapter was never committed and is absent from the final code.

After revert, run the affected offline tests appropriate to the restored contract. Existing baseline tests were not edited. Root A and Root B may also be reviewed or reverted separately; the live diagnostic descriptions and schema hashes in this closure apply to the combined candidate only.
