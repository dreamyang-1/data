# Laya PoC 评测：意图分类与追问判定两个独立任务，Laya 与大模型基线同题对比
# 用法：
#   python laya_poc_eval2.py laya    -> 跑 Laya
#   python laya_poc_eval2.py llm     -> 跑大模型基线（qwen3.7-max）
# 输出：poc_<name>_result.json，含 meta（运行条件）、records（逐条）、summary（分任务指标）
import asyncio
import json
import re
import sys
import time

BASE_DIR = "/root/yyy/DataAnalysis_Agent/laya_poc"
DATA = json.load(open(f"{BASE_DIR}/laya_poc_dataset.json"))
OUT = {"laya": f"{BASE_DIR}/poc_laya_result.json", "llm": f"{BASE_DIR}/poc_llm_result.json"}

INTENT_LABELS = [
    "指标查询", "明细查询", "趋势分析", "对比分析", "占比分析", "异常分析",
    "归因分析", "预测分析", "报表生成", "解释口径", "闲聊", "非数据任务",
]
INTENT_CRITERIA = {
    "指标查询": "用户想知道某个指标的数值、总额、汇总结果，答案是一个或一列数字",
    "明细查询": "用户想拿一份名单或清单，返回对象名称、属性等明细行，不是算数",
    "趋势分析": "用户想看指标随时间（按月、按季度）的走势变化",
    "对比分析": "用户想把两个或多个时间段、对象、产品的数据放在一起比较高低",
    "占比分析": "用户想知道部分占总体的百分比、份额构成",
    "异常分析": "用户想找出数据里不正常、波动大的对象或点",
    "归因分析": "用户想知道某个结果为什么发生、下降或增长的原因是什么",
    "预测分析": "用户想预估未来时间段（下季度、明年）的数值",
    "报表生成": "用户想把已有的业务数据整理导出成报表文件",
    "解释口径": "用户在问某个指标或名词的定义、统计口径，不是要数据",
    "闲聊": "日常问候、寒暄、聊天，与业务数据完全无关",
    "非数据任务": "请求做数据查询分析之外的事情（写文章、设提醒、翻译、订会议室等）",
}
INTENT_INSTRUCTIONS = (
    "判断这段用户输入属于哪种数据分析意图。区分要点："
    "要名单清单选明细查询，要数字汇总选指标查询，"
    "找原因选归因分析，找不正常的对象选异常分析，"
    "问定义选解释口径，问候寒暄选闲聊，数据之外的其他请求选非数据任务"
)
FOLLOWUP_INSTRUCTIONS = (
    "上一轮已经完成了一次数据查询。判断本轮这个追问要的数据能否直接来自"
    "上一轮查询的结果：只是选列、筛行、排序、取前N，或对结果里已有数字做"
    "简单换算，数据本身没有新增，回答 true；需要上一轮没有的新指标、新维度、"
    "新粒度、新时间范围或新关联数据，回答 false"
)
FOLLOWUP_LABELS = ["SNAPSHOT", "NEW_QUERY"]


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return round(int(line.split()[1]) / 1024)
    except OSError:
        pass
    return 0


