from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTION_FILE = ROOT / "测试问题"
FAILURE_MARKERS = (
    "上游数据服务暂时不可用",
    "本轮未生成独立分析结论",
    "结果未通过",
    "请稍后重试",
    "还需要补充",
    "智能体配置错误",
)


@dataclass(frozen=True)
class Contract:
    concepts: tuple[tuple[str, ...], ...]
    top_n: int | None = None
    unique_key: tuple[str, ...] = ()
    descending_metric: tuple[str, ...] = ()
    excluded_value: str | None = None
    forbidden_concepts: tuple[str, ...] = ()


# Each tuple is an OR group; every group must have one visible business label.
CONTRACTS: tuple[Contract, ...] = (
    Contract((("经销商",), ("含税销售总额", "销售总额")), unique_key=("经销商",)),
    Contract((("公司", "经销商"), ("含税销售总额", "销售总额"), ("省",), ("市",))),
    Contract((("经销商",), ("销售额",), ("合作时长",), ("合作次数",)), 5, ("经销商",), ("销售额",)),
    Contract((("医院",), ("含税", "总金额"), ("订单笔数",), ("医院等级",)), unique_key=("医院",)),
    Contract((("含税销售总额", "销售总额"),)),
    Contract((("月", "交易日期"), ("含税销售总额", "销售总额")), unique_key=("月", "交易日期")),
    Contract((("销售趋势", "月", "交易日期"), ("销售额", "销售总额"))),
    Contract((("已合作医院数", "合作医院数"),)),
    Contract((("已合作经销商数", "合作经销商数"),)),
    Contract((("医院",),), unique_key=("医院",)),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("含税销售总额", "销售总额"), ("销售总数量", "销售量"))),
    Contract((("月", "交易日期", "销售趋势"), ("销售",))),
    Contract((("医院",),), unique_key=("医院",)),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("科室",),)),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("经销商",), ("业务规模", "销售额")), unique_key=("经销商",), descending_metric=("业务规模", "销售额")),
    Contract((("经销商",), ("业务规模", "销售额")), unique_key=("经销商",), descending_metric=("业务规模", "销售额"), excluded_value="上海洁安"),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("经销商",), ("业务规模", "销售额")), unique_key=("经销商",), descending_metric=("业务规模", "销售额")),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("月", "交易日期"), ("含税销售总额", "销售总额")), unique_key=("月", "交易日期")),
    Contract((("销售趋势", "月", "交易日期"), ("销售额", "销售总额"))),
    Contract((("含税销售总额", "销售总额"), ("订单笔数",))),
    Contract((("月", "交易日期"), ("含税销售总额", "销售总额")), unique_key=("月", "交易日期")),
    Contract((("经销商",),), unique_key=("经销商",)),
    Contract((("医院",), ("医院等级",), ("订单笔数",), ("含税销售总额", "销售总额")), 10, ("医院",), ("含税销售总额", "销售总额")),
    Contract((("医院等级",), ("含税销售总额", "销售总额"), ("销售总数量", "销售量"), ("订单笔数",)), unique_key=("医院等级",)),
)


ENTITY_REPLACEMENTS = (
    ("紫杉醇释放冠脉球囊导管", "空心纤维血液透析器"),
    ("外周插管中心静脉导管", "空心纤维血液透析器"),
    ("超声血管导引穿刺套件", "医用外科口罩"),
    ("医用外科口罩", "空心纤维血液透析器"),
    ("江苏苏云", "振德医疗"),
    ("BD", "振德医疗"),
    ("费森尤斯", "振德医疗"),
    ("上海洁安", "费森尤斯"),
    ("上海市", "北京市"),
    ("上海地区", "北京地区"),
    ("上海", "北京"),
    ("前 10", "前 5"),
    ("前 5", "前 3"),
    ("每家", "各家"),
)


def load_questions(path: Path) -> list[str]:
    questions = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(questions) != len(CONTRACTS):
        raise RuntimeError(f"题库必须包含{len(CONTRACTS)}个非空问题，当前为{len(questions)}个")
    return questions


def paraphrase(question: str) -> str:
    replacements = (
        ("查询", "请列出"),
        ("统计", "请计算"),
        ("按月", "以月份为粒度"),
        ("分析", "请分析"),
        ("并显示", "，同时返回"),
        ("汇总", "分组统计"),
    )
    result = question
    for source, target in replacements:
        if source in result:
            result = result.replace(source, target, 1)
            break
    return result


