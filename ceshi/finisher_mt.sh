#!/bin/bash
# finisher_mt.sh — 等多轮追问专项跑完后：冲刷 monitor → 离线重审 recheck → 标记完成
cd /root/yyy/ceshi

# 1000 轮 × ≥120s + 211 次会话间隔 ≈ 35 小时，等 48 小时兜底
for i in $(seq 1 5760); do
  pgrep -f "monitor_errors.py --qa-log qa_log_mt" >/dev/null || break
  sleep 30
done
pkill -f "monitor_errors.py --qa-log qa_log_mt" 2>/dev/null

python3 recheck.py --qa-log qa_log_mt.jsonl --bank bank_mt1000.json \
  --out-json corrected_report_mt.json --out-md corrected_report_mt.md > recheck_mt.out 2>&1

echo "$(date '+%F %T') done" > finisher_mt.done
