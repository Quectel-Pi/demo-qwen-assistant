#!/bin/bash
# Qwen3.5-0.8B 多模态交互对话启动器
# 用法: ./chat.sh [图片路径] [max_new_tokens]
#   默认: ./chat.sh demo.jpg 256
#
# 进去之后:
#   <image>你的问题      → 带图提问（推荐，每轮都要带 <image>）
#   0 / 1                → 预设问题（英文 / 中文「这张图片中有什么」）
#   clear                → 清 KV cache（换话题时用）
#   exit                 → 退出
cd "$(dirname "$0")" || exit 1
export LD_LIBRARY_PATH=$PWD/lib

IMG="${1:-demo.jpg}"
MT="${2:-256}"

[ -f "$IMG" ] || { echo "图片不存在: $IMG"; exit 1; }

AVAIL=$(free -m | awk '/^Mem:/{print $7}')
if [ "$AVAIL" -lt 1800 ]; then
    echo "[警告] 可用内存 ${AVAIL} MB < 1800 MB，可能 OOM"
    echo "       建议: systemctl isolate multi-user.target  关掉桌面再跑"
fi

echo "==========================================================="
echo " 模型  : Qwen3.5-0.8B-MIT W4A16 (RK3576 NPU 2核)"
echo " 图片  : $IMG"
echo " 上限  : $MT new tokens"
echo "-----------------------------------------------------------"
echo " 提问格式:  <image>你的问题       ← 每轮都要带 <image>"
echo " 预设    :  0=英文  1=中文"
echo " 其他    :  clear 清缓存   exit 退出"
echo " 注意    :  无多轮记忆(keep_history=0)，每次提问都要重新带 <image>"
echo "           图片是启动时加载的，换图需退出重开"
echo "==========================================================="

exec ./demo "$IMG" ./Qwen3.5-0.8B-MIT_vision_rk3576.rknn \
     ./Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm \
     "$MT" 1024 2 rk3576 \
     "<|vision_start|>" "<|vision_end|>" "<|image_pad|>"
