from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "简单查询与常用分析",
        "turns": [
            "查询最近一年含税销售总额。",
            "按月统计空心纤维血液透析器产品的含税销售总额。",
            "查询最近一年销售过费森尤斯产品的经销商名单。",
            "查询上海东松医疗科技股份有限公司销售过哪些产品。",
            "统计最近一年合作医院数量。",
            "查询销售额最高的前十名经销商。",
            "查询下单次数最多的三个客户。",
            "查询最近三个月每月订单数量。",
            "查询上海地区三级医院名单。",
            "查询各经销商的含税销售总额并从高到低排序。",
            "查询最近一年销量最高的五个产品。",
            "查询2026年第二季度的销售明细。",
        ],
        "independent": True,
    },
    {
        "name": "连续追问与参数替换",
        "turns": [
            "查询最近一年上海地区的含税销售总额。",
            "按月统计。",
            "改成按季度。",
            "只看2026年。",
            "再加上订单数量。",
            "按经销商拆分。",
            "只显示前五名。",
            "改成后三名。",
            "取消地区限制。",
            "导出完整结果。",
        ],
    },
    {
        "name": "实体关系与指代追问",
        "turns": [
            "查询最近一年上海东松医疗科技股份有限公司销售过的产品。",
            "其中销售额最高的是哪个？",
            "它卖给了哪些医院？",
            "这些医院分别是什么等级？",
            "只保留三级医院。",
            "按医院的销售额排序。",
            "第一家医院还与哪些经销商合作？",
            "这些经销商的销售额分别是多少？",
            "改为查看上海华美投资管理有限公司。",
            "总结刚才两家经销商的差异。",
        ],
    },
    {
        "name": "澄清、中断与新任务识别",
        "turns": [
            "展示前三名。",
            "查询最近一年销售过费森尤斯产品的经销商名单。",
            "前三名。",
            "按销售额排序。",
            "算了，不查这个了，查询各医院等级有哪些。",
            "只看上海。",
            "不是医院名单，我要各等级对应的医院数量。",
            "另外解释一下含税销售总额的统计口径。",
            "回到医院数量，按城市拆分。",
            "开始新任务：查询最近三个月的销售趋势。",
        ],
    },
    {
        "name": "多维度拆解与多问题合并",
        "turns": [
            "按月份和产品统计最近一年销售额、销量和订单数，并给出环比。",
            "比较上海和江苏2025年与2026年的销售额、订单数和客户数。",
            "查询销量前十的产品，以及下单次数最多的三个客户。",
            "统计每家医院的订单总金额、订单笔数，并关联医院等级和所在城市。",
            "分析空心纤维血液透析器在各城市、各医院等级、各月份的销售趋势。",
            "找出销售额下降最大的五个产品，并分析涉及的经销商和医院。",
            "分别查询销售额最高的三家经销商和合作医院最多的三家经销商。",
            "比较费森尤斯与贝朗产品最近一年销售额、销量和覆盖医院数。",
            "生成上海地区最近一年销售分析报告，包含趋势、排名、异常和结论。",
            "查询单价最高的三个产品，并同时查询销售额最高的三个产品。",
        ],
        "independent": True,
    },
    {
        "name": "纠错、结果运算与输出",
        "turns": [
            "查询2026年1月至6月每月含税销售总额。",
            "最高和最低分别是哪个月？",
            "两者相差多少？",
            "计算这六个月的平均值。",
            "不对，把指标改成订单数量。",
            "按周统计。",
            "还是按月。",
            "只返回月份和订单数两个字段。",
            "导出Excel文件。",
            "不用文件了，直接总结主要变化。",
        ],
    },
    {
        "name": "意图覆盖与边界场景",
        "turns": [
            "你好，你能做什么？",
            "含税销售总额这个指标是怎么计算的？",
            "销售订单数据来自哪些表和字段？",
            "为什么2025年11月到12月销售额明显上升？",
            "检查最近一年有没有异常销售月份。",
            "预测未来三个月销售额。",
            "各产品销售额占总销售额的比例是多少？",
            "比较销售额前五名经销商的平均客单价。",
        ],
        "independent": True,
    },
    {
        "name": "相对时间与显式日期追问",
        "turns": [
            "查询最近一年销售过费森尤斯产品的经销商名单。",
            "那再查询一下最近一个月的。",
            "那最近半年呢？",
            "那查询一下25年12月1号到31号的。",
        ],
    },
]


