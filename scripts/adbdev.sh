#!/bin/bash
# 选中部署了 rkllm-chat 的目标板（多设备时避免歧义）。用法: ./adbdev.sh shell "..."
for t in $(adb devices -l | grep -oE 'transport_id:[0-9]+' | cut -d: -f2); do
  if adb -t "$t" shell "test -d /data/rkllm-chat" 2>/dev/null; then
    exec adb -t "$t" "$@"
  fi
done
echo "no target board" >&2; exit 1
