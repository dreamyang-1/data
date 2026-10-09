#!/bin/bash
# finisher_v4.sh — 等第四批 500 轮跑完后：冲刷 monitor → 离线重审 recheck → 标记完成
cd /root/yyy/ceshi

for i in $(seq 1 240); do
  pgrep -f "monitor_errors.py --qa-log qa_log_v4" >/dev/null || break
  sleep 30
done
pkill -f "monitor_errors.py --qa-log qa_log_v4" 2>/dev/null

python3 recheck.py --qa-log qa_log_v4.jsonl --bank bank_v4.json \
  --out-json corrected_report_v4.json --out-md corrected_report_v4.md > recheck_v4.out 2>&1

echo "$(date '+%F %T') done" > finisher_v4.done