def expanded_followups(question: str) -> list[str]:
    """Return two realistic, semantically different follow-ups per standalone case."""
    compact = question.replace(" ", "")
    if any(word in compact for word in ("你好", "能做什么", "支持什么")):
        return ["支持多轮追问和结果导出吗？", "给我三个可以直接测试的问题。"]
    if any(word in compact for word in ("口径", "怎么计算", "定义")):
        return ["再说明这个指标的单位和排除范围。", "用业务语言举一个计算例子。"]
    if any(word in compact for word in ("血缘", "来自哪些表", "数据来源")):
        return ["再说明中间的加工和聚合逻辑。", "再用业务名称总结一遍，不展示内部物理字段名。"]
    if any(word in compact for word in ("为什么", "原因", "归因")):
        return ["再按产品维度拆解贡献。", "改成按经销商维度解释，并列出前三个影响因素。"]
    if any(word in compact for word in ("预测", "预估", "预计")):
        return ["改成预测未来一个季度。", "在刚才预测基础上给出区间和主要风险。"]
    if any(word in compact for word in ("异常", "突增", "突降")):
        return ["改成按季度检查。", "只展示异常最明显的三个结果并解释判定依据。"]
    if any(word in compact for word in ("报告", "报表")):
        return ["再增加一个按季度的汇总表。", "不用重新查询，基于刚才报告总结三条结论。"]
    if "医院名单" in compact and not any(
        marker in compact for marker in ("销售", "订单", "最近", "过去")
    ):
        return [
            "只保留医院名称和医院等级。",
            "不要重新查询，基于刚才表格展示前5条。",
        ]
    # The first follow-up replaces a material query parameter. The second must
    # operate on the verified snapshot and therefore exercises table context,
    # attachment-backed previews, and no-repeat-query reuse.
    return [
        "把时间范围改成2025年10月1日至2025年12月30日，其他条件不变。",
        "不要重新查询，基于刚才结果用表格展示前5条，不足5条就全部展示。",
    ]


