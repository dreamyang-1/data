# Phase 0C regression delta

Baseline `3b3d60ccdd2698887ba57e2b849564861adf7e7c`: {'passed': 1763, 'failed': 29}. Final complete serial batches: {'passed': 1853, 'failed': 27}.

Scope suite: 88/88 passed. Old pass → new fail: 0. Missing baseline nodes: 0. Collection errors: 0. The two existing OpenAPI failures now pass because trusted headers are actually enforced.

The remaining 27 failures were already failing in Phase 0B. Their node IDs are retained in the JSON delta. Full-process attempts were interrupted by host memory exhaustion; only the complete module-batch aggregate is the final gate. No real external model calls or production writes were permitted by the existing offline network guard.
