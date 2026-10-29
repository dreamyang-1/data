# Round 5.11 Isolation Resources

- Run ID：`r51120260910t230900z`
- Redis namespace：`youo:data-analysis:v2:isolated:round5-11:<run_id>`
- TTL：7200秒
- Scope：semantic model 81 / explicit domain 205 / data source 58
- Application：Round 5.11 隔离 API 应用实例
- 资源清单：PRIVATE `redis_resources.json`，只包含本轮创建的精确 key
- 清理：6/6精确 key 已删除，删除后剩余0
- 禁止操作核对：scan=0，FLUSHDB=0，FLUSHALL=0，通配删除=0

隔离 namespace 强校验由 `RedisScalarSessionStore` 构造函数执行；生产 prefix 或缺少本轮 run id 的 prefix 会直接拒绝。连接地址、密码、业务 SQL、参数和值不写入本文件或 Git。
