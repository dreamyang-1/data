"""Prompt 构建 - 动态拼接Prompt：系统指令 + 召回知识 + 用户问题。"""
from __future__ import annotations

from collections import deque
import json
import re

from vector_store import ChromaVectorStore, SearchResult, normalize_vector_text
from scope_contract import normalize_domains, require_model_id, scope_filter, require_candidate_scope

SYSTEM_PROMPT = """
【角色】
你是一个业务查询理解专家，负责将用户的自然语言问题转换为结构化的 AST（抽象语法树）JSON，供下游 SQL 翻译器消费。

【任务】
根据用户问题 + 三份语义元数据（entities.yml / metrics.yml / dimensions.yml），生成严格符合 schema 的 AST JSON。

【输入分工】
- 补全后的完整问题表达用户当前需求；结构化提取提供意图、实体、指标、分组、展示字段和过滤条件的参考，不重新解释成另一项任务。
- 本轮 DSL/语义目录负责标准编码、字段、关系、口径和已发布默认值。业务描述文件辅助理解，不是物理字段或授权清单；描述重复或冲突时，不虚构连接关系。
- 只有调用方明确标记的结构契约/已确认选择属于硬约束；一般结构化提取可结合上下文修正。字段仍必须绑定到本轮目录。
- 不用固定词表代替业务判断，不因单个词出现而自动加指标、分组、筛选、时间范围或追问。通用说明和示例不是当前问题的新需求。

【AST Schema】（严格遵守字段名与类型）

{
  "version": "2.0",                  // AST版本号，固定 "2.0"
  "intent": "query",                 // 查询意图，固定 "query"
  "subject": {                       // 查询主体，由指标 source_dependency 推断
    "entity": "ent_xxx"              // 实体代码，如 ent_order, ent_customer
  },
  "metrics": [                       // 聚合指标列表；逐条记录/属性名单查询允许为空
    {
      "name": "指标编码",         // 必须来自 metrics.yml 的 metric_code，严禁用 表.字段 格式
      "alias": "指标标准名称",        // 必须使用所选指标的 metric_name，不能填用户简称或自行改写
      "time_anchor": null            // 时间锚点：null 用指标默认锚点，或填 "表名.字段名" 覆盖
    }
  ],
  "dimensions": [
    {
      "name": "dim_xxx",             // 必须来自 dimensions.yml 的 dim_code，或实体属性 field_mapping 的表.字段
      "attr": null,                  // 指向 dimensions.yml 中 bind_entities[].attr，用于多实体绑定时指定归属实体；单实体绑定或实体属性作维度时为 null
      "level": null,                 // 层级维度的下钻层级，如 "goods_l1"
      "granularity": null            // 仅时间维度使用：day / month / quarter / year
    }
  ],
  "filters": [
    {
      "field": "table.column",       // 【必填】表名.字段名 格式，避免多表同名字段歧义
      "operator": "= | != | > | >= | < | <= | IN | NOT IN | LIKE | BETWEEN",
      "value": "标量或数组"           // BETWEEN 时 value 为 [start, end] 两元素数组
    }
  ],
  "time_context": {                  // 时间上下文，type/unit/anchor 必填
    "type": "this_month | last_month | this_year | year | custom | range | today | yesterday",
    "start": null,                   // type=custom/range 时必填，格式 YYYY-MM-DD
    "end": null,                     // type=custom/range 时必填，格式 YYYY-MM-DD
    "value": null,                   // type=year 时必填，如 2026
    "unit": "day | month | quarter | year",
    "anchor": "order_info.pay_time"  // 【必填】时间锚点，必须是 表名.字段名 格式
  },
  "sort": {                          // 排序配置，无排序时为 null
    "field": "metric_code 或 dim_code 或 table.column",
    "direction": "ASC | DESC",
    "field_type": "metric | dimension | field"  // metric聚合指标 / dimension维度字段 / field普通字段
  },
  "limit": 10,                       // Top N，无则 null
  "having": [                        // HAVING子句条件列表 - 聚合后过滤，每条为完整SQL表达式，如 "COUNT(DISTINCT order_info.order_id) >= 2"；无则空数组
    "HAVING表达式"
  ],
  "ambiguity": [                     // 歧义列表 - 非空时翻译器将暂停并要求用户确认
    {
      "type": "metric | dimension | filter | time_anchor",
      "question": "向用户澄清的问题",
      "candidates": ["候选1", "候选2"]
    }
  ]
}

【time_context 条件必填规则】

| type | 必填字段 |
|------|---------|
| year | value |
| custom | start, end |
| range | start, end |
| today | （仅 type/unit/anchor） |
| yesterday | （仅 type/unit/anchor） |
| this_month / last_month / this_year | （仅 type/unit/anchor） |

【字段生成规则】

### 1. intent（意图）
- 固定为 `"query"`
- Top N / 排行榜：通过 sort + limit 表达，intent 仍为 "query"
- 时间趋势：通过时间维度 + sort 表达，intent 仍为 "query"
- 对比场景：通过多个 metrics 或 dimensions 分组表达，intent 仍为 "query"

### 2. subject（主实体）
- 汇总查询结合所选指标的 source_dependency 与已发布关系确定主体；明细/名单结合返回对象和关系路径确定主体，不能要求 metrics[0] 必须存在。
- 多指标来自不同实体不自动构成歧义，先检查目录已有的依赖与关系是否能支持当前查询。

### 3. metrics（指标）
- 匹配优先级：`metric_code` > `metric_name` > `synonyms`
- 复合指标（如客单价、退款率）需展开 `depend_metrics` / `depend_atom_metric` 时**不要展开**，直接输出复合指标 code，由下游 SQL 生成器递归处理
- `alias` 必须填写本轮召回中所选 `metric_code` 对应的 `metric_name`，不填用户原话、不缩写、不自行改名；目录缺少标准名称时使用该指标编码。即使 `calc_formula` 带有旧输出列别名，仍填写标准名称，由 SQL 翻译器统一输出列名与排序引用，不改计算表达式。`name` 仍必须填写标准编码，不能改成中文名称
- `time_anchor` 默认 null（用指标默认锚点）；若用户明确说"按下单时间"或"按付款时间"则覆盖为 `表名.字段名` 格式（如 `order_info.create_time`）
- **【强制】`name` 必须是 metrics.yml 中存在的 `metric_code`**，严禁使用 `表.字段` 格式（如 `goods_info.sales_volume`）；`表.字段` 格式仅用于 dimensions（实体属性作维度）和 filters.field
- **用户要求统计且 metrics.yml 中无匹配指标时**：不要从实体属性借用字段当 metric，也不要将最接近的指标当成已选指标；仅列出有业务含义证据的候选并报告未绑定的具体计算要求。用户只要求实体名称、属性值或关系名单时，metrics=[]，直接使用已发布属性投影，不要求“对应的明细指标”。
- **“明细/列表”不是指标名称**：根据用户实际要求的结果粒度判断。例如“逐笔订单，显示订单号和实付金额”应使用已发布订单属性作展示字段，metrics=[]；“各订单的实付金额合计”才涉及按订单汇总。不得因为出现某个词就改选其他实体或聚合指标。
- 确定确实需要聚合后，再按目录名称、别名和业务口径匹配指标。名称匹配不能把单笔金额条件或否定提及强制变成汇总指标。
- 对较宽泛的表达，先结合补全问题、结构化参考、已确认选择和目录业务默认值理解；已有足够依据时直接生成，不因同时召回多个相关指标就重新追问。确实缺少必要业务含义时才在 ambiguity 说明具体缺项。

### 4. dimensions（维度 / 明细展示字段）
- 先结合补全后的完整问题、业务上下文和目录含义，确定“一行结果代表什么”，再生成字段。结构化参考中的意图、维度和展示字段不是最终结论，允许根据完整语义纠正。
- 需要筛选范围内一个汇总值时，使用指标且 dimensions=[]；需要不同业务对象/类别/时间段之间的结果时，使用对应分组；需要逐条记录或名单时，dimensions 表达展示字段，metrics=[]。不要仅因实体出现在问题或参考中就加名称分组。
- “全部/各/每/总数/明细”等词只能作为语义线索，不能据此固定分类。理解其修饰对象、否定及对比关系：例如“不要按医院分开，只给全市合计”不分组；“医院数量最多的城市”需要按城市分组，即使没有“按”字；“区域全部医院总数”作为指标完整名称时，其内部文字不产生额外分组。
- 每个 dimensions 项都应能说明用户需要它作为分组或返回列的业务依据；不能为丰富展示自行添加。结构化参考维度为空不等于禁止分组；不为空也不等于必须保留，必须结合完整问题判断。只输出最终 ASL，不输出内部推理过程。
- 匹配优先级：`dim_code` > `dim_name` > `synonyms`
- **实体属性作维度**：若用户说"按商品名称""按客户年龄"等，维度字段不在 dimensions.yml 中，则 `name` 直接填 `表.字段`（如 `order_item_detail.goods_name`），`level` 和 `granularity` 为 null
- 时间维度：`name` 必须填写 dimensions.yml 中实际召回的 `dim_code`（例如 `statistical_date`）；除非元数据里的真实编码就是 `dim_date`，否则严禁使用通用占位名 `dim_date`。并根据用户说法填 `granularity`（日/月/季/年）
- 层级维度：若用户指定层级（如"一级类目"），填 `level`
- **attr（归属实体属性）**：当维度来自 dimensions.yml 时，查其 `bind_entities` 列表：
  - 若 `bind_entities` 有多个条目，**必须**从中选择与当前查询主体（subject.entity）匹配的条目，将 `attr` 填为该条目的 `attr` 值（如 `"2085638300251906049"`）
  - 若 `bind_entities` 只有一个条目，`attr` 填 null（无需消歧）
  - 若维度为实体属性作维度（`name` 为 `表.字段` 格式），`attr` 填 null
- “某等级医院渠道 / 仅保留某等级医院渠道”中，若语句明确是对医院等级或医疗机构进行筛选，“渠道”只是业务路径的口语表达；应使用已召回的医院实体及其等级/地区字段，不得改成渠道维度。只有用户明确要求按渠道类型、渠道名称分组时才选择渠道维度

### 5. filters（过滤条件）
- **【强制】`field` 必须是 `表名.字段名` 格式**，从 entities.yml 的 `attributes[].field_mapping` 查表
- **关系路径中的名称过滤**：依据目录映射选择名称字段或该名称对应的标准编码，不把名称文本直接填入编码字段。精确值用 = / IN，只有用户要求包含、前缀等模糊匹配时才用 LIKE，不自动给所有名称加通配符。
- **跨实体修饰词必须拆分**：一句话同时给出品牌/厂家、商品、地区等不同实体或属性值时，每个值必须写入各自召回属性的独立 filter。不得把品牌、厂家或地区文字拼进商品名称，也不得把商品名称拼进品牌/厂家名称；缺少相应实体、属性或关系路径时必须 ambiguity 追问
- **已注册的间接关系可直接执行**：若 relations 元数据已经用一个或多个桥接实体连通源条件实体和目标结果实体，必须沿该路径生成查询；不得因为两端没有直连关系而追问，也不得要求用户确认是否使用中间实体。用户已给出源实体过滤值和目标实体时，中间实体只作 JOIN 桥梁，不要再要求中间实体的具体名称。只有当当前作用域内的关系元数据确实不能连通时才追问
- 支持的操作符：`=`, `!=`, `>`, `>=`, `<`, `<=`, `IN`, `NOT IN`, `LIKE`, `BETWEEN`
- 数值范围（如 30-40 岁）可用 `BETWEEN`：`{ "field": "customer_info.age", "operator": "BETWEEN", "value": [30, 40] }`，也可拆成两个 filter（`>= 30` 和 `<= 40`）
- 枚举值过滤：value 用 label（如"已支付"）或 code（如 1）均可，但同一 filter 内统一
- 指标的 `global_filters`（如 `order_status = 1`）**不要**写入 filters，由下游自动注入
- 状态、品牌、类别、活跃度等业务含义由当前目录定义，不在提示词中固定解释成某种物理字段。日期属性使用日期条件，枚举属性使用已注册值；不能用邻近字段替代另一种业务含义。

### 6. time_context（时间范围）
- `type`、`unit`、`anchor` 三个字段**必填**
- 用户说"本月" → `type=this_month, unit=month`
- 用户说"上个月" → `type=last_month, unit=month`
- 用户说"今年" → `type=this_year, unit=year`
- 用户说"今天" → `type=today, unit=day`
- 用户说"昨天" → `type=yesterday, unit=day`
- 用户说"2026年" → `type=year, value=2026, unit=year`
- 用户说"最近30天" → `type=range, start=计算日期, end=今天, unit=day`
- 用户说"2026-01-01至2026-06-30" → `type=custom, start="2026-01-01", end="2026-06-30", unit=day`
- 用户说"去年" → `type=range, start=去年1月1日, end=去年12月31日, unit=year`
- 用户未要求时间且调用方未给适用的已确认时间口径时，time_context=null；不要把指标名称中的“年度”或无关示例当成本次时间条件。通用提示词不新增最近一年之类默认值。
- “正在销售 / 当前在售”等描述先按目录业务含义理解，可能是状态、关系或时间条件，不固定要求用户提供日期。
- `anchor` 必须是当前目录可执行的表.字段。汇总优先采用指标发布的 time_caliber.time_anchor；指标未定义时结合相关时间维度、主体日期属性及其业务含义选择。明细不要求存在指标时间锚点。只有当前时间要求确实无法绑定时才说明具体缺项；不能丢弃明确时间条件返回全时间结果。
- type=year 时必填 `value`；type=custom/range 时必填 `start` 和 `end`

### 7. sort（排序）
- Top N 问题（"前10""排名前5"）必填 sort + limit，无排序时 sort 为 null
- `field_type` **必填**：
  - `"metric"`：按聚合后指标值排序（如按销量、金额排序）→ SQL 体现为 `ORDER BY SUM(quantity) DESC`
  - `"dimension"`：按维度字段值排序（如按日期、名称排序）→ SQL 体现为 `ORDER BY order_date ASC`
  - `"field"`：按普通字段排序（非聚合、非维度的表字段）
- 默认 `direction = "DESC"`（Top N 场景）；时间趋势用 `ASC`

### 8. limit
- 仅 Top N 场景填写，否则 null

### 9. having（聚合后过滤）
- 对聚合结果进行过滤，区别于 filters（行级过滤）
- 每条为完整 SQL 表达式字符串，如 `"COUNT(DISTINCT order_info.order_id) >= 2"`、`"SUM(order_info.pay_amount) > 1000"`
- 字段必须用 `表名.字段名` 格式
- 无聚合后过滤时返回空数组 `[]`
- 常见场景：
  - "购买过2次以上的客户" → `["COUNT(DISTINCT order_info.order_id) >= 2"]`
  - "总消费金额超过1000的会员" → `["SUM(order_info.pay_amount) > 1000"]`

### 10. ambiguity（必要缺项）
- 不因出现某个词或多个召回候选自动追问。先利用完整问题、结构化参考、业务口径和已确认选择解决。
- 缺少某个非必要展示属性时，保留可以输出的字段；只报告该属性无法显示，不让整个可执行查询中断。不使用相似但含义不同的字段凑数。
- 真正影响所求指标、结果对象或必要约束且仍无法处理的缺项，说明具体字段/值及原因，不泛称“语义不明确”。没有这种缺项时 ambiguity=[]。

【关键约束】

1. **filters.field 必须带表名前缀**（如 `customer_info.age`），不能只写 `age`
2. **sort.field_type 必填**，区分 metric / dimension / field
3. **复合指标不展开**，直接输出 指标编码
4. **指标 global_filters 不写入 filters**，由下游注入
5. **实体属性作维度时**，name 用 `表.字段` 格式
6. **time_context.anchor 必须为 `表名.字段名` 格式**（如 `order_info.pay_time`）
7. **having 中字段必须用 `表名.字段名` 格式**，每条为完整 SQL 表达式字符串
8. **metrics.alias 使用已召回指标的 metric_name，不采用用户简称或公式中的旧列名；metrics.name 保留标准 metric_code**
9. **输出纯 JSON**，不要包裹 markdown 代码块，不要解释
10. **【空召回硬性约束】当 entities.yml / metrics.yml / dimensions.yml 三者均为 "(无召回)" 时，严禁凭空编造实体编码、指标编码或维度字段**——此时你对用户问题所需的业务对象一无所知，任何 AST 都是臆造。必须输出下列"空召回澄清"结构（metrics/dimensions 留空，ambiguity 非空），由下游暂停并向用户追问：
```json
{
  "version": "2.0",
  "intent": "query",
  "subject": { "entity": null },
  "metrics": [],
  "dimensions": [],
  "filters": [],
  "time_context": null,
  "sort": null,
  "limit": null,
  "having": [],
  "ambiguity": [
    {
      "type": "metric",
      "question": "当前业务域未召回到任何语义元数据（实体/指标/维度均为空），无法将问题转换为查询。请确认：1) 查询作用域（semantic_model_id / business_domain_id）是否正确；2) 该业务域是否已构建向量索引；3) 或用更具体的业务术语重新描述查询对象。",
      "candidates": []
    }
  ]
}
```
即便只有部分章节为空，也不得编造编码。只有完成当前问题确实需要该类元数据时，才报告该类元数据缺失。实体属性或关系名单查询允许 metrics=[]；不需要任何统计指标，不得因 metrics.yml 为空或没有对应的“明细指标”而追问。用户已明确要求名单/属性时直接选择已发布属性和关系路径，不再询问是否需要明细或统计。

【输出格式】

直接输出符合 schema 的 JSON，无任何额外文字。



【用户提示词模板（User Prompt）】

## 语义元数据

### entities.yml
{{entities_yml_content}}

### metrics.yml
{{metrics_yml_content}}

### dimensions.yml
{{dimensions_yml_content}}

【用户问题】
{{user_question}}

【当前时间】
{{current_date}}

【任务】
请根据上述语义元数据，将用户问题转换为符合 schema 的 AST JSON。
- 严格遵循 System Prompt 中的字段生成规则
- filters.field 必须带表名前缀
- sort.field_type 必填
- time_context.anchor 必须为 表名.字段名 格式
- 输出纯 JSON，无任何额外文字
"""



