# Contract and self-review

The installed PyMySQL source was inspected: Cursor.mogrify binds dictionaries
through connection.literal; Connection.escape_string reads SERVER_STATUS_NO_BACKSLASH_ESCAPES.
Tests exercise the installed implementation using defer_connect=True rather
than a hand-written escape function or an actual database connection.

Generated parameters contain only validated JSON scalar values. They never
replace identifiers, operators or SQL grammar. All ordinary percent characters
are doubled before compiler-owned markers become named placeholders. The lexical
guard rejects placeholders inside quotes/comments, missing/repeated values,
nonfinite/nested values, encoding errors and collisions with catalog text.

Agent compares ASL identity, statement-plus-values fingerprint, style and count.
The existing executor repeats template/fingerprint validation before connecting,
copies the scalar map and includes parameters in the snapshot hash. This closes
value mutation and same-template/different-parameter reuse at that boundary.
The parameter fingerprint is not authorization: full current semantic scope,
catalog pin, data source and source/JOIN checks remain required independently.

Public entry points pass no new fields or keywords and retain their previous
behavior; all V2 planning stays SHADOW_ONLY. The private pinned translator still
raises PINNED_TRANSLATION_PLAN_ONLY for execution methods. Actual driver/source
deployment, source collation, native results, publication/recovery and canary
remain unproven. No complete ResultContract or cutover PASS is inferred from
fixture rows, the mocked executor or the installed driver alone.
