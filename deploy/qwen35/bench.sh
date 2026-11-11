#!/bin/bash
# 板端 Qwen3.5-0.8B 多模态基准测试
# 用法: ./bench.sh <max_new_tokens> [preset_idx] [image]
#   preset_idx: 0=英文提问, 1=中文提问（必须用预设，自定义输入不会带 <image> token）
set -u
cd /data/rkllm_qwen35 || exit 1
export LD_LIBRARY_PATH=$PWD/lib

MT="${1:-192}"
IDX="${2:-1}"
IMG="${3:-demo.jpg}"
LOG=/tmp/bench.log
FIFO=/tmp/bench.fifo
MODEL=./Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm

cleanup() {
    [ -n "${WRITER:-}" ] && kill -9 -- -"$WRITER" 2>/dev/null
    [ -n "${PID:-}" ] && kill -9 -- -"$PID" 2>/dev/null
    rm -f "$FIFO"
}
trap cleanup EXIT INT TERM

rm -f "$LOG" "$FIFO"
# 预热 page cache，避免把 eMMC 读带宽算进首字延迟
cat "$MODEL" > /dev/null 2>&1
cat ./Qwen3.5-0.8B-MIT_vision_rk3576.rknn > /dev/null 2>&1

mkfifo "$FIFO"
t0=$(date +%s%3N)

setsid bash -c "printf '%s\n' '$IDX'; sleep 900" > "$FIFO" 2>/dev/null &
WRITER=$!
setsid ./demo "$IMG" ./Qwen3.5-0.8B-MIT_vision_rk3576.rknn "$MODEL" \
        "$MT" 1024 2 rk3576 \
        "<|vision_start|>" "<|vision_end|>" "<|image_pad|>" \
        < "$FIFO" > "$LOG" 2>&1 &
PID=$!

tfirst=""
prev=-1
stable=0
while kill -0 "$PID" 2>/dev/null; do
    sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if [ -z "$tfirst" ] && grep -q 'robot:' "$LOG" 2>/dev/null; then
        tfirst=$(date +%s%3N)
    fi
    if [ -n "$tfirst" ]; then
        if [ "$sz" -eq "$prev" ]; then stable=$((stable + 1)); else stable=0; fi
        [ "$stable" -ge 3 ] && break
    fi
    prev=$sz
    sleep 0.15
done
tend=$(date +%s%3N)

PRE=${tfirst:-$tend}
echo "=============================================="
echo "max_new_tokens = $MT   preset = $IDX"
echo "首字前(load+imgenc+prefill) : $((PRE - t0)) ms"
echo "首字后到回答结束            : $((tend - PRE)) ms"

ANS=$(sed -n '/robot:/,$p' "$LOG" | sed '1s/^.*robot: //' | sed '/^user: *$/,$d')
CHARS=$(printf '%s' "$ANS" | wc -m)
echo "回答字符数                  : $CHARS"
echo "=============================================="
printf '%s\n' "$ANS" | head -6
echo "----------------------------------------------"
grep -E "LLM Model loaded|ImgEnc Model inference|npu_core_num|model_dtype" "$LOG" | head
