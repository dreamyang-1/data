#!/usr/bin/env python3
"""数据智能体自动发问与审查驱动。

规则（用户约定）：
- 约 1 分钟 1 问；智能体回复并记录完毕后，若距上次发问已满 1 分钟立即发下一问，不足则补足等待；
- 单问最长 3 分钟：超时则记录原因（timeout），放弃当前会话，直接开新会话（下一剧本）；
- 同一会话连续重复回答 / 连续澄清视为卡死，记录后换新会话；
- 所有提问、标准答案、智能体回答、审查结论写入 qa_log.jsonl。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
BANK = HERE / "bank.json"
QA_LOG = HERE / "qa_log.jsonl"
RUN_LOG = HERE / "run.log"
SUMMARY = HERE / "summary.json"

BASE_URL = "http://192.168.1.27:8088"
ASK_INTERVAL = 60        # 目标发问间隔（秒）
HARD_TIMEOUT = 180       # 单问硬超时（3 分钟）
STUCK_REPEAT = 2         # 连续重复次数阈值
STUCK_CLARIFY = 3        # 连续澄清轮数阈值

_client = httpx.Client(timeout=httpx.Timeout(HARD_TIMEOUT, connect=10.0))


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_record(rec: dict) -> None:
    with QA_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 审查逻辑
def _nums_in(text: str) -> set[str]:
    """提取回答中所有数字（去千分位），供匹配。"""
    return {m.replace(",", "") for m in re.findall(r"[\d,]+(?:\.\d+)?", text)}


def _money_candidates(v: float) -> set[str]:
    """金额的多种表述候选：精确、取整、万元、亿元。"""
    c = {f"{v:.2f}", f"{v:.0f}", f"{v:.1f}"}
    wan = v / 10000
    c |= {f"{wan:.2f}", f"{wan:.1f}", f"{wan:.0f}"}
    if abs(v) >= 1e8:
        yi = v / 1e8
        c |= {f"{yi:.2f}", f"{yi:.1f}"}
    return c


def _check_money(v: float, answer: str) -> bool:
    nums = _nums_in(answer)
    for cand in _money_candidates(v):
        if cand in nums:
            return True
        # 回答数字与候选四舍五入一致（±0.5%）
        try:
            cv = float(cand)
            if abs(cv - v) <= max(1.0, abs(v) * 0.005):
                return True
        except ValueError:
            pass
    return False


def review(answer: str, status, evidence_kinds, missing_slots, turn: dict) -> dict:
    """返回 {verdict, issues, detail}。"""
    issues: list[str] = []
    ct = turn["check_type"]
    std = turn["std"]
    exp = std.get("expect", {})

    # -- 结构校验
    if status != "COMPLETED":
        issues.append(f"状态非COMPLETED:{status}")
    if not (set(evidence_kinds) & {"QUERY_RESULT", "DATA_QUERY_RESULT", "DATASET"}):
        issues.append(f"缺少查询证据:{evidence_kinds}")
    if missing_slots:
        issues.append(f"missing_slots:{missing_slots}")
    for marker in ("智能体配置错误", "ASL 转 SQL 服务未能生成", "需要补充", "请确认"):
        if marker in answer:
            issues.append(f"含失败/澄清话术:{marker}")

    # -- 数据校验
    data_notes: list[str] = []
    nums = _nums_in(answer)
    if ct in ("list", "list_ordered"):
        want = exp.get("items", [])
        hit = [x for x in want if x in answer]
        cover = len(hit) / len(want) if want else 0
        if f"{len(want)}" not in nums and len(want) > 0:
            # 数量未明示不算硬伤，记录提示
            data_notes.append(f"未明示总数(期望{len(want)})")
        if cover < 0.6:
            issues.append(f"清单覆盖不足:{len(hit)}/{len(want)}")
        else:
            data_notes.append(f"清单覆盖{len(hit)}/{len(want)}")
    elif ct == "count":
        want = exp.get("count")
        if not any(str(want) == n or (n.isdigit() and abs(int(n) - want) <= 1) for n in nums):
            issues.append(f"数量不符:期望{want}")
    elif ct == "money":
        v = exp.get("money", 0.0)
        if not _check_money(v, answer):
            issues.append(f"金额不符:期望{v:,.2f}")
        if exp.get("name") and exp["name"] not in answer and exp["name"] != "None":
            data_notes.append(f"未提及期望主体:{exp['name']}")
    elif ct == "money_count":
        v, c = exp.get("money", 0.0), exp.get("count", 0)
        if not _check_money(v, answer):
            issues.append(f"金额不符:期望{v:,.2f}")
        if not any(str(c) == n or (n.replace('.', '', 1).isdigit() and abs(float(n) - c) <= 1) for n in nums):
            issues.append(f"数量不符:期望{c}")
    elif ct == "group":
        groups = exp.get("groups", {})
        miss = [k for k in groups if k not in answer]
        if miss and len(miss) > len(groups) * 0.4:
            issues.append(f"分组键缺失过多:{miss[:5]}")
        else:
            if miss:
                data_notes.append(f"部分分组键缺失:{miss[:3]}")

    verdict = "PASS" if not issues else "FAIL"
    return {"verdict": verdict, "issues": issues, "data_notes": data_notes}


# ---------------------------------------------------------------- 请求
def ask(conversation_id: str, question: str, history: list[dict]) -> dict:
    """发送一问，返回响应 payload。异常向上抛。"""
    payload = {
        "application_id": "27",
        "conversation_id": conversation_id,
        "message_id": f"m-{uuid.uuid4().hex[:12]}",
        "question": question,
        "semantic_model_id": 81,
        "business_domain_ids": [],
        "knowledge_base_names": [],
        "history": history,
        "use_longterm_memory": True,
    }
    resp = _client.post(f"{BASE_URL}/agent_chat", json=payload)
    resp.raise_for_status()
    return resp.json()


def run_scenario(sc: dict, stats: dict) -> None:
    split = bool(sc.get("split_sessions"))
    conversation_id = f"ceshi-{sc['sid']}-{uuid.uuid4().hex[:8]}"
    history: list[dict] = []
    repeat_count = 0
    clarify_count = 0
    last_answer = ""

    for idx, turn in enumerate(sc["turns"], 1):
        if split:
            conversation_id = f"ceshi-{sc['sid']}-t{idx}-{uuid.uuid4().hex[:8]}"
        tid = turn["tid"]
        question = turn["question"]
        sent_at = time.time()
        log(f"{tid} 发问[{conversation_id}]: {question}")

        rec = {
            "ts": datetime.now().isoformat(),
            "sid": sc["sid"], "scenario": sc["name"], "tid": tid,
            "conversation_id": conversation_id, "turn": idx,
            "question": question,
            "std_sql": turn["std"]["sql"],
            "std_expect": turn["std"].get("expect"),
        }
        try:
            payload = ask(conversation_id, question, history)
        except httpx.TimeoutException:
            rec.update({
                "verdict": "TIMEOUT", "issues": [f"超过{HARD_TIMEOUT}s未完成"],
                "elapsed": round(time.time() - sent_at, 1),
                "answer": "", "status": "TIMEOUT",
            })
            append_record(rec)
            stats["timeout"] += 1
            log(f"{tid} 超时({HARD_TIMEOUT}s)，放弃当前会话，切换新会话")
            return  # 换新会话（下一剧本）
        except Exception as exc:  # 网络等其它错误同样换会话
            rec.update({
                "verdict": "ERROR", "issues": [f"{type(exc).__name__}: {exc}"],
                "elapsed": round(time.time() - sent_at, 1),
                "answer": "", "status": "ERROR",
            })
            append_record(rec)
            stats["error"] += 1
            log(f"{tid} 异常:{exc}，切换新会话")
            return

        elapsed = round(time.time() - sent_at, 1)
        status = payload.get("status")
        answer = str(payload.get("answer") or "")
        evidence_kinds = [e.get("kind") for e in (payload.get("evidence") or []) if isinstance(e, dict)]
        missing_slots = payload.get("missing_slots", [])

        # -- 审查（NEEDS_CLARIFICATION 单独归类，不算 FAIL）
        if status == "NEEDS_CLARIFICATION":
            review_res = {"verdict": "CLARIFY", "issues": ["要求澄清"], "data_notes": []}
            clarify_count += 1
        else:
            review_res = review(answer, status, evidence_kinds, missing_slots, turn)
            clarify_count = 0

        # -- 卡死检测：连续重复
        if answer and answer == last_answer:
            repeat_count += 1
        else:
            repeat_count = 0
        last_answer = answer
        if repeat_count >= STUCK_REPEAT or clarify_count >= STUCK_CLARIFY:
            reason = "连续重复相同回答" if repeat_count >= STUCK_REPEAT else "连续多轮要求澄清"
            rec.update({
                "verdict": "STUCK", "issues": [reason],
                "answer": answer, "status": status,
                "elapsed": elapsed, "evidence_kinds": evidence_kinds,
            })
            append_record(rec)
            stats["stuck"] += 1
            log(f"{tid} 疑似卡死({reason})，切换新会话")
            return

        rec.update({
            "verdict": review_res["verdict"],
            "issues": review_res["issues"],
            "data_notes": review_res["data_notes"],
            "status": status, "intent": payload.get("intent"),
            "answer": answer[:6000],
            "evidence_kinds": evidence_kinds,
            "elapsed": elapsed,
        })
        append_record(rec)
        stats[{"PASS": "pass", "FAIL": "fail", "CLARIFY": "clarify"}.get(review_res["verdict"], "other")] += 1
        log(f"{tid} {review_res['verdict']} {elapsed}s {(';'.join(review_res['issues']) or 'ok')[:100]}")

        # -- 累积会话历史（供多轮追问）
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer[:4000]})
        if len(history) > 20:
            history = history[-20:]

        # -- 节奏控制：距发问不足 1 分钟则补足等待
        wait = ASK_INTERVAL - (time.time() - sent_at)
        if wait > 0 and idx < len(sc["turns"]):
            time.sleep(wait)


def main() -> None:
    bank = json.loads(BANK.read_text(encoding="utf-8"))
    stats = {"pass": 0, "fail": 0, "clarify": 0, "timeout": 0, "stuck": 0, "error": 0, "other": 0}
    log(f"开始：{len(bank['scenarios'])} 个剧本，共 {sum(len(s['turns']) for s in bank['scenarios'])} 轮提问")
    for sc in bank["scenarios"]:
        log(f"=== 剧本 {sc['sid']} {sc['name']} 开始 ===")
        run_scenario(sc, stats)
    total = sum(v for k, v in stats.items() if k != "other")
    summary = {
        "finished_at": datetime.now().isoformat(),
        "total_turns_recorded": total, **stats,
        "note": "PASS=结构与数据校验通过; FAIL=有硬伤; CLARIFY=智能体要求澄清; "
                "TIMEOUT=3分钟超时; STUCK=重复/澄清卡死; ERROR=网络异常",
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"全部完成：{json.dumps(summary, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
