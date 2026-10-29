# Round 2.4 Rollback

本轮没有生产代码、Prompt、Schema、Oagnet、SQL Translator、8088、Redis 或 Benchmark 变更。回滚只需 revert 本轮文档提交：

```powershell
git revert <round-2.4-commit>
```

PRIVATE `.eval_private/entity-instance-round2-4` 不进入 Git，不随源码回滚处理。不得用回滚命令删除 `.env`、凭据、日志、用户改动或其他阶段证据。
