# 时间语义可行性

## 当前表达

- DataAnalysis 保存时间策略和规范区间，调用方契约见 `app/services/intent_asl_contract.py:62-202`。
- Oagnet ASL 通过 `time_context` 和时间维度的 `granularity` 表达，Schema见 `E:/YouoAgent/Oagnet/prompt_build.py:10-180`。
- SQL翻译器将时间范围与粒度转换为过滤、分组和排序，主流程见 `sql_translator_prod.py:2973-3186`。
- 目录中只有5/11指标声明时间锚点；交易日期维度支持日/周/月/季/年。

## 缺口

- **OBSERVED_FAILURE**：9个历史案例的时间粒度/投影在ASL阶段丢失或替换错误。
- `最近一年` 等相对时间依赖请求基准时刻，若基准晚于数据水位，会得到合法空结果。
- 边界语义、时区、自然周/财年、数据水位和“按月”是否要求补零没有统一契约。

## 目标对象

统一为 `anchor + range + grain + boundary + timezone + calendar + source + as_of`。系统推断必须回显来源；执行前比较查询区间与数据水位，但不得擅自改写用户区间。若区间在水位之后，应提供“保留区间执行”与“调整到可用数据期”两种明确选择。

## 可行性结论

**PROPOSAL**：现有解析器和SQL能力足以承载统一对象，主要工作是跨仓库契约一致性和目录补齐。先 shadow 生成新时间对象，再与现有 ASL/SQL 做等价性测试。

