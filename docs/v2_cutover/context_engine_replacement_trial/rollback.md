# Rollback

The committed default remains `DATA_AGENT_RUNTIME_MODE=V1`. To end the current 8088 internal trial, restore the pre-trial local `.env` backup from the ignored trial backup directory and restart the same uvicorn command on port 8088. Do not copy that backup, credentials, logs or private receipts into Git.

To revert the code after review, use `git revert <context-engine-trial-commit>` on a new branch. The revert removes the internal read-only catalog option and the two bounded recognition repairs; it does not modify Redis data, Catalog data, Milvus, Oagnet or SQL Translator.

The trial namespace is separate from the earlier V1 namespace. Cleanup, if needed, must use the exact recorded trial keys; never use `FLUSHDB`, `FLUSHALL`, wildcard deletion or a database-wide scan-and-delete operation.
