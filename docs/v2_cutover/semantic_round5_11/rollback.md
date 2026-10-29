# Round 5.11 Rollback

本轮未部署到现有服务，V1、8088与生产配置未改变。代码回滚以本轮独立提交为单位执行：

```text
git revert <ROUND_5_11_COMMIT>
```

回滚前检查该提交的子阶段依赖和后续提交，不使用 `reset --hard`、`clean`、force push 或目录镜像。显式文件清单位于 `evidence_index.json`。

本轮隔离 Redis 的6个精确资源已清理完毕，不能对生产 prefix 做额外删除。PRIVATE 回执用于审计，不进入 Git。源码回滚不得修改 `.env`、恢复旧密钥、重启当前服务或删除正式用户状态。
