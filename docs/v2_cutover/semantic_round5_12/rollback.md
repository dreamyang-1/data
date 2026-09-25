# Round 5.12 Rollback

Before deployment, code rollback is `git revert <round-5.12-commit>` on a review branch. The default runtime remains V1, so reverting this commit does not require changing the current process or configuration.

After a future authorized candidate deployment, follow `deployment_runbook.md`: identify and stop only the recorded candidate process, restart the recorded V1 source with current valid secrets, use a new V1 conversation, and leave the stable V2 namespace intact for audit. Never restore an expired key, translate V2 state into V1, broadly scan/delete Redis, or claim an unknown SQL outcome succeeded.