def entity_variant(question: str) -> str:
    for source, target in ENTITY_REPLACEMENTS:
        if source in question:
            return question.replace(source, target, 1)
    if "最近一年" in question:
        return question.replace("最近一年", "2025年", 1)
    return question.rstrip("。") + "，时间范围改为2025年。"


def followup(question: str) -> str:
    compact = question.replace(" ", "")
    if "前10" in compact or "前5" in compact:
        return "改成前3名，其他条件不变。"
    if "按月" in compact or "销售趋势" in compact:
        return "改成按季度，其他条件不变。"
    if any(word in compact for word in ("名单", "清单", "适用的科室")):
        return "只返回前5个，其他筛选条件不变。"
    if "订单笔数" in compact:
        return "只保留订单笔数，其他条件不变。"
    return "再加上订单笔数，其他条件不变。"


def followup_contract(base: Contract, question: str) -> Contract:
    if "只保留订单笔数" in question:
        return Contract((("订单笔数",),), forbidden_concepts=("销售总额", "销售总数量"))
    if "改成前3名" in question:
        return Contract(
            base.concepts,
            top_n=3,
            unique_key=base.unique_key,
            descending_metric=base.descending_metric,
            excluded_value=base.excluded_value,
        )
    if "再加上订单笔数" in question:
        return Contract(
            (*base.concepts, ("订单笔数",)),
            top_n=base.top_n,
            unique_key=base.unique_key,
            descending_metric=base.descending_metric,
            excluded_value=base.excluded_value,
        )
    if "按季度" in question:
        return Contract(
            (*base.concepts, ("季度", "Q1", "Q2", "Q3", "Q4")),
            top_n=base.top_n,
            unique_key=base.unique_key,
            descending_metric=base.descending_metric,
            excluded_value=base.excluded_value,
            forbidden_concepts=base.forbidden_concepts,
        )
    return base


