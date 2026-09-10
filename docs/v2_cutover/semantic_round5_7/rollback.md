# Round 5.7 rollback

基线：`a55cf33c3d4917548c7f4b47d01641199448ea52`，PR #65，分支 `semantic-fresh-task-round5-6-20260910t081523z`。

本轮一个功能提交；在 `E:/yy` 用以下只读命令定位：

```powershell
git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_7/results.json
```

需要回滚时先检查当前branch、HEAD和用户修改，再新建回滚分支，对已核实的上述提交执行 `git revert <commit>`。不得reset/clean/stash、强推或改写历史。若后续修改造成冲突，逐文件解决并验证；不整目录覆盖。

显式manifest见 `results.json`：3个生产文件、1个工具、1个测试、3个文档。只按回滚diff把对应Agent文件从版本仓同步至开发目录；新文件删除同样须核实绝对路径位于项目及manifest中。回滚后运行受影响回归及原scalar SET拒绝、源值/字段/Scope/Pin、CLEAR/REMOVE反例；不得把生产路由切换作为验证。

回滚会撤回生成端Filter完整树/操作通道/请求消费约束，以及两入口共用的当前Predicate来源检查。Round5.6的集合SET严格数组Schema、Context说明和所有原有hard guards仍应保留。

本轮未部署或接管8088，未执行SQL/生产状态写入，未改模型配置、Catalog、API/SSE/UI或跨服务生产源码，因此无需数据库、Catalog或服务切流回滚。PRIVATE原始记录与回执保持只读，不随代码回滚删去。
