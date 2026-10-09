#!/usr/bin/env python3
"""离线重审：用增强规则重新审查 qa_log.jsonl 全部记录，输出修正版报告。

增强点（相对 probe.py 内置审查）：
- list 类：校验回答声明的总数（"共查询到 N 条"）与表格行数，是否等于期望 count；
  不等则判 FAIL（典型问题：追问"只看上海的"未生效，仍返回全量）。
- group 类：逐组校验 key 及其邻近数值。
生成 corrected_report.json / corrected_report.md。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
QA_LOG = HERE / "qa_log.jsonl"
BANK = HERE / "bank.json"


def load_check_types() -> dict[str, str]:
    """从 bank.json 取 sid+tid -> check_type 映射（qa_log 未记录该字段）。"""
    mapping: dict[str, str] = {}
    try:
        bank = json.loads(BANK.read_text(encoding="utf-8"))
    except Exception:
        return mapping
    scenarios = bank.get("scenarios") if isinstance(bank, dict) else bank
    for s in scenarios or []:
        for t in s.get("turns", []):
            mapping[t["tid"]] = t.get("check_type", "")
    return mapping


def declared_total(answer: str) -> int | None:
    m = re.search(r"共(?:查询|统计|找到|返回)到?\s*([\d,]+)\s*(?:条|家|个|笔)", answer)
    return int(m.group(1).replace(",", "")) if m else None


def table_rows(answer: str) -> int:
    rows = [ln for ln in answer.splitlines() if ln.strip().startswith("|")]
    # 去掉表头与分隔行
    data = [r for r in rows if not re.match(r"^\|[\s:|-]+\|$", r.strip())]
    return max(0, len(data) - 1) if data else 0


def nums_in(text: str) -> set[str]:
    return {m.replace(",", "") for m in re.findall(r"[\d,]+(?:\.\d+)?", text)}


def money_ok(v: float, answer: str) -> bool:
    """回答文本中的数字是否与期望金额 v 匹配（允许原值/四舍五入/万元口径，±0.5%）。"""
    tol = max(1.0, abs(v) * 0.005)
    for tok in nums_in(answer):
        try:
            n = float(tok)
        except ValueError:
            continue
        if abs(n - v) <= tol or abs(n - v / 10000) <= max(1.0, abs(v) * 0.005 / 10000):
            return True
    return False


def group_value_ok(key: str, want: float, answer: str) -> bool:
    """分组键所在表格行内的数字是否与期望值匹配。"""
    tol = max(1.0, abs(want) * 0.005)
    for ln in answer.splitlines():
        if key in ln and "|" in ln:
            for tok in nums_in(ln):
                try:
                    n = float(tok)
                except ValueError:
                    continue
                if abs(n - want) <= tol or abs(n - want / 10000) <= max(1.0, abs(want) * 0.005 / 10000):
                    return True
    return False


def recheck(rec: dict, check_types: dict[str, str] | None = None) -> dict:
    """返回修正后的 {verdict, issues, data_notes}。原 verdict 为 TIMEOUT/STUCK/ERROR/CLARIFY 的保留。"""
    if rec.get("verdict") in ("TIMEOUT", "STUCK", "ERROR", "CLARIFY"):
        return {"verdict": rec["verdict"], "issues": rec.get("issues", []), "data_notes": []}
    answer = rec.get("answer", "")
    std = rec.get("std_expect") or {}
    ct = (check_types or {}).get(rec["tid"]) or rec.get("check_type", "")
    issues, notes = [], []

    want_items = std.get("items") or []
    want_count = std.get("count")
    if ct in ("list", "list_ordered"):
        n_declared = declared_total(answer)
        n_table = table_rows(answer)
        hit = [x for x in want_items if x in answer]
        # 总数核对：声明总数或表格行数
        effective = n_declared if n_declared is not None else n_table
        if effective is not None and want_count is not None and effective != want_count:
            issues.append(f"回答条数{effective}≠期望{want_count}（追问过滤/口径可能未生效）")
        if want_items and len(hit) < len(want_items) * 0.6:
            issues.append(f"清单覆盖不足{len(hit)}/{len(want_items)}")
        elif want_items:
            notes.append(f"清单覆盖{len(hit)}/{len(want_items)}")
        # 追加多余条目检测：回答表格中出现非期望名单里的"公司"行
        extra = [ln for ln in answer.splitlines()
                 if ln.strip().startswith("|") and "有限公司" in ln
                 and not any(w in ln for w in want_items)]
        if extra and want_count and n_declared and n_declared > want_count:
            notes.append(f"疑似混入未过滤条目{len(extra)}行")
    elif ct == "count" and want_count is not None:
        ns = nums_in(answer)
        if not any(str(want_count) == n or (n.isdigit() and abs(int(n) - want_count) <= 1) for n in ns):
            issues.append(f"数量不符:期望{want_count}")
    elif ct == "money":
        v = std.get("money", 0.0)
        if not money_ok(v, answer):
            issues.append(f"金额不符:期望{v:,.2f}")
    elif ct == "money_count":
        v, c = std.get("money", 0.0), std.get("count", 0)
        if not money_ok(v, answer):
            issues.append(f"金额不符:期望{v:,.2f}")
        ns = nums_in(answer)
        if not any(str(c) == n or (n.replace('.', '', 1).isdigit() and abs(float(n) - c) <= 1) for n in ns):
            issues.append(f"数量不符:期望{c}")
    elif ct == "group":
        groups = std.get("groups", {})
        miss = [k for k in groups if k not in answer]
        if miss and len(miss) > len(groups) * 0.4:
            issues.append(f"分组键缺失过多:{miss[:5]}")
        elif miss:
            notes.append(f"部分分组键缺失:{miss[:3]}")
        else:
            notes.append(f"分组键齐全({len(groups)}组)")
        # 数值校验：键所在行的数字应与期望值一致（±0.5%）
        bad = [k for k, v in groups.items() if k in answer and not group_value_ok(k, float(v), answer)]
        if bad:
            issues.append(f"分组数值错误:{bad[:5]}")

    # 结构问题沿用
    for old in rec.get("issues", []):
        if old not in issues and any(k in old for k in ("状态", "证据", "missing", "话术")):
            issues.append(old)
    return {"verdict": "PASS" if not issues else "FAIL", "issues": issues, "data_notes": notes}


def main() -> None:
    if not QA_LOG.exists():
        print("qa_log.jsonl 不存在")
        return
    records = [json.loads(ln) for ln in QA_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check_types = load_check_types()
    out = []
    for rec in records:
        fixed = recheck(rec, check_types)
        merged = dict(rec)
        merged["verdict_orig"] = rec.get("verdict")
        merged.update(fixed)
        out.append(merged)

    cnt = Counter(r["verdict"] for r in out)
    report = {
        "rechecked_at": datetime.now().isoformat(),
        "total": len(out), "verdicts": dict(cnt),
        "records": out,
    }
    (HERE / "corrected_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# 重审报告 {datetime.now():%Y-%m-%d %H:%M}", f"总计 {len(out)} 轮：{dict(cnt)}", ""]
    for r in out:
        flag = {"PASS": "✅", "FAIL": "❌", "CLARIFY": "⚠️", "TIMEOUT": "⏱", "STUCK": "🔁", "ERROR": "💥"}.get(r["verdict"], "?")
        lines.append(f"## {flag} {r['tid']} {r['verdict']}（原:{r.get('verdict_orig')}）")
        lines.append(f"- 问：{r['question']}")
        lines.append(f"- 答：{str(r.get('answer', ''))[:120]}...")
        if r.get("issues"):
            lines.append(f"- 问题：{'；'.join(r['issues'])}")
        if r.get("data_notes"):
            lines.append(f"- 备注：{'；'.join(r['data_notes'])}")
        lines.append(f"- 耗时：{r.get('elapsed')}s")
        lines.append("")
    (HERE / "corrected_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"重审完成：{len(out)} 轮 -> corrected_report.json / corrected_report.md")
    print(json.dumps(dict(cnt), ensure_ascii=False))


if __name__ == "__main__":
    main()
