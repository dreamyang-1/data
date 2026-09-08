# SQL Translator 服务说明

## 当前 Semantic Scope 边界

业务后端决定授权，服务执行当前请求的范围。空业务域使用原有 `MODEL_WIDE` 路径；单一显式域使用独立的目录、缓存和规划实例；多个显式域返回 `EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED`。请求字段与 `authorized_semantic_scope` 不一致时返回 `REQUEST_SCOPE_INVALID`。

模型内的实体、指标和维度查找只使用 `semantic_model:{model_id}:...` 键。指定模型后，缺失记录不能回退到无模型的旧键；记录中声明的模型若与键的命名空间冲突，也不会进入缓存。

单域目录读取在存储查询前限制模型、域及实体归属。维度没有独立的业务域列，必须通过目录中的实体绑定证明归属，只保留本域绑定。实体和指标的 Redis 记录必须有相符的域信息；当前 MySQL 发布事实覆盖旧映射。单域请求不复用全模型缓存，不回退到其他数据源。

显式域 `/api/execute` 必须同时提交生成 SQL 的 `asl`。服务在当前范围内重新规划，严格核对 SQL 和数据源后才执行；修改 SQL、遗漏 ASL 或目录变更导致计划不同，均拒绝执行。响应返回 `scope_contract_version=single-domain-v1`、模型、业务域和 `authorized_scope_fingerprint`。旧服务未返回该证明时，Agent 拒绝使用响应。

定义 GET 支持 `scope=<URL 编码的 AuthorizedSemanticScope JSON>`，兼容单一 `business_domain_id`；名称解析和血缘 POST 使用同一范围对象。单域缓存刷新只确认请求级缓存模式，不清除其他请求的全模型缓存。未执行生产索引重建、发布或缓存刷新。

离线回归：`python scripts/run_offline_tests.py --output <新的结果文件.json>`。该入口禁用工作区 `.env` 加载，只允许本次测试创建的临时本机 HTTP 服务；不连接已有数据库、Redis 或 MinIO。

本服务同时提供：

1. ASL 到只读 SQL 的翻译与执行；
2. 指标名称解析；
3. 指标口径定义；
4. 指标字段级血缘；
5. Redis DSL 缓存刷新。

默认监听 `0.0.0.0:48000`。

```powershell
python -m pip install -r requirements.txt
python api_server_prod.py
```

## 接口

### 健康检查

```http
GET /health
GET /api/health
```

接口契约发现：`GET /openapi.json`。该文档列出 DataAnalysis Agent 健康探测所需的 SQL 与语义路径。

### ASL 翻译并执行

```http
POST /api/ast-to-sql
Content-Type: application/json

{
  "asl": "{...ASL JSON字符串...}",
  "modelId": "6"
}
```

为了兼容现有 DataAnalysis Agent，`result` 仍是二次编码的 JSON 字符串。调用方必须同时检查外层和内层的 `success`。

失败响应会额外返回稳定的 `code`、`retryable` 和 `request_id`。同一个合法 HTTP 请求中的翻译或执行失败仍使用 HTTP 200 兼容旧调用；JSON、参数、路径错误使用 4xx。

也可以使用拆分接口，在执行前独立审查 SQL：

```http
POST /api/translate
POST /api/execute
```

`/api/execute` 会尝试在同一 MySQL 只读一致性事务中取得数据与数据库 UTC 时间。列定义、行结构和行数全部核对成功时，响应额外包含：

```json
{
  "snapshot_id": "mysql-consistent:<sha256>",
  "data_as_of": "2026-08-26T12:05:51.873605Z",
  "quality_status": "PASS",
  "quality_checks": {
    "consistent_snapshot": true,
    "read_only_transaction": true,
    "column_contract_valid": true,
    "row_contract_valid": true,
    "row_count_reconciled": true
  }
}
```

快照指纹覆盖数据源身份、规范化后的只读 SQL、完整结果和数据库时间。数据库不支持一致性只读事务或任一结构校验失败时，不返回 `PASS`，调用方必须保持降级可靠性，禁止自行补造证明。

常用内层错误码：`SQL_GENERATION_FAILED`（语义或ASL不支持，通常需修正配置/问题）、`SEMANTIC_DSL_UNAVAILABLE`、`SEMANTIC_CATALOG_UNAVAILABLE`、`DATA_SOURCE_UNAVAILABLE`、`QUERY_TIMEOUT`、`DB_POOL_TIMEOUT`（这些临时故障的 `retryable=true`），以及 `DATA_SOURCE_AMBIGUOUS`、`CROSS_DATA_SOURCE_QUERY_UNSUPPORTED`、`DATA_SOURCE_SCOPE_MISMATCH`（需明确数据源或调整架构，不能盲目重试）。

### 指标解析

```http
POST /v1/semantic/metrics/resolve
Content-Type: application/json

{
  "semantic_model_id": 6,
  "metric_names": ["销售额", "GMV"]
}
```

解析仅在给定语义模型中按指标编码、标准名称和别名精确匹配，不做可能产生误命中的模糊猜测。状态为 `RESOLVED`、`NOT_FOUND` 或 `AMBIGUOUS`。

返回的 `metric_id` 使用复合格式：

```text
semantic_model_id:metric_code
```

例如：`6:actual_payment_amount`。该格式避免不同语义模型出现同名指标时串用口径。

### 指标定义

```http
GET /v1/semantic/metrics/{metric_id}/versions/current
```

示例：

