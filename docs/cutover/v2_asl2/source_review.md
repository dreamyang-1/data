# Root cause and boundary review

PROVEN first divergence: V2_BACKEND_LOWERING. The only existing adapter classified
ASL 1.0 loss and emitted no actual downstream plan. The new opt-in compiler targets
the current ASL 2.0/query surface and preserves a shared pin through SQL acceptance.
The existing frozen adapter is not relabeled as an implemented backend.

The supported path emits all ResultContract outputs with stable SQL aliases.
Display labels stay separate; user strings do not become SQL identifiers. Actual
SELECT aliases and their order are checked at the boundary, then actual result
columns must match before structural binding proof. WHERE string contents cannot
be mistaken for SELECT aliases. A row-cap hit remains incomplete.

Only conjunctions are flattened. Operators and typed literals keep their types;
unrepresentable decimal precision, timezone assumptions, null operators, complex
Boolean semantics and query families cannot be silently dropped. A current source
value uses its verified canonical lookup value and original pin acceptance.

Primary subject inference requires one unambiguous current catalog owner. The
two actual multi-subject metric definitions remain governance evidence gaps.
TimeSpec timezone is query intent, not proof of source DATETIME storage or SQL
connection timezone. The owner question is pending. No permissions or default
81/205 scope is inferred from these gaps.

The callback is a trusted service composition point adapting current
AuthorizedSemanticScope to SQL RequestScope; it is not an HTTP/model extension.
Normal RawTurnPlanner still stops at its existing logical shadow boundary.
These new methods do not make V2 executable in production. Native publication,
runtime result receipts, complete backend semantics and Gold evidence remain open.
