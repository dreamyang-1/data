# Limited Scalar Candidate Deployment Runbook

This runbook is a reviewed plan. Round 5.12 did not execute it.

## Preconditions

1. Deploy a reviewed commit containing Round 5.11 and Round 5.12. Reject a source checkout that does not contain baseline `6b3a6e91e06ee5897a44ca2fd5d28532ecb68a79`.
2. Resolve the current blockers recorded in `closure_report.md`: publish/initialize the governed Catalog collection for model 81/domain 205, and provide production Redis persistence/recovery evidence.
3. Obtain a later explicit user approval and a maintenance window. Record the exact current Agent PID, start time, source directory and command before any process action.
4. Keep all existing secrets in the current secret configuration. Do not copy a `.env` into Git and do not restore an old API key.
5. Begin with a new conversation. V1 Tasks, Pending, Dataset and execution receipts are not migrated or interpreted as V2 state.

## Candidate configuration

The application default remains `DATA_AGENT_RUNTIME_MODE=V1`. A candidate process must explicitly set `V2_LIMITED_SCALAR` and supply the non-secret pins listed in `.env.example`: stable Store namespace/deployment ID, 81/[205]/58 scope, exact Catalog/vector/target versions, exact Oagnet and SQL source digests, and the field-bound time storage evidence. Redis and model secrets continue to come from the existing configuration chain.

Run the read-only checks from the deployed Agent directory:

```powershell
python -X utf8 tools/limited_scalar_deployment.py --action check --source-root <deployed-git-root>
python -X utf8 tools/limited_scalar_deployment.py --action enable-plan --source-root <deployed-git-root> --expected-current-pid <recorded-agent-pid>
```

The command is Dry Run only. `--execute` is deliberately rejected in Round 5.12. A valid future enable plan uses the existing `python -X utf8 -u -m uvicorn app.main:app --host 0.0.0.0 --port 8088` shape only after the operator has verified that it matches the actual service owner and process lifecycle.

## Startup and acceptance

The V2 mode is fixed when the process starts. Any missing source, Catalog, source-value, time, Redis, model or data-source dependency fails construction/readiness; requests cannot select the mode and the candidate never falls back to V1. `/live` proves only process liveness. `/ready` must show the limited scalar Redis, Catalog pin, source pin and read-only transport checks as true before traffic is admitted. Startup/readiness must not submit business SQL or write a test session.

After a separately authorized deployment, test only the documented limited scalar slice through the existing `/agent_chat` and `/agent_chat/stream` contracts. Unsupported shapes must return the existing safe rejection before SQL. Any scope expansion, wrong field/source acceptance, session crossover, invalid receipt, unknown execution without quarantine, duplicate execution, API/SSE incompatibility or unacceptable latency is a rollback trigger.

## Unknown execution operations

Use `tools/limited_scalar_session_admin.py` with exact tenant, user, application, conversation, message and 81/[205] scope. The default `status` action is read-only and shows bounded metadata. `require-review` needs `--authorize-mutation`, an operator and a reason. It only moves RUNNING/UNKNOWN to REVIEW_REQUIRED using CAS. It cannot submit SQL, enter a business value or manufacture success.

## Rollback

Generate the reviewed rollback plan before deployment:

```powershell
python -X utf8 tools/limited_scalar_deployment.py --action rollback-plan --source-root <candidate-git-root> --v1-source-root <recorded-v1-git-root>
```

During a later authorized rollback, stop only the recorded candidate PID, start the recorded V1 source with `DATA_AGENT_RUNTIME_MODE=V1`, preserve current secrets, and verify `/live`, `/ready` and the platform. Do not delete, translate or expose the V2 namespace. Start a new V1 conversation because V1 cannot be assumed to understand Tasks created while V2 was active.