```http
GET /v1/semantic/metrics/6%3Aactual_payment_amount/versions/current
```

返回业务定义、公式、单位、别名、全局过滤条件、依赖指标和绑定实体。当前数据库没有独立指标版本表，因此只支持 `current`（兼容 `latest`）。

### 指标血缘

```http
POST /v1/semantic/metrics/{metric_id}/lineage
Content-Type: application/json

{
  "version": "current"
}
```

返回指标、实体、物理表、物理字段节点及其 `PRODUCES`、`CALCULATES`、`CONTAINS`、`JOINS_TO` 关系。表/字段节点会带语义库注册 ID、数据类型、数据源 ID等信息；公式引用未在 `semantic_model_table` / `semantic_model_field` 注册时会进入 `metadata_warnings`。血缘来自指标、实体、表、字段和关系配置，不由大模型生成。

### 刷新 DSL 缓存

```http
POST /api/cache/refresh
Content-Type: application/json

{
  "modelId": 6
}
```

不传 `modelId` 表示清空全部进程内 DSL 缓存。

## SQL 安全与准确性规则

- 只允许单条 `SELECT`，禁止 DML、DDL、`UNION`、文件读写、注释和危险函数；
- 过滤字段、操作符、值类型、HAVING、排序、日期和 LIMIT 均经过白名单校验；
- 物理字段必须存在于当前语义模型元数据中；
- 未知指标和未知实体直接失败，不再生成 `NULL` 或猜测物理表；
- 缺少显式实体关系时不猜 JOIN；
- 已知一对多 JOIN 会使 `SUM`、`AVG` 或非去重 `COUNT` 重复累计时，查询被拒绝并要求配置安全聚合策略；
- `dim_date` 等通用日期表达会解析为当前主实体的日期字段。订单销售趋势会使用 `order_info.pay_time`，SELECT 与 GROUP BY 使用完全一致的粒度表达式；
- 周粒度使用 ISO 周年/周序号（`YYYY-Www`），避免跨年周排序错误；时间过滤采用左闭右开的字段范围，保留日期索引可用性；
- `semantic_model_id/modelId` 是必填正整数；同一查询涉及多个物理数据源时明确拒绝，不会任意选择一个库执行；
- 查询结果默认和最大均限制为 10000 行，可通过环境变量调整；
- MySQL 使用有界连接池，并设置连接、读写、连接池获取和查询超时。
- 响应默认不暴露数据源 host 和库名；确有内部诊断需求时才显式开启。

## 关键环境变量

服务优先使用进程环境变量，其次只读加载平台根目录 `.env`，代码中不保存数据库口令。

```text
SQL_TRANSLATOR_REDIS_HOST
SQL_TRANSLATOR_REDIS_PORT
SQL_TRANSLATOR_REDIS_DB
SQL_TRANSLATOR_REDIS_PASSWORD

SQL_TRANSLATOR_API_HOST
SQL_TRANSLATOR_API_PORT

SQL_TRANSLATOR_SEMANTIC_DB_HOST
SQL_TRANSLATOR_SEMANTIC_DB_PORT
SQL_TRANSLATOR_SEMANTIC_DB_USER
SQL_TRANSLATOR_SEMANTIC_DB_PASSWORD
SQL_TRANSLATOR_SEMANTIC_DB_DATABASE

SQL_TRANSLATOR_MAX_QUERY_ROWS
SQL_TRANSLATOR_DEFAULT_QUERY_ROWS
SQL_TRANSLATOR_MAX_METRICS_PER_QUERY
SQL_TRANSLATOR_MAX_DIMENSIONS_PER_QUERY
SQL_TRANSLATOR_MAX_FILTERS_PER_QUERY
SQL_TRANSLATOR_MAX_HAVING_PER_QUERY
SQL_TRANSLATOR_DB_POOL_SIZE
SQL_TRANSLATOR_DB_POOL_ACQUIRE_TIMEOUT
SQL_TRANSLATOR_DB_CONNECT_TIMEOUT
SQL_TRANSLATOR_DB_READ_TIMEOUT
SQL_TRANSLATOR_DB_WRITE_TIMEOUT
SQL_TRANSLATOR_DB_QUERY_TIMEOUT_MS
SQL_TRANSLATOR_MAX_REQUEST_BYTES
SQL_TRANSLATOR_EXPOSE_DATA_SOURCE_LOCATION
```

语义库变量未单独配置时，兼容读取平台现有的 `MYSQL_DEFAULT_*`，Redis 兼容读取 `REDIS_*`。

## 回归测试

```powershell
python -m unittest discover -v
```

测试覆盖日期映射和跨年周、过滤值转义、只读 SQL 防护、直接/反向/多跳 JOIN 基数、未知基数拒绝、请求参数与错误契约、指标解析/定义/依赖及字段级血缘、缓存隔离和旧接口兼容。

## DataAnalysis Agent 接入建议

将其语义服务和 SQL 服务都指向本服务：

```text
semantic_base_url=http://sql-translator.example.invalid:48000
sql_translator_base_url=http://sql-translator.example.invalid:48000
semantic_resolve_path=/v1/semantic/metrics/resolve
semantic_definition_path=/v1/semantic/metrics/{metric_id}/versions/{version}
semantic_lineage_path=/v1/semantic/metrics/{metric_id}/lineage
sql_translator_path=/api/ast-to-sql
```

这项配置需要在 DataAnalysis Agent 自己的环境中修改；本次代码审查没有修改该目录。
111111111111111
