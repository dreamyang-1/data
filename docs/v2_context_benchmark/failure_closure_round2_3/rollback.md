# Round 2.3 Rollback

回滚本轮独立提交即可移除实体实例保存逻辑、专项测试和本目录证据：

```text
git revert <round-2.3-commit>
```

回滚不会修改 `.env`、PRIVATE capture、Catalog、Redis、8088、Oagnet、SQL Translator 或 V1 路由。本轮没有部署运行进程，也没有生产状态和数据写入需要清理。