def expand_independent_scenarios(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for spec in specs:
        if not spec.get("independent"):
            expanded.append(spec)
            continue
        for index, question in enumerate(spec["turns"], 1):
            expanded.append({
                "name": f'{spec["name"]}/Q{index:02d}',
                "turns": [question, *expanded_followups(question)],
            })
    return expanded


def validate(
    payload: Any,
    status_code: int | None,
    error: str | None,
    *,
    question: str = "",
    strict_business: bool = False,
) -> list[str]:
    issues: list[str] = []
    if error:
        return [error]
    if status_code != 200:
        issues.append(f"HTTP_{status_code}")
    if not isinstance(payload, dict):
        return issues + ["响应不是JSON对象"]
    status = payload.get("status")
    if status not in {
        "COMPLETED", "PARTIAL_SUCCESS", "NEEDS_CLARIFICATION", "SAFE_FALLBACK"
    }:
        issues.append(f"未知状态:{status}")
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        issues.append("回答为空")
    if status == "COMPLETED" and any(x in answer for x in ("智能体配置错误", "Failed to fetch", "ASL 转 SQL 服务未能生成")):
        issues.append("完成状态包含链路错误")
    expected_watermark_empty = any(
        marker in answer
        for marker in ("数据只更新到", "业务数据截至", "完全位于数据水位之后")
    )
    if strict_business and status == "SAFE_FALLBACK" and not expected_watermark_empty:
        issues.append("业务链路降级")
    if (
        strict_business
        and status == "NEEDS_CLARIFICATION"
        and question.replace(" ", "") not in {"展示前三名。", "展示前三名"}
    ):
        issues.append("完整问题被误判为需要澄清")
    if strict_business and any(
        marker in answer.lower()
        for marker in ("sales_order.", "created_date", "product.category_id")
    ):
        issues.append("回答泄露内部物理字段")
    files = payload.get("files") or []
    if files and not any((item.get("download_url") or item.get("url")) for item in files if isinstance(item, dict)):
        issues.append("附件缺少下载地址")
    return issues


async def run_turn(
    client: httpx.AsyncClient,
    base_url: str,
    scenario: str,
    conversation_id: str,
    turn: int,
    question: str,
    strict_business: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    payload: Any = None
    status_code: int | None = None
    error: str | None = None
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/agent_chat",
            json={
                "application_id": "27",
                "conversation_id": conversation_id,
                "message_id": f"q{turn:02d}-{uuid4().hex[:10]}",
                "question": question,
                "semantic_model_id": 81,
                "business_domain_ids": [],
                "knowledge_base_names": [],
                "history": [],
                "use_longterm_memory": True,
            },
        )
        status_code = response.status_code
        payload = response.json()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.perf_counter() - started, 3)
    issues = validate(
        payload, status_code, error,
        question=question, strict_business=strict_business,
    )
    result = {
        "scenario": scenario,
        "turn": turn,
        "question": question,
        "http_status": status_code,
        "status": payload.get("status") if isinstance(payload, dict) else None,
        "intent": payload.get("intent") if isinstance(payload, dict) else None,
        "missing_slots": payload.get("missing_slots", []) if isinstance(payload, dict) else [],
        "answer": str(payload.get("answer") or "")[:1500] if isinstance(payload, dict) else "",
        "file_count": len(payload.get("files") or []) if isinstance(payload, dict) else 0,
        "elapsed_seconds": elapsed,
        "issues": issues,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


async def run_scenario(
    client: httpx.AsyncClient,
    base_url: str,
    spec: dict[str, Any],
    *,
    strict_business: bool = False,
) -> dict[str, Any]:
    scenario_id = uuid4().hex[:10]
    results = []
    for turn, question in enumerate(spec["turns"], 1):
        conversation_id = (
            f"live60-{scenario_id}-{turn}" if spec.get("independent") else f"live60-{scenario_id}"
        )
        results.append(await run_turn(
            client, base_url, spec["name"], conversation_id, turn, question,
            strict_business=strict_business,
        ))
    return {"name": spec["name"], "turns": results}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--output", default="")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--expanded-followups", action="store_true",
        help="为每个独立问题增加参数替换和结果集复用追问",
    )
    parser.add_argument(
        "--strict-business", action="store_true",
        help="将降级和非预期澄清视为业务失败",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="只运行名称中包含该文本的场景；可重复传入",
    )
    parser.add_argument(
        "--question-index", action="append", type=int, default=[],
        help="对独立问题场景只运行指定的1基序号；可重复传入",
    )
    args = parser.parse_args()
    selected = [
        spec for spec in SCENARIOS
        if not args.scenario or any(name in spec["name"] for name in args.scenario)
    ]
    if not selected:
        raise SystemExit("没有匹配的测试场景")
    if args.question_index:
        indexes = set(args.question_index)
        filtered: list[dict[str, Any]] = []
        for spec in selected:
            if not spec.get("independent"):
                filtered.append(spec)
                continue
            turns = [
                question for index, question in enumerate(spec["turns"], 1)
                if index in indexes
            ]
            if turns:
                filtered.append({**spec, "turns": turns})
        selected = filtered
        if not selected:
            raise SystemExit("没有匹配的问题序号")
    if args.expanded_followups:
        selected = expand_independent_scenarios(selected)
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    timeout = httpx.Timeout(240.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def guarded(spec: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await run_scenario(
                    client, args.base_url, spec,
                    strict_business=args.strict_business,
                )
        scenarios = await asyncio.gather(*(guarded(spec) for spec in selected))
    turns = [turn for scenario in scenarios for turn in scenario["turns"]]
    elapsed = [turn["elapsed_seconds"] for turn in turns]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "question_count": len(turns),
        "passed": sum(not turn["issues"] for turn in turns),
        "failed": sum(bool(turn["issues"]) for turn in turns),
        "completed": sum(turn["status"] == "COMPLETED" for turn in turns),
        "partial_success": sum(turn["status"] == "PARTIAL_SUCCESS" for turn in turns),
        "needs_clarification": sum(turn["status"] == "NEEDS_CLARIFICATION" for turn in turns),
        "safe_fallback": sum(turn["status"] == "SAFE_FALLBACK" for turn in turns),
        "files": sum(turn["file_count"] for turn in turns),
        "latency_mean": round(statistics.mean(elapsed), 3),
        "latency_p95": sorted(elapsed)[max(0, int(len(elapsed) * 0.95) - 1)],
    }
    artifact = {"summary": summary, "scenarios": scenarios}
    output = Path(args.output) if args.output else Path("evals/results") / f"live-60-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
