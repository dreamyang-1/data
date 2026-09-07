# ASL/NL2SQL 契约专项审计

## 结论

- **CURRENT_FACT**：真实链路为 `DataAnalysis_Agent → Oagnet /agent/query → sql-translator /api/translate → /api/execute`，三段源码均已取得，并非根据客户端接口反推服务内部行为。
- **CURRENT_FACT**：调用方契约能表达必需指标、投影、分组、过滤、时间策略、排序及数量；ASL 1.0 实际支持 `subject/metrics/dimensions/filters/time_context/sort/limit/having/projection_mode/ambiguity`。
- **OBSERVED_FAILURE**：比较、窗口、游标/偏移、显式 Join、通用 ResultContract 和语义角色没有一等 ASL 表达，只能依赖调用方补充字段、特殊分析契约或自然语言再解释。
- **CURRENT_FACT**：翻译器对目录中已声明的 1:N 聚合风险采取拒绝策略，没有自动预聚合修复。
- **PROPOSAL**：保留 ASL 1.0 兼容入口，增加版本化 Typed Logical Plan，再由适配器降级为 ASL 1.0；禁止一次性替换生产链路。

## 真实调用链证据

1. DataAnalysis 构造 Oagnet 请求：`app/adapters/http.py:1643-1673`。
2. Oagnet 请求模型与入口：`E:/YouoAgent/Oagnet/api.py:1149-1260,1335-1377`。
3. Oagnet 真实生成：`E:/YouoAgent/Oagnet/agent.py:6871-7163`。每次正常请求调用一次对话模型，仅格式错误允许一次重试；生成后还有确定性规范化和契约回填。
4. DataAnalysis 调用翻译/执行：`app/adapters/http.py:1930-1950,2033-2081`。
5. SQL 接口与翻译主流程：`E:/YouoAgent/sql-translator/api_server_prod.py:153-410`、`sql_translator_prod.py:2860-3186`。

## 边界能力

| 能力 | 当前表达 | 结论 |
|---|---|---|
| 多指标 | `metrics[]` | 支持，但结果投影完整性仅部分验证 |
| 时间粒度 | `dimensions[].granularity` | 支持；调用方前置契约仅以布尔/策略间接表达 |
| 排序/Top N | `sort` + `limit` | 支持；结果层缺少通用顺序证明 |
| 关系目标 | 目录关系补路径 | 支持推断，不是显式 typed relation target |
| 比较 | 分析操作符/特殊契约 | 非一等 ASL |
| 1:N 聚合安全 | 发现后拒绝 | 有保护，无自动预聚合 |
| 分页 | 无 offset/cursor | 不支持 |
| 窗口计算 | 无 | 不支持 |

完整字段对照见 `asl_nl2sql_contracts.json` 与 `asl_nl2sql_gap_matrix.csv`。

## 验证边界

- **CURRENT_FACT**：SQL 只读防护位于 `app/adapters/http.py:3741-3771` 和 `sql_translator_prod.py:3256+`。
- **CURRENT_FACT**：DataAnalysis 数据集模型检查列唯一性、行数、有限数值、时区和水位字段，见 `app/domain/models.py:746-808`。
- **CURRENT_FACT**：分析契约只覆盖六类操作及列/行边界，见 `sql-translator/analysis_contract.py:19-160`。
- **UNKNOWN**：生产数据规模下所有关系基数声明是否完整准确；本轮不允许写库或构造生产数据验证。

