"""公式生成的 prompt 组装，语法清单与前端公式编辑器一致。"""

SUPPORTED_SYNTAX = """
聚合函数: COUNT( SUM( AVG( MAX( MIN(
算术运算符: + - * /
比较运算符: = != > < >= <=
逻辑运算符: AND OR NOT
SQL关键字: IN LIKE BETWEEN IS NULL DISTINCT AS
符号: ( ) , .
日期函数: TIMESTAMPDIFF(YEAR, TIMESTAMPDIFF(MONTH, TIMESTAMPDIFF(QUARTER, DATEDIFF( CURRENT_DATE YEAR( MONTH( QUARTER( DAY(
""".strip()

RULES = """
1. 字段引用必须使用"表名.字段名"的映射形式，禁止使用中文名
2. 字符串常量用单引号包裹，如 '退货'；日期常量用单引号，如 '2025-07-01'
3. 描述中出现"总额/合计/总和"用 SUM，"平均/均值"用 AVG，"数量/条数/次数"用 COUNT，"最大/最高"用 MAX，"最小/最低"用 MIN
4. 只输出计算公式本身（SQL 表达式片段），不要 SELECT、FROM 等查询语句，不要分号结尾
5. 不要输出任何解释、注释、markdown 围栏、"公式："等前缀
6. 无法根据给定字段生成时，输出一行以 #UNKNOWN# 开头的内容，并简述缺少什么
""".strip()


def build_prompt(
    description: str,
    structured: dict,
    attributes: list[dict],
    indicator_level: int,
    atomic_operators: list[dict],
    indicator_name: str,
    business_domain: str,
) -> str:
    parts = ["你是语义建模指标计算公式的生成器，根据业务语义描述生成 SQL 计算公式。\n"]

    if business_domain or indicator_name:
        ctx = "、".join(x for x in [business_domain, indicator_name] if x)
        parts.append(f"指标上下文: {ctx}\n")

    if indicator_level in (2, 3):
        ops = []
        for op in atomic_operators:
            name = op.get("name") or op.get("label") or ""
            value = op.get("value") or op.get("code") or op.get("indicatorCode") or ""
            if name and value:
                ops.append(f"  {name} -> {value}")
        if ops:
            parts.append("可用原子算子（引用时使用箭头后的编码）:")
            parts.append("\n".join(ops) + "\n")
    else:
        fields = []
        for attr in attributes:
            name = attr.get("name") or attr.get("attrName") or ""
            table = attr.get("mappingTable") or ""
            column = attr.get("mappingColumn") or ""
            if name and table and column:
                fields.append(f"  {name} -> {table}.{column}")
        if fields:
            parts.append("可用字段（引用时使用箭头后的映射形式）:")
            parts.append("\n".join(fields) + "\n")

    parts.append(f"支持的语法:\n{SUPPORTED_SYNTAX}\n")
    parts.append(f"生成规则:\n{RULES}\n")

    if description:
        parts.append(f"业务语义描述: {description}")
    if structured:
        cfg = "；".join(f"{k}={v}" for k, v in structured.items() if v not in (None, "", []))
        if cfg:
            parts.append(f"结构化配置: {cfg}")

    parts.append("\n只输出计算公式一行：")
    return "\n".join(parts)
