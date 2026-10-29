#!/bin/bash
# start_v4.sh — 启动第四批 500 轮专项测试流水线
cd /root/yyy/ceshi

nohup python3 probe.py --bank bank_v4.json --qa-log qa_log_v4.jsonl \
  --run-log run_v4.log --summary summary_v4.json --interval 15 \
  > probe_v4.out 2>&1 &
echo "probe PID=$!"

nohup python3 monitor_errors.py --qa-log qa_log_v4.jsonl \
  --state monitor_state_v4.json --err-start 59 \
  --batch-label "第四批测试" --batch-size 5 \
  > monitor_v4.out 2>&1 &
echo "monitor PID=$!"

nohup bash finisher_v4.sh > finisher_v4.out 2>&1 &
echo "finisher PID=$!"
