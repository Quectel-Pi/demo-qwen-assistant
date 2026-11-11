#!/bin/bash
# Qwen3.5-0.8B 多模态单次问答
# 用法: ./run.sh <image> [0|1] [max_new_tokens]
#   0 = <image>What is in the image?   1 = <image>这张图片中有什么？
cd /data/rkllm_qwen35 || exit 1
export LD_LIBRARY_PATH=$PWD/lib
IMG="${1:-demo.jpg}"; IDX="${2:-1}"; MT="${3:-256}"
IN=/tmp/one.in; LOG=/tmp/one.log
[ -f "$IMG" ] || { echo "图片不存在: $IMG"; exit 1; }
printf '%s\n' "$IDX" > "$IN"; rm -f "$LOG"
T0=$(date +%s%3N)
setsid bash -c "tail -f $(printf %q "$IN") | ./demo $(printf %q "$IMG") ./Qwen3.5-0.8B-MIT_vision_rk3576.rknn ./Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm $(printf %q "$MT") 1024 2 rk3576 $(printf %q '<|vision_start|>') $(printf %q '<|vision_end|>') $(printf %q '<|image_pad|>')" > "$LOG" 2>&1 &
P=$!
TF=""; prev=-1; stable=0
while kill -0 $P 2>/dev/null; do
  sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
  [ -z "$TF" ] && grep -q robot: "$LOG" 2>/dev/null && TF=$(date +%s%3N)
  if [ -n "$TF" ]; then
    [ "$sz" -eq "$prev" ] && stable=$((stable+1)) || stable=0
    [ $stable -ge 3 ] && break
  fi
  prev=$sz; sleep 0.15
done
T1=$(date +%s%3N)
kill -9 -- -$P 2>/dev/null; rm -f "$IN"
ANS=$(sed -n '/robot:/,$p' "$LOG" | sed '1s/^.*robot: //' | sed '/^user: *$/,$d')
echo "图片: $IMG"
echo "首字(含加载): $(( ${TF:-$T1} - T0 )) ms   生成耗时: $(( T1 - ${TF:-$T1} )) ms   字符: $(printf '%s' "$ANS" | wc -m)"
echo "-----------------------------------------------"
printf '%s\n' "$ANS"
