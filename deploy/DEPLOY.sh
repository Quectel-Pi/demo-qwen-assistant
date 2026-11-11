#!/usr/bin/env bash
#
# DEPLOY.sh —— 把本目录归档的部署环境还原到 RK3576 设备（幂等）
#
# 做的事：
#   1. 推送 UI 源码      -> /data/rkllm-chat/ui/rkchat/
#   2. 推送服务端源码     -> /data/rkllm-chat/native/
#   3. 推送辅助脚本       -> /data/rkllm_qwen35/
#   4. 推送 systemd unit  -> /etc/systemd/system/rkllm-chat.service
#   5. daemon-reload + 重启服务
#
# 用法：
#   ./DEPLOY.sh --dry-run          # 只打印将要做什么，不碰设备
#   ./DEPLOY.sh                    # 实际部署并重启服务
#   ./DEPLOY.sh --no-restart       # 部署但不重启服务（改 UI 时常用）
#   ./DEPLOY.sh --build-native     # 顺便交叉编译 rkllm-service 再推送
#
# 环境变量：
#   ADB=/path/to/adb               指定 adb（默认从 PATH 找）
#   CROSS=aarch64-linux-gnu-       交叉编译前缀（默认 aarch64-linux-gnu-）
#
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
ADB=${ADB:-adb}

DRY_RUN=0
RESTART=1
BUILD_NATIVE=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN=1 ;;
        --no-restart)   RESTART=0 ;;
        --build-native) BUILD_NATIVE=1 ;;
        -h|--help)      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg（--help 看用法）" >&2; exit 2 ;;
    esac
done

say()  { printf '%s\n' "$*"; }
# 参数一律走数组，不用 eval；路径含空格也不会被拆开
run()  {
    local tag='[exec]'
    [ "$DRY_RUN" = 1 ] && tag='[dry-run]'
    printf '%s' "$tag"; printf ' %q' "$@"; printf '\n'
    [ "$DRY_RUN" = 1 ] || "$@"
}

# ── 选目标板：优先 /data/rkllm-chat 存在的设备 ────────────────────
pick_device() {
    local t
    for t in $($ADB devices -l | grep -oE 'transport_id:[0-9]+' | cut -d: -f2); do
        if $ADB -t "$t" shell "test -d /data/rkllm-chat" 2>/dev/null; then
            TID=$t; return 0
        fi
    done
    return 1
}

# dry-run 时设备可能不存在，允许跳过
if pick_device; then
    say "目标设备: transport_id=$TID"
else
    if [ "$DRY_RUN" = 1 ]; then
        TID=""
        say "目标设备: <未连接，dry-run 继续>"
    else
        say "错误：找不到部署了 /data/rkllm-chat 的设备" >&2
        say "      连上设备后重试，或用 --dry-run 先看计划" >&2
        exit 1
    fi
fi

# 所有设备命令走这个函数，dry-run 只打印
DEV_SHELL() {
    if [ -n "$TID" ]; then
        run "$ADB" -t "$TID" shell "$1"
    else
        run "$ADB" shell "$1"
    fi
}
DEV_PUSH() {
    local src=$1 dst=$2
    if [ -n "$TID" ]; then
        run "$ADB" -t "$TID" push "$src" "$dst"
    else
        run "$ADB" push "$src" "$dst"
    fi
}

# ── 可选：交叉编译服务端 ──────────────────────────────────────────
if [ "$BUILD_NATIVE" = 1 ]; then
    NATIVE_BIN="$HERE/native/rkllm-service"
    say ""
    say "== 交叉编译 rkllm-service =="
    CXX="${CROSS:-aarch64-linux-gnu-}g++"
    if ! command -v "$CXX" >/dev/null 2>&1; then
        say "错误：找不到交叉编译器 $CXX（sudo apt install g++-aarch64-linux-gnu）" >&2
        exit 1
    fi
    LLM_LIB="$ROOT/rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64"
    NN_LIB="$ROOT/rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64"
    [ -d "$LLM_LIB" ] || { say "错误：缺 $LLM_LIB" >&2; exit 1; }
    [ -d "$NN_LIB" ]  || { say "错误：缺 $NN_LIB" >&2; exit 1; }
    run "$CXX" -std=c++17 -O2 -o "$NATIVE_BIN" "$HERE/native/rkllm_service.cpp" \
        -I"$HERE/native" -L"$LLM_LIB" -L"$NN_LIB" -lrkllmrt -lrknnrt -lpthread
fi

# ── 1. UI 源码 ───────────────────────────────────────────────────
say ""
say "== 1/5 推送 UI 源码 -> /data/rkllm-chat/ui/rkchat/ =="
DEV_SHELL "mkdir -p /data/rkllm-chat/ui/rkchat"
for f in rkchat.py desktop_session.py client.py camera.py voice.py vkbd.py style.css run.sh; do
    [ -f "$HERE/ui/rkchat/$f" ] || { say "跳过（不存在）: $f"; continue; }
    DEV_PUSH "$HERE/ui/rkchat/$f" "/data/rkllm-chat/ui/rkchat/$f"
done
# 清掉 pyc，避免旧字节码盖过新源码
DEV_SHELL "rm -rf /data/rkllm-chat/ui/rkchat/__pycache__"

