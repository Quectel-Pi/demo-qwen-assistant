#!/bin/sh
# 从 adb/root 或桌面启动，统一切到真实桌面用户的原生会话。
# 默认 GNOME 系统键盘 + IBus 拼音；后备键盘须显式 --keyboard builtin。
# ./run.sh [--windowed] [--dev /dev/video74] [--keyboard system|builtin]
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOCK=/run/rkllm-chat.sock
SERVICE=rkllm-chat.service

if [ ! -S "$SOCK" ]; then
    # disabled 只表示不开机自启，不应阻止用户手动启动。
    systemctl start "$SERVICE" 2>/dev/null || true
    i=0
    while [ ! -S "$SOCK" ] && [ "$i" -lt 40 ]; do
        sleep 0.5
        i=$((i + 1))
    done
fi
if [ ! -S "$SOCK" ]; then
    echo "[run] 推理服务未就绪，UI 可启动但提问将失败" >&2
fi

WINDOWED=0
for arg in "$@"; do
    [ "$arg" != "--windowed" ] || WINDOWED=1
done
cd "$HERE"
if [ "$WINDOWED" -eq 1 ]; then
    exec python3 "$HERE/desktop_session.py" "$@"
else
    exec python3 "$HERE/desktop_session.py" --fullscreen "$@"
fi
