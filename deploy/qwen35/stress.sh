#!/bin/bash
# 连续压力测试：观察温度 / NPU 频率 / 可用内存 / decode 时间
cd /data/rkllm_qwen35 || exit 1
export LD_LIBRARY_PATH=$PWD/lib
M=./Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm
echo "run  elapsed  maxT(C)  npu_freq  availMB  prefill_ms  decode_ms"
t0=$(date +%s)
for i in 1 2 3 4; do
  LOG=/tmp/st$i.log; IN=/tmp/st$i.in
  printf '1\n' > "$IN"; rm -f "$LOG"
  T0=$(date +%s%3N)
  setsid bash -c "tail -f $IN | ./demo demo.jpg ./Qwen3.5-0.8B-MIT_vision_rk3576.rknn $M 400 1024 2 rk3576 '<|vision_start|>' '<|vision_end|>' '<|image_pad|>'" > "$LOG" 2>&1 &
  P=$!
  TF=""
  prev=-1; stable=0
  while kill -0 $P 2>/dev/null; do
    sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if [ -z "$TF" ] && grep -q robot: "$LOG" 2>/dev/null; then TF=$(date +%s%3N); fi
    if [ -n "$TF" ]; then
      if [ "$sz" -eq "$prev" ]; then stable=$((stable+1)); else stable=0; fi
      [ $stable -ge 3 ] && break
    fi
    prev=$sz; sleep 0.15
  done
  T1=$(date +%s%3N)
  kill -9 -- -$P 2>/dev/null; sleep 1
  TMAX=$(cat /sys/class/thermal/thermal_zone*/temp | sort -n | tail -1)
  FRQ=$(cat /sys/class/devfreq/27700000.npu/cur_freq)
  AV=$(free -m | awk '/^Mem:/{print $7}')
  printf "%3d  %7d  %6d  %8s  %7s  %9s  %9s\n" $i $(( $(date +%s)-t0 )) $((TMAX/1000)) "$FRQ" "$AV" "$(( ${TF:-$T1} - T0 ))" "$(( T1-${TF:-$T1} ))"
  rm -f "$IN"
done
