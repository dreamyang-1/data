"""Request-local arithmetic over the data already supplied to synthesis."""
from __future__ import annotations

import json
from decimal import Decimal, localcontext
from app.numeric_operations import aggregate_numeric, evaluate, finite, number


CALCULATOR_GUIDANCE = """
洞察计算工具：insight_calculator 可对本轮已返回数据做新的汇总、计数、均值、占比及差异计算。
需要计算时调用工具，不要在文字生成中口算；按 source 和字段引用完整原始行，避免手抄数字漏行。
计算结果中的 source 可作为下一次调用的数据源；四则运算的 left/right 可引用 {source,row,field}，
也可用 {value:数值} 表示明确常量（例如百分比换算的100）。分组汇总结果同时返回 row_count。
只依据成功工具结果描述计算数值；失败时修正参数或说明无法计算，不能把错误当零。
sample_only 或上游截断范围仍然有效，计算不扩大查询范围，不修改 ASL，不查询外部数据。
不同单位、不同业务口径、重叠去重计数不可直接合并。工具只验证数学操作，不证明业务归因。
"""

CALCULATOR_TOOL = {
    "type": "function", "function": {
        "name": "insight_calculator",
        "description": "计算当前请求数据。aggregate按字段汇总，可按group_by分组；arithmetic进行引用结果的四则运算。返回新source供后续计算。",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": ["aggregate", "arithmetic"]},
                "source": {"type": "string"},
                "field": {"type": "string"},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "function": {"type": "string", "enum": ["sum", "count", "count_distinct", "avg", "min", "max", "median"]},
                "operator": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
                "left": {"type": "object", "properties": {"source": {"type": "string"}, "row": {"type": "integer"}, "field": {"type": "string"}, "value": {"type": "number"}}, "additionalProperties": False},
                "right": {"type": "object", "properties": {"source": {"type": "string"}, "row": {"type": "integer"}, "field": {"type": "string"}, "value": {"type": "number"}}, "additionalProperties": False},
            },
            "required": ["operation"],
        },
    },
}


class InsightCalculator:
    def __init__(self, payload: dict):
        self.sources = {}
        self.results = []
        for index, item in enumerate(payload.get("tasks") or [payload]):
            if item.get("status") not in (None, "COMPLETED"):
                continue
            query = (item.get("facts") or {}).get("query_data") or {}
            rows = query.get("rows")
            if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                self.sources[f"data_{index}"] = {
                    "rows": rows, "task_id": item.get("task_id"),
                    "sample_only": query.get("sample_only"),
                    "total_row_count_confirmed": query.get("total_row_count_confirmed"),
                }
        for result in payload.get("insight_calculations", []):
            if isinstance(result, dict) and isinstance(result.get("rows"), list):
                self.sources[result["source"]] = result
                self.results.append(result)

    def catalog(self):
        return [{"source": name, "task_id": data.get("task_id"),
                 "row_count": len(data["rows"]), "sample_only": data.get("sample_only"),
                 "columns": list(dict.fromkeys(k for row in data["rows"] for k in row))}
                for name, data in self.sources.items()]

    def _operand(self, ref):
        if not isinstance(ref, dict):
            raise ValueError("操作数必须是字段引用或明确数值常量")
        if set(ref) == {"value"}:
            return number(ref["value"])
        if set(ref) != {"source", "row", "field"}:
            raise ValueError("字段引用需包含source、row、field")
        rows = self.sources[ref["source"]]["rows"]
        if type(ref["row"]) is not int or not 0 <= ref["row"] < len(rows):
            raise ValueError("行索引超出本轮结果范围")
        return number(rows[ref["row"]][ref["field"]])

    def execute(self, arguments: str):
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("计算参数必须是对象")
            operation = args.get("operation")
            if operation == "aggregate":
                if set(args) - {"operation", "source", "field", "group_by", "function"}:
                    raise ValueError("不支持的汇总参数")
                data = self.sources[args["source"]]
                rows = data["rows"]
                groups = args.get("group_by", [])
                field, function = args.get("field"), args.get("function")
                if function not in {"sum", "count", "count_distinct", "avg", "min", "max", "median"}:
                    raise ValueError("不支持的统计函数")
                if not isinstance(groups, list) or any(not isinstance(k, str) for k in groups):
                    raise ValueError("group_by必须是字段名列表")
                if len(groups) != len(set(groups)) or any(k in {"value", "row_count"} for k in groups):
                    raise ValueError("分组字段重复或与结果列重名")
                required = groups + ([field] if function != "count" or field is not None else [])
                if any(not isinstance(k, str) or any(k not in row for row in rows) for k in required):
                    raise ValueError("字段不在本轮数据中")
                buckets = {}
                for row in rows:
                    key = tuple(row[k] for k in groups)
                    buckets.setdefault(key, []).append(row)
                if not rows and not groups and function == "count":
                    buckets[()] = []
                if len(buckets) > 200:
                    raise ValueError("分组超过200项，请减少分组字段；未返回部分结果冒充完整结果")
                result_rows = []
                for key, sample in buckets.items():
                    if function == "count":
                        value = sum(row.get(field) is not None for row in sample) if field else len(sample)
                    elif function in {"sum", "avg", "min", "max"}:
                        values = [number(row[field]) for row in sample if row[field] is not None]
                        if not values:
                            raise ValueError("没有可用于统计的非空数值样本")
                        with localcontext() as ctx:
                            ctx.prec = 50
                            total = sum(values, Decimal(0))
                            value = total if function == "sum" else total/len(values) if function == "avg" else min(values) if function == "min" else max(values)
                    else:
                        value = aggregate_numeric(sample, {"function": function, "field": field})
                    result_rows.append({**dict(zip(groups, key)), "value": finite(value), "row_count": len(sample)})
                if not result_rows:
                    raise ValueError("没有可计算的样本")
                scope = {k: data.get(k) for k in ("sample_only", "total_row_count_confirmed")}
            elif operation == "arithmetic":
                if set(args) - {"operation", "operator", "left", "right"} or args.get("operator") not in {"add", "subtract", "multiply", "divide"}:
                    raise ValueError("不支持的四则运算参数")
                value = evaluate(args["operator"], self._operand(args.get("left")), self._operand(args.get("right")))
                result_rows, scope = [{"value": finite(value)}], {"scope": "Referenced operands only; inherits their limitations"}
            else:
                raise ValueError("未知计算操作")
            source = f"calc_{len(self.results)}"
            result = {"source": source, "rows": result_rows, "inputs": args, **scope}
            self.sources[source] = result
            self.results.append(result)
            return result
        except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
            return {"error": str(exc), "status": "FAILED"}
