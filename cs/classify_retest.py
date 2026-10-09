# 把重测结果按状态和问题特征归类，输出待人工复核的清单
import json
from collections import Counter, defaultdict

rows = [json.loads(l) for l in open("/root/yyy/cs/retest_results.jsonl", encoding="utf-8")]

print("=== 状态分布 ===")
print(Counter(r["status"] for r in rows))

def classify(r):
    text = r["answer"] + r["clarify"]
    if r["status"] == "COMPLETED":
        if any(k in text for k in ("未匹配", "尚未", "需要补充", "请补充")):
            return "COMPLETED但含补充提示"
        if len(text.strip()) < 30:
            return "COMPLETED但回答过短"
        return "OK"
    if r["status"] == "SAFE_FALLBACK":
        return "SAFE_FALLBACK"
    if r["status"] == "NEEDS_CLARIFICATION":
        return "澄清"
    if r["status"] == "EXC":
        return "请求异常"
    return f"其他:{r['status']}"

groups = defaultdict(list)
for r in rows:
    groups[classify(r)].append(r)

print("\n=== 分组 ===")
for g, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print(f"\n## {g} ({len(items)}条)")
    for r in items:
        print(f"- [{r['secs']}s|{r['error_code'] or ''}] {r['question']}")
        snippet = (r["answer"] or r["clarify"])[:180].replace("\n", " ")
        if g != "OK":
            print(f"    ↳ {snippet}")

with open("/root/yyy/cs/retest_classified.json", "w", encoding="utf-8") as f:
    json.dump({g: [r["question"] for r in items] for g, items in groups.items()}, f, ensure_ascii=False, indent=1)
print("\n明细已写入 /root/yyy/cs/retest_classified.json")
