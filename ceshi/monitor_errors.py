#!/usr/bin/env python3
"""监控 qa_log JSONL，将非 PASS 记录持续追加到 错误问题文档.md。

规则：
- 每积累 5 条非 PASS 记录（FAIL/CLARIFY/STUCK/TIMEOUT/ERROR）追加一批到文档；
- 断点续传：进度存 state JSON，可随时重启；
- probe 结束后（日志 10 分钟无增长）自动冲刷剩余缓冲并退出。
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC = HERE / "错误问题文档.md"

POLL_SEC = 15          # 轮询间隔
IDLE_EXIT_SEC = 600    # probe 结束且无新行后冲刷退出（10分钟无增长才认为结束）


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"processed_lines": 0, "err_seq": 0, "buffer": [], "batch_no": 0,
            "started_at": datetime.now().isoformat()}


def save_state(st: dict, state_path: Path) -> None:
    tmp = dict(st)
    state_path.write_text(json.dumps(tmp, ensure_ascii=False), encoding="utf-8")


def classify(rec: dict) -> tuple[str, str, str]:
    """返回 (severity, signature, analysis)。signature 用于同因去重。"""
    v = rec.get("verdict")
    issues = ";".join(rec.get("issues", []))
    q = rec.get("question", "")

    if v == "TIMEOUT":
        return "严重", "TIMEOUT", "超过3分钟未响应，会话被放弃"
    if v == "STUCK":
        return "严重", "STUCK", "连续重复回答/连续澄清，判定卡死并切换新会话"
    if v == "ERROR":
        return "严重", "ERROR", "网络/服务异常"

    if v == "CLARIFY":
        if "订单分布" in q or ("省份" in q and "分布" in q):
            return "中", "CLARIFY-省份分布", (
                "用户问\"各省份的销售订单分布\"，指标就是订单数，智能体却反问"
                "\"要查询或分析哪个指标？\"——不识别\"订单分布\"为指标，属过度澄清")
        return "中", "CLARIFY-其他", "智能体要求澄清（疑似过度澄清），未直接作答"

    # FAIL 细分
    if "状态非COMPLETED" in issues or "SAFE_FALLBACK" in rec.get("status", ""):
        code = rec.get("status", "")
        return "严重", f"FALLBACK-{code[:40]}", f"语义层服务降级（{code}），未返回任何数据"
    if "金额不符" in issues or "分组数值错误" in issues:
        return "严重", f"FAIL-数值-{rec['tid']}", "返回金额/分组数值与数据库标准答案不符"
    if "分组键缺失" in issues:
        return "严重", "FAIL-分组键缺失", "分组结果缺失大量分组键（省份/月份等）"
    if "清单覆盖不足" in issues:
        return "严重", f"FAIL-清单-{rec['tid']}", "经销商/产品清单与标准答案覆盖度过低"
    if "数量不符" in issues:
        return "中", "FAIL-数量不符", (
            "数量与标准答案不符：多因品名模糊匹配口径（多品命中）或"
            "智能体默认近1年时间窗口与全量口径差异所致，需口径透明化")
    return "中", f"FAIL-其他-{rec['tid']}", issues or "结构/数据校验未通过"


def fmt_expect(exp) -> str:
    if not exp:
        return "（无）"
    if "groups" in exp:
        gs = list(exp["groups"].items())[:6]
        more = len(exp["groups"]) - len(gs)
        s = "; ".join(f"{k}={v:,.0f}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in gs)
        return f"分组：{s}" + (f" 等{len(exp['groups'])}组" if more > 0 else "")
    if "items" in exp:
        items = [str(i) for i in exp["items"][:5]]
        return f"共{exp.get('count', len(exp['items']))}项：{'、'.join(items)}" + (
            " 等" if len(exp["items"]) > 5 else "")
    parts = []
    if "money" in exp:
        parts.append(f"金额={exp['money']:,.2f}")
    if "count" in exp:
        parts.append(f"数量={exp['count']:,.0f}")
    return "; ".join(parts) or json.dumps(exp, ensure_ascii=False)


def fmt_entry(err_no: int, sev: str, rec: dict, analysis: str) -> str:
    ans = (rec.get("answer") or "").strip()
    ans = ans.replace("\n", " ")[:260] or "（空）"
    issues = "；".join(rec.get("issues", [])) or "—"
    turn_info = f"第{rec.get('turn')}轮" if rec.get("turn") else "单轮"
    return (
        f"### ERR-{err_no:03d}【{sev}】{rec['question'][:38]}（{rec['tid']}，{rec.get('verdict')}，{turn_info}）\n"
        f"- **问题**：{rec['question']}\n"
        f"- **智能体回答**：{ans}\n"
        f"- **标准答案**：{fmt_expect(rec.get('std_expect'))}\n"
        f"- **问题点**：{issues}\n"
        f"- **错误分析**：{analysis}\n"
    )


def flush(st: dict, err_start: int, batch_label: str, state_path: Path) -> None:
    if not st["buffer"]:
        return
    st["batch_no"] += 1
    lines = [
        "\n---\n",
        f"## {batch_label}·批次 {st['batch_no']}"
        f"（{datetime.now():%m-%d %H:%M}，本批 {len(st['buffer'])} 条错误）\n",
    ]
    for rec in st["buffer"]:
        sev, sig, analysis = classify(rec)
        st["err_seq"] += 1
        lines.append(fmt_entry(err_start - 1 + st["err_seq"], sev, rec, analysis))

    with DOC.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    st["buffer"] = []
    save_state(st, state_path)
    print(f"[{datetime.now():%H:%M:%S}] 批次{st['batch_no']} 已写入文档"
          f"（{len(lines)-2} 条，累计 ERR-{err_start - 1 + st['err_seq']:03d}）", flush=True)


def probe_alive(qa_log: Path) -> bool:
    """通过检查 qa_log 行数是否在增长来判断 probe 是否还在跑。"""
    try:
        if not qa_log.exists():
            return True
        n = sum(1 for _ in qa_log.open(encoding="utf-8"))
        time.sleep(5)
        n2 = sum(1 for _ in qa_log.open(encoding="utf-8"))
        return n2 > n
    except Exception:
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="监控 qa_log 并追加错误到文档")
    parser.add_argument("--qa-log", default=str(HERE / "qa_log.jsonl"))
    parser.add_argument("--state", default=str(HERE / "monitor_state.json"))
    parser.add_argument("--err-start", type=int, default=32)
    parser.add_argument("--batch-label", default="第二批测试")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--poll-sec", type=int, default=15)
    args = parser.parse_args()
    qa_log = Path(args.qa_log)
    state_path = Path(args.state)

    st = load_state(state_path)
    print(f"监控启动：已处理 {st['processed_lines']} 行，缓冲 {len(st['buffer'])} 条，"
          f"批次 {st['batch_no']}", flush=True)
    last_growth = time.time()
    while True:
        n0 = st["processed_lines"]
        if qa_log.exists():
            for line in qa_log.read_text(encoding="utf-8").splitlines()[st["processed_lines"]:]:
                st["processed_lines"] += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("verdict") == "PASS":
                    continue
                st["buffer"].append(rec)
            if st["processed_lines"] > n0:
                last_growth = time.time()
                save_state(st, state_path)
        if len(st["buffer"]) >= args.batch_size:
            flush(st, args.err_start, args.batch_label, state_path)
        if not probe_alive(qa_log) and time.time() - last_growth > IDLE_EXIT_SEC:
            flush(st, args.err_start, args.batch_label, state_path)
            print(f"probe 已结束，监控收尾退出。共 {st['batch_no']} 批，错误条目 {st['err_seq']} 个。", flush=True)
            break
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