class PromptBuilder:
    """动态拼接Prompt：系统指令 + 召回知识 + 用户问题

    通过向量库按 type 分组召回与用户问题语义相关的实体/维度/指标，
    将业务知识结构化注入 prompt，供 LLM 生成 AST。
    """

    # Surface evidence contains business phrases extracted from the completed
    # question, not arbitrary tokenizer words.  Recall each phrase separately
    # while keeping both embedding cost and prompt size bounded.
    MAX_SURFACE_MENTIONS = 12
    MAX_PROMPT_RESULTS_PER_TYPE = 12

    def __init__(
        self,
        store: ChromaVectorStore,
        embed_fn,
        top_k: int = 5,
        semantic_model_id: int = None,
        business_domain_id: int = None,
        business_domain_ids: list[int] | tuple[int, ...] | None = None,
        preferred_metric_codes: list[str] | tuple[str, ...] | None = None,
        authoritative_entity_scope: bool = False,
        surface_mentions: list[str] | None = None,
    ):
        """
        Args:
            store: 向量存储实例
            embed_fn: 单条文本向量化函数 str -> list[float]
            top_k: 每种类型召回数量
            semantic_model_id: 语义建模 ID（必填，限定检索作用域）
            business_domain_id: 业务域 ID（可选，进一步限定业务域；
                为 None 时取该语义建模下全部数据）
            business_domain_ids: 兼容数组入口；当前仅支持空集合或一个不同业务域
        """
        self.store = store
        self.embed_fn = embed_fn
        self.top_k = top_k
        self.semantic_model_id = require_model_id(semantic_model_id)
        normalized_ids = normalize_domains(business_domain_id, business_domain_ids)
        self.business_domain_ids = normalized_ids
        self.business_domain_id = (
            normalized_ids[0] if len(normalized_ids) == 1 else None
        )
        self.preferred_metric_codes = list(dict.fromkeys(preferred_metric_codes or []))
        self.authoritative_entity_scope = bool(authoritative_entity_scope)
        self.surface_mentions = list(dict.fromkeys(surface_mentions or []))[
            :self.MAX_SURFACE_MENTIONS
        ]

    # -------- 检索 --------

    def _build_where(self, type_filter: str) -> dict:
        """Apply the current model and exact explicit domain to every role."""
        return scope_filter(self.semantic_model_id,
                            business_domain_ids=self.business_domain_ids,
                            record_type=type_filter)

    @staticmethod
    def _term_values(value) -> list[str]:
        """Flatten names/synonyms stored as native values or JSON strings."""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if decoded is not None and decoded != value:
                return PromptBuilder._term_values(decoded)
            return [
                item.strip().strip("\"'")
                for item in text.replace("，", ",").split(",")
                if item.strip().strip("\"'")
            ]
        if isinstance(value, (list, tuple, set)):
            values: list[str] = []
            for item in value:
                values.extend(PromptBuilder._term_values(item))
            return values
        return [str(value).strip()]

    @classmethod
    def _generic_sales_measure_query(cls, user_query: str) -> bool:
        query = str(user_query or "")
        generic_sales = bool(
            re.search(r"销售(?:情况|趋势|表现|数据|分析)", query, re.IGNORECASE)
            or re.search(
                r"(?:卖|售卖).{1,100}(?:分析报告|销售报告|经营报告|报告)",
                query,
                re.IGNORECASE,
            )
        )
        return bool(
            generic_sales
            and not re.search(
                r"含税|不含税|销售(?:总)?额|销售金额|销量|销售(?:总)?数量|"
                r"订单(?:数|笔数)|销售成本|毛利|利润",
                query,
                re.IGNORECASE,
            )
        )

    @classmethod
    def _is_sales_measure_metric(cls, result: SearchResult) -> bool:
        metadata = result.metadata or {}
        terms = [
            *cls._term_values(metadata.get("metric_name")),
            *cls._term_values(metadata.get("synonyms")),
        ]
        return bool(re.search(
            r"销售(?:总)?额|销售金额|销量|销售(?:总)?数量",
            " ".join(terms),
            re.IGNORECASE,
        ))

    @classmethod
    def _exact_match_length(cls, user_query: str, result: SearchResult) -> int:
        """Return the longest semantic name/synonym explicitly in the question."""
        metadata = result.metadata or {}
        keys_by_type = {
            "metric": ("metric_code", "metric_name", "synonyms"),
            "entity": ("entity_code", "entity_name", "entity_alias"),
            "attribute": (
                "attr_code", "attr_name", "description", "parent_name",
            ),
            "relation": (
                "relation_code", "relation_name", "relation_semantic",
                "description", "parent_name", "target_entity",
            ),
            "dimension": ("dim_code", "dim_name", "synonyms"),
            "entity_attribute_value": (
                "entity_name", "entity_alias", "attr_name", "attr_code", "attr_value",
            ),
        }
        query_folded = user_query.casefold()
        matches: list[int] = []
        for key in keys_by_type.get(str(metadata.get("type")), ()):
            for term in cls._term_values(metadata.get(key)):
                folded = term.casefold()
                # Single-character labels are too ambiguous to override vector rank.
                if len(folded) >= 2 and folded in query_folded:
                    matches.append(len(folded))
        return max(matches, default=0)

    @classmethod
    def _rerank_exact_mentions(
        cls,
        user_query: str,
        results: list[SearchResult],
        top_k: int,
    ) -> list[SearchResult]:
        """Hybrid rerank: exact business terms first, vector similarity second."""
        deduped: list[SearchResult] = []
        seen: set[tuple] = set()
        for item in sorted(results, key=lambda value: value.score, reverse=True):
            metadata = item.metadata or {}
            item_type = str(metadata.get("type") or "")
            if item_type == "metric":
                key = (item_type, str(metadata.get("metric_code") or item.id))
            elif item_type == "entity":
                key = (item_type, str(metadata.get("entity_code") or item.id))
            elif item_type == "attribute":
                key = (
                    item_type,
                    str(metadata.get("parent") or ""),
                    str(metadata.get("attr_code") or item.id),
                )
            elif item_type == "relation":
                key = (
                    item_type,
                    str(metadata.get("parent") or ""),
                    str(metadata.get("relation_code") or item.id),
                )
            elif item_type == "dimension":
                key = (item_type, str(metadata.get("dim_code") or item.id))
            elif item_type == "entity_attribute_value":
                key = (
                    item_type,
                    metadata.get("semantic_model_id"),
                    metadata.get("business_domain_id"),
                    str(metadata.get("entity_name") or ""),
                    str(metadata.get("attr_code") or ""),
                    str(metadata.get("attr_value") or ""),
                )
            else:
                key = (item_type, item.id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        scored = [
            (cls._exact_match_length(user_query, item), item)
            for item in deduped
        ]
        scored.sort(
            key=lambda pair: (pair[0] > 0, pair[0], pair[1].score),
            reverse=True,
        )
        return [item for _, item in scored[:top_k]]

    @staticmethod
    def _decoded_metadata(value):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    @classmethod
    def _physical_field(cls, value) -> str | None:
        value = cls._decoded_metadata(value)
        if isinstance(value, dict):
            table = value.get("mappingTable") or value.get("table")
            column = value.get("mappingColumn") or value.get("column")
            value = f"{table}.{column}" if table and column else None
        if not isinstance(value, str):
            return None
        field = value.strip()
        parts = field.split(".")
        if len(parts) != 2 or not all(parts):
            return None
        return field

    @classmethod
    def _is_time_dimension(cls, result: SearchResult) -> bool:
        """Return whether a scoped dimension is registered as a time dimension."""
        metadata = result.metadata or {}
        granularities = cls._decoded_metadata(
            metadata.get("granularity_support")
        ) or []
        if isinstance(granularities, str):
            granularities = [granularities]
        if isinstance(granularities, list) and granularities:
            return True
        semantic_text = " ".join(
            str(metadata.get(key) or "")
            for key in (
                "dim_code", "dim_name", "dim_type", "synonyms",
                "business_definition",
            )
        )
        return bool(re.search(r"时间|日期|time|date", semantic_text, re.IGNORECASE))

    @staticmethod
    def _requires_time_dimension_recall(user_query: str) -> bool:
        """Return whether the question explicitly requires a time dimension.

        The caller marker remains authoritative for sales-record semantics.  A
        trend, comparison, time grouping, or explicit relative period also
        requires a scoped time dimension even when the caller did not add that
        marker.  This only widens the time-dimension slice; it never authorizes
        records outside the current model/domain.
        """
        text = str(user_query or "")
        if "TRANSACTION_TIME_SCOPE=SALES_RECORD" in text:
            return True
        return bool(re.search(
            r"趋势|走势|同比|环比|按(?:日|天|周|月|季度|季|年)|"
            r"(?:最近|近|过去|本|上)(?:[一二三四五六七八九十两\d]+)?"
            r"(?:天|日|周|月|季度|季|年)",
            text,
            re.IGNORECASE,
        ))

    @staticmethod
    def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
        deduped: dict[str, SearchResult] = {}
        for result in results:
            deduped.setdefault(str(result.id), result)
        return list(deduped.values())

    def _load_scope_records(
        self,
        type_name: str,
        fallback: list[SearchResult],
    ) -> list[SearchResult]:
        """Load records only for deterministic completion of recalled anchors."""
        loader = getattr(self.store, "get_by_where", None)
        if not callable(loader):
            self._validate_record_scope(fallback)
            return self._dedupe_results(fallback)
        records = list(loader(self._build_where(type_name)))
        self._validate_record_scope(records)
        return self._dedupe_results(records)

    def _exact_entity_value_mentions(self) -> list[SearchResult]:
        """Load exact value hits without trusting an upstream role label.

        Surface extraction may call a brand a product (or the reverse).  The
        literal itself is still useful evidence, so look it up across every
        entity-attribute value in the already pinned model/domain scope.  This
        is a bounded list of at most six mentions and never broadens scope.
        """
        loader = getattr(self.store, "find_exact", None)
        if not callable(loader):
            loader = getattr(self.store, "get_by_where", None)
        if not callable(loader):
            return []
        records: list[SearchResult] = []
        base_where = self._build_where("entity_attribute_value")
        for mention in self.surface_mentions:
            canonical = normalize_vector_text(mention)
            if not canonical:
                continue
            matches = list(loader({
                "$and": [base_where, {"canonical_value": canonical}],
            }))
            self._validate_record_scope(matches)
            for item in matches:
                item.score = 1.0
            records.extend(matches)
        return self._dedupe_results(records)

    def _validate_record_scope(self, records):
        for record in records:
            require_candidate_scope(record.metadata, self.semantic_model_id, self.business_domain_ids)

    @staticmethod
    def _relation_endpoints(result: SearchResult) -> tuple[str, str] | None:
        metadata = result.metadata or {}
        source = str(metadata.get("parent") or "").strip()
        target = str(metadata.get("target_entity") or "").strip()
        if not source or not target or source == target:
            return None
        return source, target

    @classmethod
    def _shortest_relation_path(
        cls,
        start: str,
        end: str,
        relations: list[SearchResult],
        max_hops: int = 4,
    ) -> list[SearchResult]:
        """Return a deterministic, bounded undirected semantic relation path."""
        if not start or not end or start == end:
            return []
        adjacency: dict[str, list[tuple[str, SearchResult]]] = {}
        for relation in relations:
            endpoints = cls._relation_endpoints(relation)
            if endpoints is None:
                continue
            source, target = endpoints
            adjacency.setdefault(source, []).append((target, relation))
            adjacency.setdefault(target, []).append((source, relation))
        for edges in adjacency.values():
            edges.sort(key=lambda pair: (pair[0], str(pair[1].id)))

        queue = deque([(start, [], frozenset({start}))])
        while queue:
            node, path, visited = queue.popleft()
            if len(path) >= max_hops:
                continue
            for next_node, relation in adjacency.get(node, []):
                if next_node in visited:
                    continue
                next_path = [*path, relation]
                if next_node == end:
                    return next_path
                queue.append((next_node, next_path, visited | {next_node}))
        return []

    @classmethod
    def _entity_attributes(cls, metadata: dict) -> list[dict]:
        attributes = cls._decoded_metadata(metadata.get("attributes")) or []
        if not isinstance(attributes, list):
            return []
        return [item for item in attributes if isinstance(item, dict)]

    @staticmethod
    def _semantic_label(value) -> str:
        return re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+",
            "",
            str(value or ""),
        ).casefold()

    @classmethod
    def _unique_primary_name_fields(cls, metadata: dict) -> set[str]:
        """Return one explicit entity display-name field, or no field."""
        entity_name = cls._semantic_label(metadata.get("entity_name"))
        entity_code = cls._semantic_label(metadata.get("entity_code"))
        candidates: set[str] = set()
        for attribute in cls._entity_attributes(metadata):
            field = cls._physical_field(attribute.get("field_mapping"))
            if field is None:
                continue
            attr_code = cls._semantic_label(attribute.get("attr_code"))
            attr_name = cls._semantic_label(attribute.get("attr_name"))
            role = str(
                attribute.get("semantic_role")
                or attribute.get("attribute_role")
                or ""
            ).casefold()
            explicit = any(
                bool(attribute.get(flag))
                for flag in (
                    "is_main_attribute",
                    "is_primary_name",
                    "is_display_name",
                    "is_name",
                )
            ) or role in {"name", "display_name", "primary_name", "title"}
            label_match = bool(
                entity_name
                and attr_name in {
                    entity_name + suffix
                    for suffix in ("名称", "姓名", "标题", "name", "title")
                }
            )
            code_match = bool(
                entity_code
                and attr_code in {
                    entity_code + "name",
                    entity_code + "title",
                }
            )
            generic_name = attr_name in {
                "名称", "姓名", "标题", "name", "title", "label",
            }
            if explicit or label_match or code_match or generic_name:
                candidates.add(field)
        return candidates if len(candidates) == 1 else set()

    @classmethod
    def _relation_join_fields(cls, result: SearchResult) -> set[str]:
        metadata = result.metadata or {}
        join_key = cls._decoded_metadata(metadata.get("join_key")) or {}
        if not isinstance(join_key, dict):
            return set()
        return {
            field
            for field in (
                cls._physical_field(join_key.get("source_field")),
                cls._physical_field(join_key.get("target_field")),
            )
            if field
        }

    def _complete_relational_scope(
        self,
        direct_entities: list[SearchResult],
        direct_attributes: list[SearchResult],
        direct_relations: list[SearchResult],
        metrics: list[SearchResult],
        entity_attribute_values: list[SearchResult],
        entity_candidates: list[SearchResult],
        relation_candidates: list[SearchResult],
    ) -> tuple[list[SearchResult], list[SearchResult], list[SearchResult], dict]:
        """Complete only paths connecting records recalled from the same scope.

        Vector recall supplies the semantic anchors.  The full scoped relation
        graph is used solely to find bounded connector paths between those
        anchors.  Entity records added by this step are field-pruned so graph
        completion cannot authorize arbitrary attributes.
        """
        all_entities = self._load_scope_records("entity", entity_candidates)
        all_relations = self._load_scope_records("relation", relation_candidates)
        entity_by_code: dict[str, SearchResult] = {}
        for entity in [*direct_entities, *all_entities]:
            code = str((entity.metadata or {}).get("entity_code") or "").strip()
            if code:
                entity_by_code.setdefault(code, entity)

        direct_entity_codes = {
            str((entity.metadata or {}).get("entity_code"))
            for entity in direct_entities
            if (entity.metadata or {}).get("entity_code")
        }
        anchors = set(direct_entity_codes)
        for attribute in direct_attributes:
            parent = str((attribute.metadata or {}).get("parent") or "").strip()
            if parent:
                anchors.add(parent)

        # A selected metric's bound entity is part of the executable query
        # graph even when that entity did not survive entity top-k.  Without
        # this anchor, a profile/snapshot metric can be authorized as the
        # subject while its path to the requested result dimension is absent
        # from the prompt, causing a false "no relation" clarification.
        metric_entity_codes: set[str] = set()
        for metric in metrics:
            dependency = self._decoded_metadata(
                (metric.metadata or {}).get("source_dependency")
            ) or {}
            bound_entities = (
                self._decoded_metadata(dependency.get("bind_entity"))
                if isinstance(dependency, dict)
                else []
            ) or []
            if isinstance(bound_entities, str):
                bound_entities = [bound_entities]
            for value in bound_entities if isinstance(bound_entities, list) else []:
                code = str(value or "").strip()
                if code in entity_by_code:
                    metric_entity_codes.add(code)
        anchors.update(metric_entity_codes)

        # Entity-value recall is also a semantic anchor.  Resolve its display
        # name against entities already constrained to this model/domain, and
        # authorize only the exact referenced attribute on a completed entity.
        value_attribute_refs: set[tuple[str, str]] = set()
        for value in entity_attribute_values:
            metadata = value.metadata or {}
            entity_name = str(metadata.get("entity_name") or "").strip()
            attr_code = str(metadata.get("attr_code") or "").strip()
            matches = {
                code
                for code, entity in entity_by_code.items()
                if entity_name
                and entity_name in {
                    code,
                    str((entity.metadata or {}).get("entity_name") or "").strip(),
                }
            }
            if len(matches) == 1:
                entity_code = next(iter(matches))
                anchors.add(entity_code)
                if attr_code:
                    value_attribute_refs.add((entity_code, attr_code))

        selected_relations: dict[str, SearchResult] = {}
        for result in direct_relations:
            endpoints = self._relation_endpoints(result)
            if endpoints is not None and anchors.intersection(endpoints):
                selected_relations[str(result.id)] = result
        sorted_anchors = sorted(code for code in anchors if code in entity_by_code)
        for index, source in enumerate(sorted_anchors):
            for target in sorted_anchors[index + 1:]:
                for relation in self._shortest_relation_path(
                    source,
                    target,
                    all_relations,
                ):
                    selected_relations.setdefault(str(relation.id), relation)

        required_entity_codes = set(anchors)
        for relation in selected_relations.values():
            endpoints = self._relation_endpoints(relation)
            if endpoints is not None:
                required_entity_codes.update(endpoints)

        allowed_completed_fields: set[str] = set()
        for attribute in direct_attributes:
            field = self._physical_field(
                (attribute.metadata or {}).get("field_mapping")
            )
            if field:
                allowed_completed_fields.add(field)
        for relation in selected_relations.values():
            allowed_completed_fields.update(self._relation_join_fields(relation))
        # A completed endpoint may be filtered by a natural-language name. Add
        # only its uniquely identifiable display-name attribute; all other
        # non-path attributes remain pruned and unauthorized.
        for entity_code in required_entity_codes - direct_entity_codes:
            entity = entity_by_code.get(entity_code)
            if entity is not None:
                allowed_completed_fields.update(
                    self._unique_primary_name_fields(entity.metadata or {})
                )
        for entity_code, attr_code in value_attribute_refs:
            entity = entity_by_code.get(entity_code)
            if entity is None:
                continue
            for attribute in self._entity_attributes(entity.metadata or {}):
                if str(attribute.get("attr_code") or "") == attr_code:
                    field = self._physical_field(attribute.get("field_mapping"))
                    if field:
                        allowed_completed_fields.add(field)

        selected_relation_codes = {
            str((relation.metadata or {}).get("relation_code") or "")
            for relation in selected_relations.values()
        }
        completed_entities: list[SearchResult] = []
        for code in sorted(required_entity_codes - direct_entity_codes):
            entity = entity_by_code.get(code)
            if entity is None:
                continue
            metadata = dict(entity.metadata or {})
            metadata["attributes"] = [
                attribute
                for attribute in self._entity_attributes(metadata)
                if self._physical_field(attribute.get("field_mapping"))
                in allowed_completed_fields
            ]
            relations = self._decoded_metadata(metadata.get("relations")) or []
            if isinstance(relations, list):
                metadata["relations"] = [
                    relation
                    for relation in relations
                    if isinstance(relation, dict)
                    and str(relation.get("relation_code") or "")
                    in selected_relation_codes
                ]
            completed_entities.append(SearchResult(
                id=entity.id,
                score=entity.score,
                text=entity.text,
                metadata=metadata,
            ))

        entities = self._dedupe_results([*direct_entities, *completed_entities])
        relations = self._dedupe_results(list(selected_relations.values()))
        completion = {
            "direct_entity_codes": sorted(direct_entity_codes),
            "metric_anchor_entity_codes": sorted(metric_entity_codes),
            "anchor_entity_codes": sorted(anchors),
            "completed_entity_codes": sorted(
                required_entity_codes - direct_entity_codes
            ),
            "relation_record_ids": [str(item.id) for item in relations],
            "allowed_completed_fields": sorted(allowed_completed_fields),
        }
        return entities, direct_attributes, relations, completion

    def retrieve(
        self,
        user_query: str,
    ) -> dict[str, list[SearchResult] | dict]:
        """召回语义锚点，并在同一作用域内补全受限关系路径。

        Returns:
            实体、属性、关系、指标、维度及关系补全诊断信息。
        """
        count = getattr(self.store, "count", None)
        if callable(count) and count() == 0:
            return {
                "entities": [],
                "attributes": [],
                "relations": [],
                "metrics": [],
                "dimensions": [],
                "entity_attribute_values": [],
                "_relational_completion": {},
            }
        vec = self.embed_fn(user_query)
        mention_vectors = [self.embed_fn(text) for text in self.surface_mentions
                           if text and text != user_query]
        # Recall a small candidate pool, then deterministically put exact business
        # names/synonyms first. Pure vector top-3 previously omitted the canonical
        # “销售额” metric even when that exact synonym appeared in a cross-domain
        # question, allowing a nearby “商品总金额” metric to be selected instead.
        candidate_k = min(40, max(12, self.top_k * 4))
        exact_entity_value_mentions = self._exact_entity_value_mentions()

        candidate_pools: dict[str, list[SearchResult]] = {}

        def retrieve_type(type_name: str) -> list[SearchResult]:
            # Short names, brands and model numbers are easy to misclassify and
            # weak dense-search inputs.  Widen only their hidden candidate pool;
            # the prompt-facing result remains compact below.
            search_k = 40 if type_name == "entity_attribute_value" else candidate_k
            candidates = self.store.search(
                vec,
                top_k=search_k,
                where=self._build_where(type_name),
            )
            self._validate_record_scope(candidates)
            if type_name == "entity_attribute_value":
                candidates = self._dedupe_results([
                    *exact_entity_value_mentions,
                    *candidates,
                ])
            mention_hits = []
            for mention_vec in mention_vectors:
                mention_k = (
                    max(8, self.top_k)
                    if type_name == "entity_attribute_value"
                    else self.top_k
                )
                extra = self.store.search(mention_vec, top_k=mention_k,
                                          where=self._build_where(type_name))
                self._validate_record_scope(extra)
                mention_hits.extend(extra)
                candidates = self._dedupe_results([*candidates, *extra])
            candidate_pools[type_name] = list(candidates)
            if type_name == "metric" and self.preferred_metric_codes:
                allowed = set(self.preferred_metric_codes)
                candidates = [
                    item for item in candidates
                    if str((item.metadata or {}).get("metric_code") or "") in allowed
                ]
                return self._rerank_exact_mentions(
                    user_query, candidates, max(self.top_k, len(allowed))
                )
            # Keep the bounded per-mention recall in the generation context.
            # Re-ranking only against the whole sentence would discard exactly
            # the qualifier/specification candidates this recall was added for.
            if type_name == "entity_attribute_value":
                # Keep the 40-row pool for deterministic disambiguation, but
                # expose only a small reranked set to generation.  Exact literal
                # hits and per-mention hits compete on value/name evidence here;
                # an upstream role label is never part of the ranking key.
                return self._rerank_exact_mentions(
                    user_query,
                    [*exact_entity_value_mentions, *mention_hits, *candidates],
                    max(12, self.top_k),
                )
            # Every business phrase contributes its own top-k search above.
            # Fuse those hits with the whole-question pool, then apply one
            # prompt-facing cap per semantic type.  The full fused pool remains
            # available in ``_ambiguity_candidates`` for deterministic checks.
            return self._rerank_exact_mentions(
                user_query,
                self._dedupe_results([*mention_hits, *candidates]),
                max(self.MAX_PROMPT_RESULTS_PER_TYPE, self.top_k),
            )

        direct_entities = retrieve_type("entity")
        direct_attributes = retrieve_type("attribute")
        direct_relations = retrieve_type("relation")
        metrics = retrieve_type("metric")
        if self.authoritative_entity_scope and len(self.business_domain_ids) == 1:
            # Attribute-detail planning must not depend on a top-k embedding hit.
            # The business-domain scope is already explicit and bounded, so load
            # its published entity/attribute records deterministically. This also
            # makes a freshly rebuilt semantic snapshot effective immediately
            # when an attribute name is distant from the surrounding query text.
            direct_entities = self._load_scope_records("entity", direct_entities)
            direct_attributes = self._load_scope_records(
                "attribute", direct_attributes
            )
        if self.preferred_metric_codes:
            # Caller-bound metric IDs have already been resolved by the
            # orchestration layer.  They are an execution contract, not a
            # similarity-search hint, so a low vector rank must never make an
            # existing scoped metric look unavailable.  Load the exact
            # semantic-model/domain records deterministically; stores without
            # metadata lookup retain the bounded vector fallback for tests and
            # compatibility.
            allowed = set(self.preferred_metric_codes)
            scoped_metrics = self._dedupe_results([
                *candidate_pools.get("metric", []),
                *self._load_scope_records(
                    "metric", candidate_pools.get("metric", [])
                ),
            ])
            metrics = self._dedupe_results([
                item
                for item in scoped_metrics
                if str((item.metadata or {}).get("metric_code") or "") in allowed
            ])
        if (
            not self.preferred_metric_codes
            and self._generic_sales_measure_query(user_query)
        ):
            # “销售趋势/情况” does not identify amount versus quantity. Keep
            # the bounded recalled alternatives in the prompt/validator scope
            # so the deterministic ambiguity guard can ask instead of letting
            # vector rank silently choose a business definition.
            alternatives = self._rerank_exact_mentions(
                user_query,
                [
                    item
                    for item in self._load_scope_records(
                        "metric", candidate_pools.get("metric", [])
                    )
                    if self._is_sales_measure_metric(item)
                ],
                6,
            )
            metrics = self._dedupe_results([*alternatives, *metrics])[:8]
        dimensions = retrieve_type("dimension")
        # Dimension roles supplied by the caller are deterministic semantic
        # evidence, not merely vector-search hints.  Load the current scoped
        # dimension catalogue and retain exact name/code/synonym mentions even
        # when a long business sentence pushes them outside vector top-k.  The
        # bounded exact-match set keeps prompts compact and automatically tracks
        # renamed or newly published dimensions without embedding physical
        # fields in application code.
        scoped_dimensions = self._load_scope_records(
            "dimension", candidate_pools.get("dimension", [])
        )
        exact_dimensions = [
            item for item in scoped_dimensions
            if self._exact_match_length(user_query, item) > 0
        ]
        if exact_dimensions:
            dimensions = self._rerank_exact_mentions(
                user_query,
                [*exact_dimensions, *dimensions],
                max(self.top_k, min(len(exact_dimensions), 12)),
            )
        if self._requires_time_dimension_recall(user_query):
            # Time-bound aggregates and trends need a registered timestamp even
            # when geographic/product wording pushes it outside vector top-k.
            # Load only time dimensions from the same caller scope, then keep a
            # bounded set for deterministic prompt and validation reuse.
            scoped_time_dimensions = [
                item
                for item in scoped_dimensions
                if self._is_time_dimension(item)
            ]
            dimensions = self._rerank_exact_mentions(
                user_query,
                [*scoped_time_dimensions, *dimensions],
                max(self.MAX_PROMPT_RESULTS_PER_TYPE, self.top_k),
            )
        entity_attribute_values = retrieve_type("entity_attribute_value")
        entities, attributes, relations, completion = self._complete_relational_scope(
            direct_entities,
            direct_attributes,
            direct_relations,
            metrics,
            entity_attribute_values,
            candidate_pools.get("entity", []),
            candidate_pools.get("relation", []),
        )
        return {
            "entities": entities,
            "attributes": attributes,
            "relations": relations,
            "metrics": metrics,
            "dimensions": dimensions,
            "entity_attribute_values": entity_attribute_values,
            # Keep the bounded pre-rerank pools outside the prompt so the
            # deterministic ambiguity gate can inspect every vector hit that
            # competed for the same explicit user phrase.  These records are
            # never rendered into the model prompt and therefore do not expand
            # its semantic choice surface.
            "_ambiguity_candidates": {
                type_name: self._dedupe_results(items)
                for type_name, items in candidate_pools.items()
            },
            "_relational_completion": completion,
        }

    # -------- Prompt 构建 --------

    def build(self, user_query: str) -> str:
        """检索知识 + 拼接 prompt

        SYSTEM_PROMPT 末尾含占位符模板:
          {{entities_yml_content}} / {{metrics_yml_content}}
          {{dimensions_yml_content}} / {{user_question}} / {{current_date}}

        使用 str.replace 逐个替换（不用 .format 避免与 JSON schema 中的花括号冲突）
        """
        from datetime import date

        knowledge = self.retrieve(user_query)
        if self.preferred_metric_codes:
            allowed = set(self.preferred_metric_codes)
            knowledge["metrics"] = [
                item for item in knowledge.get("metrics", [])
                if str((item.metadata or {}).get("metric_code") or "") in allowed
            ]
            found = {
                str((item.metadata or {}).get("metric_code") or "")
                for item in knowledge["metrics"]
            }
            missing = allowed - found
            if missing:
                raise ValueError("caller-bound metric metadata is unavailable: " + ",".join(sorted(missing)))
        # Reuse the exact retrieval set for deterministic validation of the
        # model output.  This prevents semantic codes from being guessed.
        self.last_knowledge = knowledge

        # 三类知识分别格式化（模板已含 ### 标题，这里只输出记录列表）
        entity_records = [
            *knowledge.get("entities", []),
            *knowledge.get("attributes", []),
            *knowledge.get("relations", []),
            *knowledge.get("entity_attribute_values", []),
        ]
        entities_text = self._format_section(entity_records)
        metrics_text = self._format_section(knowledge.get("metrics", []))
        dimensions_text = self._format_section(knowledge.get("dimensions", []))

        if self.preferred_metric_codes:
            user_query += (
                "\n\n【指标硬约束】调用方已将指标解析为 "
                + str(self.preferred_metric_codes)
                + "。metrics必须且只能使用这些metric_code，不得替换、增加或遗漏。"
            )

        # 空召回检测：三块全空时在用户问题后追加强提醒，防止模型臆造 AST
        empty_sections = [
            name for name, text in [
                ("entities", entities_text),
                ("metrics", metrics_text),
                ("dimensions", dimensions_text),
            ] if text == "(无召回)"
        ]
        if len(empty_sections) == 3:
            user_query = (
                user_query
                + f"\n\n【系统提醒】以下语义元数据章节为空召回: {', '.join(empty_sections)}。"
                "请严格遵守 System Prompt 第 10 条【空召回硬性约束】："
                "不得凭空编造实体/指标/维度编码，必须输出空召回澄清结构（ambiguity 非空）以触发追问。"
            )
        elif empty_sections:
            user_query += (
                f"\n\n【目录提示】以下章节无召回: {', '.join(empty_sections)}。"
                "这本身不构成查询失败：明细可以没有指标，汇总总数可以没有维度，"
                "实体属性可以作展示字段。仅在当前任务实际需要且无法绑定时报告具体缺项。"
            )

        current_date = date.today().isoformat()

        prompt = SYSTEM_PROMPT
        prompt = prompt.replace("{{entities_yml_content}}", entities_text)
        prompt = prompt.replace("{{metrics_yml_content}}", metrics_text)
        prompt = prompt.replace("{{dimensions_yml_content}}", dimensions_text)
        prompt = prompt.replace("{{user_question}}", user_query)
        prompt = prompt.replace("{{current_date}}", current_date)
        return prompt

    # 字段名 → 中文标签映射（覆盖实体/属性/关系/维度/枚举/指标全部字段）
    FIELD_LABELS: dict[str, str] = {
        # 通用
        "type": "类型",
        "code": "编码",
        "name": "名称",
        "status": "状态",
        "description": "描述",
        "parent": "父级编码",
        "parent_name": "父级名称",
        "score": "匹配度",
        # 实体
        "entity_code": "实体编码",
        "entity_name": "实体名称",
        "entity_alias": "实体别名",
        "business_domain": "业务域",
        "update_frequency": "更新频率",
        "attributes": "属性列表",
        "physical_table_join": "物理表关联",
        "base_table": "基础表",
        "join_logic": "关联逻辑",
        "relations": "关系列表",
        "bind_assets": "绑定资产",
        # 属性
        "attr_code": "属性编码",
        "attr_name": "属性名称",
        "attr_code": "属性编码",
        "attr_value": "属性值",
        "attr_description": "属性描述",
        "entity_description": "实体描述",
        "data_type": "数据类型",
        "field_mapping": "字段映射",
        "is_nullable": "是否可空",
        "enum_values": "枚举值",
        "value": "值",
        "label": "标签",
        # 关系
        "relation_code": "关系编码",
        "relation_name": "关系名称",
        "target_entity": "目标实体",
        "relation_semantic": "关系语义",
        "relation_constraint": "关系约束",
        "join_key": "关联键",
        # 维度
        "dim_code": "维度编码",
        "dim_name": "维度名称",
        "synonyms": "同义词",
        "business_desc": "业务描述",
        "dim_type": "维度类型",
        "dim_hierarchy": "维度层级",
        "enum_list": "枚举值列表",
        "special_rules": "特殊规则",
        "bind_metrics": "绑定指标",
        # 指标
        "metric_code": "指标编码",
        "metric_name": "指标名称",
        "metric_level": "指标级别",
        "unit": "单位",
        "format_rule": "格式规则",
        "business_definition": "业务定义",
        "application_scenes": "应用场景",
        "time_caliber": "时间口径",
        "stat_cycle": "统计周期",
        "time_anchor": "时间锚点",
        "calculation_rule": "计算规则",
        "depend_atom_metric": "依赖原子指标",
        "calc_formula": "计算公式",
        "global_filters": "全局过滤条件",
        "filter_type": "过滤类型",
        "condition": "条件",
        "desc": "说明",
        "bind_dimensions": "绑定维度",
        "source_dependency": "来源依赖",
        "bind_entity": "绑定实体",
        "bind_entities": "绑定实体列表",
        "attr": "属性ID",
        "entity": "实体ID",
        "attrName": "属性名称",
        "entityName": "实体名称",
        "mappingTable": "映射表",
        "mappingColumn": "映射字段",
        "businessDomain": "业务域",
        "permission_config": "权限配置",
        "view_roles": "可见角色",
        "data_limit": "数据权限限制",
        # 绑定资产子字段
        "bind_dimensions_list": "绑定维度列表",
        "bind_metrics_list": "绑定指标列表",
        "metric_logic": "指标逻辑",
    }

    def _format_section(self, records: list[SearchResult]) -> str:
        """单类型记录格式化为文本段（不含分类标题，模板里已有 ### 标题）

        Args:
            records: 同一类型的召回记录列表
        Returns:
            格式化后的文本，空召回时返回 "(无召回)"
        """
        if not records:
            return "(无召回)"
        lines: list[str] = []
        for i, r in enumerate(records, 1):
            lines.append(self._format_record(r, indent="", index=i))
        return "\n".join(lines)

    # 字段顺序模板：与 JSON 原始字段顺序保持一致
    # key 可以是 metadata.type（顶层记录）或父字段名（嵌套结构）
    FIELD_ORDER: dict[str, list[str]] = {
        # 顶层记录（按 metadata.type）
        "entity": [
            "entity_code", "entity_name", "entity_alias", "business_domain",
            "update_frequency", "description",
            "attributes", "physical_table_join", "relations", "bind_assets",
        ],
        "attribute": [
            "attr_code", "attr_name", "field_mapping", "data_type",
            "description", "enum_values", "is_nullable",
            "parent", "parent_name",
        ],
        "entity_attribute_value": [
            "entity_name", "entity_description", "attr_name", "attr_code",
            "attr_description", "attr_value",
        ],
        "relation": [
            "relation_code", "relation_name", "target_entity",
            "relation_semantic", "relation_constraint", "join_key", "description",
            "parent", "parent_name",
        ],
        "metric": [
            "metric_code", "metric_name", "synonyms", "business_domain",
            "metric_level", "unit", "format_rule",
            "business_definition", "time_caliber", "calculation_rule",
            "bind_dimensions", "source_dependency", "permission_config",
        ],
        "dimension": [
            "dim_code", "dim_name", "synonyms", "business_desc",
            "dim_type", "dim_hierarchy", "enum_list",
            "special_rules", "field_mapping", "bind_metrics", "bind_entities",
        ],
        "enum": [
            "code", "name", "status",
            "parent", "parent_name",
        ],
        # 嵌套子结构（按父字段名）
        "business_definition": ["description", "application_scenes"],
        "time_caliber": ["stat_cycle", "time_anchor"],
        "calculation_rule": ["depend_atom_metric", "calc_formula", "global_filters"],
        "physical_table_join": ["base_table", "join_logic"],
        "bind_assets": ["bind_metrics", "bind_dimensions"],
        "permission_config": ["view_roles", "data_limit"],
        "source_dependency": ["bind_entity"],
        # 列表项子结构（按 "<父字段名>_item" 推断）
        "attributes_item": ["attr_code", "attr_name", "field_mapping", "data_type", "description", "enum_values", "is_nullable"],
        "relations_item": ["relation_code", "relation_name", "target_entity", "relation_semantic", "relation_constraint", "join_key", "description"],
        "enum_list_item": ["code", "name", "status"],
        "enum_values_item": ["value", "label"],
        "global_filters_item": ["filter_type", "condition", "desc"],
        # bind_assets 子列表项
        "bind_metrics_item": ["metric_code", "metric_name", "metric_logic"],
        "bind_dimensions_item": ["dim_code", "dim_name"],
        # 维度绑定实体子列表项
        "bind_entities_item": ["attr", "entity", "attrName", "entityName", "mappingTable", "mappingColumn", "businessDomain"],
    }

    def _format_record(
        self,
        r: SearchResult,
        indent: str = "",
        index: int | None = None,
    ) -> str:
        """单条记录格式化为中文标签文本（递归展开嵌套结构）"""
        prefix = f"{indent}{index}. " if index else indent
        lines = [f"{prefix}匹配度: {r.score:.4f}"]
        # 顶层使用 metadata.type 决定字段顺序
        order_key = r.metadata.get("type", "")
        self._format_dict(r.metadata, indent=indent + "  ", lines=lines, order_key=order_key)
        return "\n".join(lines)

    def _format_dict(
        self,
        d: dict,
        indent: str,
        lines: list[str],
        order_key: str = "",
    ) -> None:
        """递归格式化 dict

        Args:
            order_key: 用于选择字段顺序的key
                       - 顶层: metadata.type (entity/metric/dimension/...)
                       - 嵌套: 父字段名 (business_definition/calculation_rule/...)
        """
        # 跳过的字段（已通过其他方式展示）
        SKIP_TOP = {"type", "score"}

        # 按字段顺序模板排序，未在模板中的字段追加在末尾（保持原顺序）
        ordered_keys = self._order_keys(d.keys(), order_key)

        for k in ordered_keys:
            if k in SKIP_TOP:
                continue
            v = d.get(k)
            label = self.FIELD_LABELS.get(k, k)
            if v is None or v == "":
                continue
            if isinstance(v, dict):
                lines.append(f"{indent}{label}:")
                # 嵌套 dict 用字段名作为 order_key
                self._format_dict(v, indent=indent + "  ", lines=lines, order_key=k)
            elif isinstance(v, list):
                if not v:
                    continue
                # 列表项为 dict 时逐项展开，否则用顿号拼接
                if all(isinstance(x, dict) for x in v):
                    lines.append(f"{indent}{label}:")
                    # 列表项的 order_key 为 "<父字段名>_item"
                    item_order_key = f"{k}_item"
                    for i, item in enumerate(v, 1):
                        lines.append(f"{indent}  {i}.")
                        self._format_dict(item, indent=indent + "    ", lines=lines, order_key=item_order_key)
                else:
                    parts = [str(x) for x in v if x]
                    if parts:
                        lines.append(f"{indent}{label}: {'、'.join(parts)}")
            else:
                lines.append(f"{indent}{label}: {v}")

    def _order_keys(self, keys, order_key: str) -> list[str]:
        """按字段顺序模板对 keys 排序，未在模板中的字段追加在末尾"""
        order = self.FIELD_ORDER.get(order_key, [])
        # 模板中存在的字段，按模板顺序输出
        ordered = [k for k in order if k in keys]
        # 未在模板中的字段，按原顺序追加
        ordered.extend([k for k in keys if k not in order])
        return ordered


if __name__ == "__main__":
    # 自检：构建一次 prompt
    from embedding import embed_query, embed_documents
    from vector_store import ChromaVectorStore, rebuild_index

    store = ChromaVectorStore()
    # 确保索引存在
    if store.count() == 0:
        print("向量库为空，先重建索引...")
        stats = rebuild_index(store, embed_documents)
        print(f"索引构建: {stats}")

    builder = PromptBuilder(
        store, embed_query, top_k=3,
        semantic_model_id=6, business_domain_id=7,  # 商超业务数据 / 销售交易域
    )

    for q in [
        "今年小程序渠道的支付GMV是多少？",
        "各渠道的订单量",
    ]:
        print("=" * 60)
        print(f"Query: {q}")
        print("=" * 60)
        print(builder.build(q))
        print()
