from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx


SCENARIOS: dict[str, list[str]] = {
    "metric_evolution": [
        "查询最近一年上海地区含税销售总额", "按月统计", "改成按季度", "只看2026年", "那2025年呢",
        "换成销售数量", "同时加上订单数", "按经销商拆分", "显示前10名", "改成前5名",
        "从低到高排序", "只看上海市", "去掉地区条件", "按医院拆分", "只保留三级医院",
        "再按月看趋势", "哪个月最高", "最低的是哪个月", "最高和最低差多少", "计算平均值",
        "只显示月份和销售额", "把销售数量也加回来", "改看最近6个月", "按周统计", "还是按月吧",
        "导出Excel", "再导出PDF", "解释一下为什么最后一个月下降", "和上个月做环比", "总结刚才的分析",
    ],
    "entity_relationship": [
        "查询最近一年上海东松医疗科技股份有限公司销售的产品", "只显示产品名称", "按销售额排序", "前20个",
        "它合作了哪些医院", "统计医院数量", "只看三级医院", "这些医院分别在哪个城市", "只看上海",
        "按医院等级分组", "再看合作经销商数量", "经销商销售额前5名", "第1名销售什么产品", "这些产品卖给哪些医院",
        "改查上海美笛投资管理有限公司", "它销售哪些产品", "合作医院有多少家", "按月看销售趋势", "换成订单数",
        "只看2026年上半年", "那下半年呢", "按产品拆分", "销售额最高的产品", "这个产品覆盖哪些医院",
        "去掉时间限制看全部历史", "恢复最近一年", "导出完整结果", "只预览前10条", "生成一段业务摘要", "列出本轮使用的筛选条件",
    ],
    "correction_and_reset": [
        "查询最近一年上海地区销售额", "不是销售额，是订单量", "不是上海，是江苏", "按月统计", "不按月了，按季度",
        "去掉地区条件", "按经销商分组", "不要经销商维度，改按医院", "只看三级医院", "取消医院等级条件",
        "看前10名", "不是前10，是后5名", "改回前5名", "加上销售额", "去掉订单量",
        "比较2025年和2026年", "取消比较，只看2026年", "改成2026年7月", "不对，是2026年6月", "那5月呢",
        "另外问一下销售额的指标口径", "回到刚才查询", "按周统计", "显示趋势", "只显示日期和销售额",
        "清除刚才条件，查询经销商名单", "只看上海的", "显示前30条", "导出Excel", "开始新任务：查询医院等级有哪些",
    ],
    "natural_and_failure_prone": [
        "帮我看看最近一年生意怎么样", "具体看销售额", "按月给我", "上海怎么样", "那江苏呢",
        "两个地区对比一下", "哪个更高", "差多少", "为什么会有差异", "按产品看看原因",
        "卖得最好的三个产品", "第一个是谁卖的", "这些经销商还卖什么", "合作医院呢", "医院最多的经销商是谁",
        "看它每个月的变化", "最近三个月就行", "按天看看", "数据太多了，只看前20条", "只留销售额最高的5天",
        "撤销刚才的前5限制", "换成订单量", "也要销售数量", "算一下平均每单数量", "结果导出来",
        "刚才如果失败了就继续原来的问题", "再试一次", "不需要文件了，直接总结", "告诉我数据截止日期", "现在开始一个全新的问题：上海东松医疗科技股份有限公司都销售什么产品",
    ],
}


async def run_scenario(client: httpx.AsyncClient, base_url: str, name: str, questions: list[str]) -> dict:
    conversation_id = f"followup-30-{name}-{uuid4().hex[:8]}"
    turns = []
    for index, question in enumerate(questions, 1):
        started = time.perf_counter()
        error = None
        payload = None
        status_code = None
        try:
            response = await client.post(
                f"{base_url.rstrip('/')}/agent_chat",
                headers={
                    "X-Tenant-Id": "followup-eval",
                    "X-User-Id": f"user-{name}",
                    "X-Application-Id": "27",
                },
                json={
                    "application_id": "27",
                    "conversation_id": conversation_id,
                    "message_id": f"m{index:02d}-{uuid4().hex[:8]}",
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
        except Exception as exc:  # evaluation must retain later turns
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started
        turns.append({
            "turn": index,
            "question": question,
            "http_status": status_code,
            "status": payload.get("status") if isinstance(payload, dict) else None,
            "intent": payload.get("intent") if isinstance(payload, dict) else None,
            "missing_slots": payload.get("missing_slots", []) if isinstance(payload, dict) else [],
            "answer": (payload.get("answer", "")[:800] if isinstance(payload, dict) else ""),
            "file_count": len(payload.get("files", [])) if isinstance(payload, dict) else 0,
            "elapsed_seconds": round(elapsed, 3),
            "error": error,
        })
        print(json.dumps({"scenario": name, **turns[-1]}, ensure_ascii=False), flush=True)
    return {"scenario": name, "conversation_id": conversation_id, "turns": turns}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    timeout = httpx.Timeout(180.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(*[
            run_scenario(client, args.base_url, name, questions)
            for name, questions in SCENARIOS.items()
        ])
    turns = [turn for result in results for turn in result["turns"]]
    latencies = [turn["elapsed_seconds"] for turn in turns]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "scenario_count": len(results),
        "turns_per_scenario": {item["scenario"]: len(item["turns"]) for item in results},
        "total_turns": len(turns),
        "http_errors": sum(turn["http_status"] != 200 for turn in turns),
        "transport_errors": sum(bool(turn["error"]) for turn in turns),
        "needs_clarification": sum(turn["status"] == "NEEDS_CLARIFICATION" for turn in turns),
        "safe_fallback": sum(turn["status"] == "SAFE_FALLBACK" for turn in turns),
        "completed": sum(turn["status"] == "COMPLETED" for turn in turns),
        "files": sum(turn["file_count"] for turn in turns),
        "latency_mean": round(statistics.mean(latencies), 3),
        "latency_p95": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 3),
    }
    artifact = {"summary": summary, "scenarios": results}
    output = Path(args.output) if args.output else Path("evals/results") / f"followup-30turn-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