# ── 2. 服务端 ────────────────────────────────────────────────────
say ""
say "== 2/5 推送服务端 -> /data/rkllm-chat/native/ =="
for f in rkllm_service.cpp rkllm_service.h rkllm.h rknn_api.h stb_image.h; do
    [ -f "$HERE/native/$f" ] || continue
    DEV_PUSH "$HERE/native/$f" "/data/rkllm-chat/native/$f"
done
# 二进制只在显式 --build-native 时覆盖（否则保留设备上已编译好的）
if [ "$BUILD_NATIVE" = 1 ] && [ -f "$HERE/native/rkllm-service" ]; then
    # Do not overwrite a mapped executable (ETXTBSY). Stage and rename atomically;
    # the service keeps the old inode until step 5 restarts it.
    DEV_PUSH "$HERE/native/rkllm-service" "/data/rkllm-chat/native/rkllm-service.new"
    DEV_SHELL "chmod +x /data/rkllm-chat/native/rkllm-service.new && mv -f /data/rkllm-chat/native/rkllm-service.new /data/rkllm-chat/native/rkllm-service"
else
    say "   （跳过 rkllm-service 二进制；需要更新请加 --build-native）"
fi

# 语音模型和二进制依赖仍由设备环境提供，仅同步小体积后端源码。
if [ -f "$HERE/voice-vision/talk.py" ]; then
    DEV_SHELL "mkdir -p /data/voice-vision"
    DEV_PUSH "$HERE/voice-vision/talk.py" "/data/voice-vision/talk.py"
fi

# ── 3. 辅助脚本 ──────────────────────────────────────────────────
say ""
say "== 3/5 推送辅助脚本 -> /data/rkllm_qwen35/ =="
DEV_SHELL "mkdir -p /data/rkllm_qwen35"
for f in chat.sh run.sh bench.sh stress.sh chat.py; do
    [ -f "$HERE/qwen35/$f" ] || continue
    DEV_PUSH "$HERE/qwen35/$f" "/data/rkllm_qwen35/$f"
    DEV_SHELL "chmod +x /data/rkllm_qwen35/$f"
done

# ── 4. systemd unit ──────────────────────────────────────────────
say ""
say "== 4/5 推送 systemd unit =="
if [ -f "$HERE/rkllm-chat.service" ]; then
    DEV_PUSH "$HERE/rkllm-chat.service" "/etc/systemd/system/rkllm-chat.service"
    DEV_SHELL "systemctl daemon-reload"
else
    say "   跳过（deploy/rkllm-chat.service 不存在）"
fi

# ── 5. 重启服务 ──────────────────────────────────────────────────
say ""
if [ "$RESTART" = 1 ]; then
    say "== 5/5 重启推理服务 =="
    DEV_SHELL "systemctl enable rkllm-chat 2>/dev/null; systemctl restart rkllm-chat"
    if [ "$DRY_RUN" = 0 ]; then
        sleep 8
        state=$($ADB -t "$TID" shell "systemctl is-active rkllm-chat" | tr -d '\r')
        say "服务状态: $state"
        $ADB -t "$TID" shell "journalctl -u rkllm-chat -n 5 --no-pager" 2>/dev/null | tail -5
    fi
else
    say "== 5/5 跳过重启（--no-restart）=="
    say "   生效方式：systemctl restart rkllm-chat"
fi

# ── 校验：比对本地与设备上的文件 md5 ─────────────────────────────
if [ "$DRY_RUN" = 0 ] && [ -n "$TID" ]; then
    say ""
    say "== 校验归档与设备一致性 =="
    _chk() {  # $1=本地文件 $2=设备路径
        local l r
        [ -f "$1" ] || return 0
        l=$(md5sum "$1" | cut -d' ' -f1)
        r=$($ADB -t "$TID" shell "md5sum $2 2>/dev/null" | cut -d' ' -f1 | tr -d '\r')
        if [ "$l" = "$r" ]; then printf '  OK   %s\n' "$2"
        else printf '  DIFF %s (local=%s dev=%s)\n' "$2" "$l" "$r"; fi
    }
    for f in rkchat.py desktop_session.py client.py camera.py voice.py vkbd.py style.css run.sh; do
        _chk "$HERE/ui/rkchat/$f" "/data/rkllm-chat/ui/rkchat/$f"
    done
    for f in rkllm_service.cpp rkllm_service.h rkllm.h rknn_api.h stb_image.h; do
        _chk "$HERE/native/$f" "/data/rkllm-chat/native/$f"
    done
    if [ -f "$HERE/voice-vision/talk.py" ]; then
        _chk "$HERE/voice-vision/talk.py" "/data/voice-vision/talk.py"
    fi
    for f in chat.sh run.sh bench.sh stress.sh chat.py; do
        _chk "$HERE/qwen35/$f" "/data/rkllm_qwen35/$f"
    done
fi

say ""
if [ "$DRY_RUN" = 1 ]; then
    say "dry-run 结束，未改动设备。"
else
    say "部署完成。UI 启动："
    say "  adb shell \"cd /data/rkllm-chat/ui/rkchat && ./run.sh\""
fi