def percentile(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def latency_block(lat):
    return {"P50_ms": round(percentile(lat, 50), 1),
            "P95_ms": round(percentile(lat, 95), 1),
            "avg_ms": round(sum(lat) / len(lat), 1),
            "max_ms": round(max(lat), 1)}


def task_summary(records, labels):
    correct = [r for r in records if r["ok"]]
    lat = [r["latency_ms"] for r in records]
    # 混淆矩阵：行=预期，列=判定
    idx = {l: i for i, l in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for r in records:
        if r["got"] in idx and r["expected"] in idx:
            matrix[idx[r["expected"]]][idx[r["got"]]] += 1
    per_class = {}
    for l in labels:
        rs = [r for r in records if r["expected"] == l]
        per_class[l] = {
            "total": len(rs),
            "correct": sum(r["ok"] for r in rs),
            "accuracy": round(sum(r["ok"] for r in rs) / len(rs), 4) if rs else None,
        }
    wrong = [{"text": r["text"], "expected": r["expected"], "got": r["got"],
              "confidence": r["confidence"]} for r in records if not r["ok"]]
    return {
        "total": len(records),
        "correct": len(correct),
        "accuracy": round(len(correct) / len(records), 4) if records else 0,
        "latency": latency_block(lat),
        "per_class": per_class,
        "confusion_matrix": {"labels": labels, "rows": matrix},
        "wrong_samples": wrong,
    }


def confidence_block(records):
    confs = [r["confidence"] for r in records]
    hist = [0] * 10
    for c in confs:
        hist[min(9, int(c * 10))] += 1
    ece, n = 0.0, len(records)
    for b in range(10):
        bucket = [r for r in records if b / 10 <= r["confidence"] < (b + 1) / 10]
        if bucket:
            acc_b = sum(r["ok"] for r in bucket) / len(bucket)
            conf_b = sum(r["confidence"] for r in bucket) / len(bucket)
            ece += len(bucket) / n * abs(acc_b - conf_b)
    ok_c = [r["confidence"] for r in records if r["ok"]]
    bad_c = [r["confidence"] for r in records if not r["ok"]]
    # conf>=0.5 直通（低置信回退大模型时）的表现
    direct = [r for r in records if r["confidence"] >= 0.5]
    return {
        "histogram_0.1_bins": hist,
        "ece": round(ece, 4),
        "correct_avg_conf": round(sum(ok_c) / len(ok_c), 3) if ok_c else None,
        "wrong_avg_conf": round(sum(bad_c) / len(bad_c), 3) if bad_c else None,
        "direct_ratio": round(len(direct) / n, 3) if n else 0,
        "direct_accuracy": round(sum(r["ok"] for r in direct) / len(direct), 4) if direct else None,
    }


def report(name, records, meta):
    intent = [r for r in records if r["task"] == "intent"]
    follow = [r for r in records if r["task"] == "followup"]
    summary = {
        "intent": task_summary(intent, INTENT_LABELS),
        "followup": task_summary(follow, FOLLOWUP_LABELS),
        "confidence": confidence_block(records),
    }
    all_lat = [r["latency_ms"] for r in records]
    summary["overall"] = {
        "total": len(records),
        "accuracy": round(sum(r["ok"] for r in records) / len(records), 4) if records else 0,
        "latency": latency_block(all_lat),
        "throughput_qps": round(len(records) / meta["total_wall_seconds"], 2) if meta["total_wall_seconds"] else 0,
    }
    json.dump({"meta": meta, "summary": summary, "records": records},
              open(OUT[name], "w"), ensure_ascii=False, indent=1)

    print(f"\n===== {meta['model']} =====")
    print(f"运行条件: device={meta['device']} concurrency={meta['concurrency']} "
          f"model_load={meta['model_load_seconds']}s cold_start={meta['cold_start_ms']}ms "
          f"warm_avg={meta['warm_avg_ms']}ms total_wall={meta['total_wall_seconds']}s "
          f"throughput={summary['overall']['throughput_qps']} qps")
    print(f"内存 RSS: {meta['rss_mb']} MB")
    for key, title in (("intent", "意图分类"), ("followup", "追问判定")):
        s = summary[key]
        print(f"\n[{title}] {s['correct']}/{s['total']} = {s['accuracy']:.1%} "
              f"P50={s['latency']['P50_ms']}ms P95={s['latency']['P95_ms']}ms")
        for l, pc in s["per_class"].items():
            if pc["accuracy"] is not None and pc["accuracy"] < 1:
                print(f"  {l}: {pc['correct']}/{pc['total']}")
        m = s["confusion_matrix"]
        print("  混淆(预期->判定:条数):")
        for i, l in enumerate(m["labels"]):
            offs = {m["labels"][j]: m["rows"][i][j] for j in range(len(m["labels"]))
                    if j != i and m["rows"][i][j] > 0}
            if offs:
                print(f"    {l} -> {offs}")
        for w in s["wrong_samples"]:
            print(f"    错例[{w['expected']}] {w['text']} -> {w['got']} conf={w['confidence']:.2f}")
    c = summary["confidence"]
    print(f"\n置信度: ECE={c['ece']} 判对均conf={c['correct_avg_conf']} "
          f"判错均conf={c['wrong_avg_conf']} 直通比例={c['direct_ratio']:.0%} 直通准确率={c['direct_accuracy']}")
    print(f"置信度直方图(0.1分桶): {c['histogram_0.1_bins']}")


def build_meta(name, model, device, concurrency, load_s, wall_s, lat):
    return {
        "model": model,
        "device": device,
        "concurrency": concurrency,
        "model_load_seconds": round(load_s, 1),
        "cold_start_ms": round(lat[0], 1),
        "warm_avg_ms": round(sum(lat[1:]) / max(1, len(lat) - 1), 1),
        "total_wall_seconds": round(wall_s, 1),
        "rss_mb": rss_mb(),
        "dataset": f"{BASE_DIR}/laya_poc_dataset.json",
        "note": "cold_start为加载后第一条请求耗时；吞吐量为整轮平均，llm模式并发下不代表单实例上限",
    }


def run_laya():
    from laya import Router
    t0 = time.perf_counter()
    router = Router()  # 按需加载，首次 predict 时载入 multilingual
    load_s = time.perf_counter() - t0

    q_intent = {"intent": {"type": "choice", "instructions": INTENT_INSTRUCTIONS, "criteria": INTENT_CRITERIA}}
    q_follow = {"reuse": {"type": "noul", "instructions": FOLLOWUP_INSTRUCTIONS}}

    records = []
    wall0 = time.perf_counter()
    for item in DATA["intent"]:
        t = time.perf_counter()
        res = router.predict({"body": item["text"]}, q_intent)
        ms = (time.perf_counter() - t) * 1000
        a = res["answers"]["intent"]
        records.append({"task": "intent", "text": item["text"], "expected": item["label"],
                        "got": a["choice"], "confidence": a["confidence"],
                        "ok": a["choice"] == item["label"], "latency_ms": round(ms, 1)})
    for item in DATA["followup"]:
        t = time.perf_counter()
        res = router.predict({"previous_task": item["context"], "current_question": item["text"]}, q_follow)
        ms = (time.perf_counter() - t) * 1000
        a = res["answers"]["reuse"]
        got = "SNAPSHOT" if a["noul"] >= 0.5 else "NEW_QUERY"
        records.append({"task": "followup", "text": f"[{item['context']}] {item['text']}",
                        "expected": item["label"], "got": got,
                        "confidence": a["noul"] if got == "SNAPSHOT" else 1 - a["noul"],
                        "ok": got == item["label"], "latency_ms": round(ms, 1)})
    wall = time.perf_counter() - wall0
    lat = [r["latency_ms"] for r in records]
    # 首条包含了 multilingual checkpoint 的磁盘加载，单独记为冷启动
    report("laya", records, build_meta("laya", "Laya multilingual (CPU)", "cpu", 1, load_s, wall, lat))


def run_llm():
    import httpx
    from dotenv import dotenv_values
    env = dotenv_values("/root/yyy/DataAnalysis_Agent/.env")
    key = env["DATA_AGENT_INTENT_MODEL_API_KEY"].strip("'\"")
    base = env["DATA_AGENT_INTENT_MODEL_BASE_URL"].strip("'\"")
    model = env["DATA_AGENT_V2_RECOGNITION_MODEL_NAME"].strip("'\"")
    client = httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=60)

    def prompt_intent(text):
        opts = "\n".join(f"- {k}: {v}" for k, v in INTENT_CRITERIA.items())
        return (f"{INTENT_INSTRUCTIONS}\n意图类别：\n{opts}\n"
                f"只输出JSON：{{\"label\": \"<类别名>\", \"confidence\": <0到1的小数>}}\n用户输入：{text}")

    def prompt_follow(ctx, text):
        return (f"{FOLLOWUP_INSTRUCTIONS}\n上一轮查询任务：{ctx}\n本轮追问：{text}\n"
                f"只输出JSON：{{\"reuse\": true/false, \"confidence\": <0到1的小数>}}")

    CONCURRENCY = 4
    sem = asyncio.Semaphore(CONCURRENCY)

    async def ask(prompt):
        async with sem:
            t = time.perf_counter()
            resp = await client.post("/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": 100,
            })
            ms = (time.perf_counter() - t) * 1000
            content = resp.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{[^}]*\}", content, re.S)
            return (json.loads(m.group()) if m else {}), ms

    async def main():
        load_s = 0.0  # 远程API无本地加载
        records = []
        wall0 = time.perf_counter()
        results = await asyncio.gather(*[ask(prompt_intent(it["text"])) for it in DATA["intent"]],
                                       return_exceptions=True)
        for item, r in zip(DATA["intent"], results):
            if isinstance(r, Exception):
                records.append({"task": "intent", "text": item["text"], "expected": item["label"],
                                "got": "ERROR", "confidence": 0, "ok": False, "latency_ms": 60000})
                continue
            ans, ms = r
            got = ans.get("label", "ERROR")
            records.append({"task": "intent", "text": item["text"], "expected": item["label"],
                            "got": got, "confidence": float(ans.get("confidence", 0) or 0),
                            "ok": got == item["label"], "latency_ms": round(ms, 1)})
        results = await asyncio.gather(*[ask(prompt_follow(it["context"], it["text"])) for it in DATA["followup"]],
                                       return_exceptions=True)
        for item, r in zip(DATA["followup"], results):
            if isinstance(r, Exception):
                records.append({"task": "followup", "text": f"[{item['context']}] {item['text']}",
                                "expected": item["label"], "got": "ERROR", "confidence": 0,
                                "ok": False, "latency_ms": 60000})
                continue
            ans, ms = r
            got = "SNAPSHOT" if bool(ans.get("reuse")) else "NEW_QUERY"
            records.append({"task": "followup", "text": f"[{item['context']}] {item['text']}",
                            "expected": item["label"], "got": got,
                            "confidence": float(ans.get("confidence", 0) or 0),
                            "ok": got == item["label"], "latency_ms": round(ms, 1)})
        wall = time.perf_counter() - wall0
        await client.aclose()
        lat = [r["latency_ms"] for r in records]
        report("llm", records, build_meta("llm", f"基线大模型 ({model})", "remote-api",
                                          CONCURRENCY, load_s, wall, lat))

    asyncio.run(main())


if __name__ == "__main__":
    {"laya": run_laya, "llm": run_llm}[sys.argv[1]]()