def markdown_table(answer: str) -> tuple[list[str], list[list[str]]]:
    lines = [line.strip() for line in answer.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    parsed = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    header = parsed[0]
    rows = [row for row in parsed[2:] if len(row) == len(header)]
    return header, rows


def _column_index(header: list[str], aliases: tuple[str, ...]) -> int | None:
    for index, value in enumerate(header):
        if any(alias in value for alias in aliases):
            return index
    return None


def _number(value: str) -> float | None:
    match = re.search(r"-?[\d,]+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def validate(payload: Any, contract: Contract, *, followup_turn: bool = False) -> list[str]:
    if not isinstance(payload, dict):
        return ["响应不是JSON对象"]
    issues: list[str] = []
    status = str(payload.get("status") or "")
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        issues.append("回答为空")
        return issues
    no_rows = any(marker in answer for marker in (
        "未查询到", "没有查询到", "没有找到", "没有匹配到", "无匹配数据",
        "无数据", "共 0 行", "共0行", "返回 0 条", "返回0条",
    ))
    verified_empty = no_rows and status in {"COMPLETED", "SAFE_FALLBACK"}
    evidence = payload.get("evidence")
    honest_partial = (
        status == "PARTIAL_SUCCESS"
        and isinstance(evidence, list)
        and bool(evidence)
        and any(marker in answer for marker in (
            "数据不足", "不足以", "少于趋势分析", "无法形成趋势",
            "不据此生成趋势", "不生成趋势",
        ))
    )
    if status != "COMPLETED" and not verified_empty and not honest_partial:
        issues.append(f"状态不是COMPLETED:{status}")
    if not verified_empty and not honest_partial:
        for marker in FAILURE_MARKERS:
            if marker in answer:
                issues.append(f"回答包含失败标记:{marker}")
    header, rows = markdown_table(answer)
    if not no_rows:
        for aliases in contract.concepts:
            temporal_visible = (
                any(alias in {"月", "交易日期", "销售趋势"} for alias in aliases)
                and bool(re.search(r"(?:19|20)\d{2}[-年/](?:0?[1-9]|1[0-2])", answer))
            )
            if not temporal_visible and not any(alias in answer for alias in aliases):
                issues.append("缺少业务字段:" + "/".join(aliases))
        for concept in contract.forbidden_concepts:
            if concept in answer:
                issues.append("仍返回已移除字段:" + concept)
    if contract.top_n is not None and rows and len(rows) > contract.top_n:
        issues.append(f"返回行数{len(rows)}超过TOP{contract.top_n}")
    if followup_turn and rows and len(rows) > 5:
        issues.append("前5追问返回超过5行")
    if contract.unique_key and rows:
        index = _column_index(header, contract.unique_key)
        if index is not None:
            values = [row[index].strip() for row in rows]
            if len(values) != len(set(values)):
                issues.append("结果主维度存在重复")
    if contract.descending_metric and len(rows) > 1:
        index = _column_index(header, contract.descending_metric)
        if index is not None:
            values = [_number(row[index]) for row in rows]
            numeric = [value for value in values if value is not None]
            if len(numeric) > 1 and numeric != sorted(numeric, reverse=True):
                issues.append("排序指标未按降序返回")
    if contract.excluded_value and contract.excluded_value in "\n".join("|".join(row) for row in rows):
        issues.append(f"排除值仍出现在结果中:{contract.excluded_value}")
    if status == "COMPLETED" and not payload.get("evidence"):
        issues.append("完成结果缺少证据")
    return issues


async def ask(client: httpx.AsyncClient, base_url: str, conversation_id: str, question: str) -> tuple[Any, int | None, str | None, float]:
    started = time.perf_counter()
    try:
        response = await client.post(
            f"{base_url.rstrip('/')}/agent_chat",
            json={
                "application_id": "27",
                "conversation_id": conversation_id,
                "message_id": f"matrix-{uuid4().hex[:12]}",
                "question": question,
                "semantic_model_id": 81,
                "business_domain_ids": [],
                "knowledge_base_names": [],
                "history": [],
                "use_longterm_memory": True,
            },
        )
        return response.json(), response.status_code, None, round(time.perf_counter() - started, 3)
    except Exception as exc:  # pragma: no cover - live harness
        return None, None, f"{type(exc).__name__}: {exc}", round(time.perf_counter() - started, 3)


async def run_case(client: httpx.AsyncClient, base_url: str, number: int, question: str, contract: Contract) -> list[dict[str, Any]]:
    base_conversation = f"business-matrix-{number:02d}-{uuid4().hex[:8]}"
    cases = (
        ("original", question, base_conversation, False),
        ("followup", followup(question), base_conversation, True),
        ("paraphrase", paraphrase(question), f"{base_conversation}-p", False),
        ("entity_variant", entity_variant(question), f"{base_conversation}-e", False),
    )
    results: list[dict[str, Any]] = []
    for variant, text, conversation_id, is_followup in cases:
        payload, http_status, error, elapsed = await ask(client, base_url, conversation_id, text)
        active_contract = followup_contract(contract, text) if is_followup else contract
        issues = [error] if error else validate(
            payload,
            active_contract,
            followup_turn=is_followup and "前5" in text,
        )
        item = {
            "number": number,
            "variant": variant,
            "question": text,
            "http_status": http_status,
            "status": payload.get("status") if isinstance(payload, dict) else None,
            "intent": payload.get("intent") if isinstance(payload, dict) else None,
            "answer": str(payload.get("answer") or "") if isinstance(payload, dict) else "",
            "elapsed_seconds": elapsed,
            "issues": issues,
        }
        print(json.dumps({key: item[key] for key in ("number", "variant", "status", "elapsed_seconds", "issues")}, ensure_ascii=False), flush=True)
        results.append(item)
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTION_FILE)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    questions = load_questions(args.questions)
    selected = {int(value) for value in args.only.split(",") if value.strip()} if args.only else set()
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    timeout = httpx.Timeout(300.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def guarded(number: int, question: str, contract: Contract) -> list[dict[str, Any]]:
            async with semaphore:
                return await run_case(client, args.base_url, number, question, contract)

        groups = await asyncio.gather(*(
            guarded(number, question, CONTRACTS[number - 1])
            for number, question in enumerate(questions, 1)
            if not selected or number in selected
        ))
    results = [item for group in groups for item in group]
    elapsed = [item["elapsed_seconds"] for item in results]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "case_count": len(results),
        "passed": sum(not item["issues"] for item in results),
        "failed": sum(bool(item["issues"]) for item in results),
        "completed": sum(item["status"] == "COMPLETED" for item in results),
        "latency_mean": round(statistics.mean(elapsed), 3) if elapsed else 0,
        "latency_max": max(elapsed, default=0),
    }
    artifact = {"summary": summary, "results": results}
    output = args.output or ROOT / "evals" / "results" / f"business-question-matrix-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output": str(output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
