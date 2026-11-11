# Quectel Pi M2 (RK3576) 部署环境归档

本目录是板端 **Qwen3.5-0.8B 多模态语音助手** 的完整部署环境快照，用于复现和二次开发。

模型权重（`.rkllm` 约 1 GB、vision `.rknn` 约 214 MiB）**不在本目录**，见 [模型文件](#模型文件)。

语音后端源码补充在 [`voice-vision/`](voice-vision/README.md)；模型及 ONNX/sherpa
依赖仍复用设备已有环境。默认优先独立 USB 麦克风，避免 ES8389 同卡录放音争用。

---

## 1. 目录结构

```
deploy/
├── README.md                 # 本文件
├── DEPLOY.sh                 # 幂等部署脚本（支持 --dry-run）
├── rkllm-chat.service        # systemd unit（注意：出厂为 disabled，需跑一次 DEPLOY.sh 或手动 enable）
│
├── ui/rkchat/                # GTK3 前端 -> /data/rkllm-chat/ui/rkchat/
│   ├── rkchat.py             # 主界面：对话气泡、摄像头、TTS、系统键盘
│   ├── desktop_session.py    # 切到真实桌面用户及原生输入法环境
│   ├── client.py             # Unix socket 通信层（收流式 token，回主线程渲染）
│   ├── camera.py             # ffmpeg预览流 + 带时间/会话校验的新鲜快照
│   ├── voice.py              # 语音识别/合成（sherpa-onnx）
│   ├── vkbd.py               # 可选后备键盘（--keyboard builtin）
│   ├── style.css             # GTK CSS 主题
│   └── run.sh                # 启动器：拉起服务 + 桌面用户/原生会话 + 全屏
│
├── native/                   # 推理服务（C++）-> /data/rkllm-chat/native/
│   ├── rkllm_service.cpp     # 服务主程序源码（socket 服务 + 多模态推理）
│   ├── rkllm_service.h
│   ├── rkllm.h               # RKLLM C API 头文件
│   ├── rknn_api.h            # RKNN C API 头文件
│   ├── stb_image.h           # 图片解码（单头文件库）
│   ├── rkllm-service         # 已编译的 aarch64 二进制
│   └── rkllm-service.bak     # 摄像头修复**之前**的基线二进制（非「上一版」，缺 ev=image 视觉 KV 刷新）
│
└── qwen35/                   # 板端辅助脚本 -> /data/rkllm_qwen35/
    ├── chat.sh               # 交互对话启动器（单图多轮）
    ├── run.sh                # 单次问答（带计时）
    ├── bench.sh              # 性能基准
    ├── stress.sh             # 压力测试
    └── chat.py               # CLI 对话界面（TUI）
```

---

## 2. 设备信息

| 项 | 值 |
|---|---|
| 板卡 | Quectel PI M2 RK3576 Board |
| 内核 | Linux 6.1.118-rt36（PREEMPT_RT） |
| 发行版 | Debian GNU/Linux 13 (trixie) |
| Python | 3.13.5 |
| NPU 驱动 | 0.9.8 |
| rkllm runtime | **1.3.1**（`librkllmrt.so`） |
| rknn runtime | **2.3.2**（`librknnrt.so`） |
| 内存 | 3.9 GB |
| 存储 | `/` 42 GB，`/userdata` 8 GB |

> **运行时版本约束**：模型由 `rkllm-toolkit 1.3.1` 转换，板端 runtime **必须 ≥ 1.3.1**。
> 若降级到 1.3.0，加载时报
> `The rkllm-runtime version is lower than the rkllm-toolkit version`。

两个 so 均与仓库 `rknn-llm/` 中官方文件逐字节一致：

```
librkllmrt.so  2a4867ece83de3735a37f6459811eb92
  ← rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/
librknnrt.so   a37ee1d5d664c79836bf6e35b7ef6289
  ← rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64/
```

---

## 3. 模型文件

板端 `/data/rkllm_qwen35/` 下的模型（**未纳入本归档**，体积过大）：

| 文件 | 大小 | md5 | 说明 |
|---|---|---|---|
| `Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm` | 1068 MB | `d08e1bc0b3ecd7a5fd52b4ad5f8906fe` | **当前线上**，W4A16 |
| `Qwen3.5-0.8B-MIT_vision_rk3576.rknn` | 214 MB | `25dc659db250932fd284d9781f935399` | **当前线上**，ViT 448×448 |
| `Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm` | 1126 MB | `e65c820dbc4e45f57a042a441851d157` | 原版 base 模型（回滚用） |
| `Qwen3.5-0.8B_vision_rk3576.rknn` | 225 MB | `880af90aab7578b9c0e546b8d2bbf441` | 原版 vision（回滚用） |

### 来源与转换

- **MIT 版权重**：`RikiyaT/Qwen3.5-0.8B-weight-space-programming`
  —— weight-space edit，只改了 layers 15/19 的 `down_proj` 共 131,072 个权重（0.016%），
  用于打断思考死循环。
- **转换方式**：见仓库根 `scripts/01_convert_all.sh`，本机转换产物在 `out/`。

> ⚠️ **注意差异**：本机 `out/qwen3.5-0.8b-mit_w4a16_rk3576.rkllm`（941 MB，md5 `00380f51...`）
> 是 `optimization_level=0` 的产物，**输出乱码不可用**；线上用的是 `optimization_level=1`
> 重新转换的 1068 MB 版本。重建设备时必须用 `optimization_level=1`。

### 备份

设备上完整备份在 `/userdata/backup-qwen35-20260419-102350/`（1.4 GB）：
旧模型 + `rkllm-chat` 源码 + systemd unit + 1.3.0 版 runtime 库。

---

## 4. 架构与运行机制

```
┌─────────────────────────────────────────────┐
│  rkchat.py (GTK3 前端, 全屏 1280x800)        │
│   对话气泡 / 摄像头预览 / TTS / 虚拟键盘       │
└───────────────┬─────────────────────────────┘
                │ Unix socket  /run/rkllm-chat.sock
                │ 一行一个 JSON，流式 token
┌───────────────▼─────────────────────────────┐
│  rkllm-service (C++, 常驻)                   │
│   ├── RKNN: vision encoder (~0.8 s/图)      │
│   └── RKLLM: LLM 权重常驻内存                │
└─────────────────────────────────────────────┘
```

**为什么需要常驻服务**：官方 demo 把图片固定在启动参数里，换图要重启进程（约 7 秒）。
本服务每轮单独构造 `RKLLMInput`，图片编码结果作为 `image_embed` 传入，
换图只需重跑 vision encoder（0.8 秒），LLM 权重始终常驻。

### Socket 协议

```jsonc
// 请求
{"cmd":"ask","prompt":"<image>这张图里有什么？","image":"/tmp/a.jpg","keep_history":1,"max_new":256}
{"cmd":"abort"}     // 中断当前生成
{"cmd":"clear"}     // 清 KV cache
{"cmd":"ping"}      // 探活

// 响应（流式）
{"ev":"image","kv_reset":1,"kv_before":223,"kv_after":0,"embedding_hash":"7dbdfd51d061c247"}
{"ev":"tok","t":"这是"}                      // 增量文本
{"ev":"ttft","ms":498}                        // 首字延迟
{"ev":"done","ms":946,"ttft":498,"tokens":7,"chars":36}
{"ev":"error","msg":"图像编码失败，本轮未进行推理，请重新取图"}
{"ev":"error","msg":"run failed"}
```

> **关键点**：图片路径必须与 `prompt` 的字面量 `<image>` 一起发送。
> 缺图片、标记不匹配或解码失败会明确报错，不再降级为可能沿用旧图的纯文本回答。

### 当前画面与上下文规则

- 每次带新图，服务端先清除旧视觉KV，再发送 `ev=image` 诊断事件。事件包含实际送入
  RKLLM 的 embedding 指纹和清理前后KV长度，不能把文件名当作画面已更新的证明。
- 新图代表新场景，不重放上一张图片的描述。关闭摄像头后的纯文字追问仍保留最近
  一张已成功发送的图；跨多张历史图比较不在这一规则的保证范围内。
- UI后台等下一帧，每次使用私有、独立的快照路径。帧超过1秒未更新、超时或会话已
  关闭，本轮不发送；也不会再启动另一个ffmpeg与预览争抢设备。
- 回答生成时允许关闭摄像头。关闭立即隐藏预览、后台释放设备；若本轮图片已发送，
  当前回答仍基于发送时快照。若还在取图，关闭会取消本轮，绝不发送迟到的拍照结果。
- 回答生成中暂不能重新打开摄像头；结束或停止后可重新打开。

### 实测性能（RK3576，2 NPU core，ctx 4096）

| 场景 | TTFT | 总耗时 | tokens |
|---|---|---|---|
| 纯文本 | 495 ms | 944 ms | 7 |
| 带图 448×448 | 3486 ms | 6081 ms | 24 |

服务冷启动：LLM 约 2.8–4.1 s + vision 约 1.3 s，合计约 4–5.4 s。

---

## 5. systemd 服务

`rkllm-chat.service` → `/etc/systemd/system/rkllm-chat.service`

```ini
Environment=LD_LIBRARY_PATH=/data/rkllm_qwen35/lib
ExecStart=/data/rkllm-chat/native/rkllm-service \
    --sock /run/rkllm-chat.sock \
    --llm    /data/rkllm_qwen35/Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm \
    --vision /data/rkllm_qwen35/Qwen3.5-0.8B-MIT_vision_rk3576.rknn \
    --ctx 4096 --max-new 256 --cores 2
Restart=always
RuntimeDirectory=rkllm-chat
```

**切换回原版模型**：把 `ExecStart` 里的两个文件名改回
`Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm` / `Qwen3.5-0.8B_vision_rk3576.rknn`，
然后 `systemctl daemon-reload && systemctl restart rkllm-chat`。

常用操作：

```bash
systemctl status rkllm-chat
systemctl restart rkllm-chat
journalctl -u rkllm-chat -f          # 看推理日志
journalctl -u rkllm-chat -n 50       # 看最近日志
```

---

## 6. 从本目录还原到设备

### 前置条件

- 设备已连 adb（`adb devices` 能看到）
- 设备上已有 `/data/rkllm_qwen35/` 目录和模型文件（模型不在归档内）
- 板端已装 `python3-gi`、GTK3、sherpa-onnx

### 一键部署

```bash
cd deploy
./DEPLOY.sh --dry-run        # 先看计划，不碰设备
./DEPLOY.sh                  # 实际部署并重启服务
./DEPLOY.sh --no-restart     # 只推 UI 源码，不重启服务
./DEPLOY.sh --build-native   # 顺便交叉编译 rkllm-service 再推送
```

脚本行为（幂等，可反复执行）：
1. 推送 `ui/rkchat/*` → `/data/rkllm-chat/ui/rkchat/`，并清掉 `__pycache__`
2. 推送 `native/*` 源码 → `/data/rkllm-chat/native/`（二进制仅在 `--build-native` 时覆盖）
3. 推送 `qwen35/*` → `/data/rkllm_qwen35/`，并 `chmod +x`
4. 推送 systemd unit → `/etc/systemd/system/`，`daemon-reload`
5. `systemctl enable + restart rkllm-chat`，等待后打印服务状态

### 启动 UI

```bash
adb shell
cd /data/rkllm-chat/ui/rkchat
./run.sh                      # 桌面用户 + 原生会话，默认系统键盘，全屏
./run.sh --windowed            # 窗口模式（调试）
./run.sh --keyboard builtin    # 仅无系统键盘的环境才显式用后备键盘
```

也可直接从 adb 启动（内部会自动修正运行用户与环境）：

```bash
python3 rkchat.py --fullscreen 1280x800+0+0
```

**当前 GNOME 48 Wayland 桌面不要强制 `GDK_BACKEND=x11` 或以 root 直接运行
窗口。** `desktop_session.py` 自动发现已登录图形会话，切到桌面用户，设置
`GDK_BACKEND=wayland`、`GTK_IM_MODULE=wayland` 与其 D-Bus/runtime 路径。
该协议把系统键盘请求交给 GNOME，中文由 GNOME 内部的 IBus libpinyin 处理。
仅开启 `screen-keyboard-enabled` 而不修正启动方式，仍可能不弹系统键盘。
默认模式完全不构造 `vkbd.VirtualKeyboard`，所以不会同时弹两套键盘。

触摸输入框使用 GNOME 键盘；中英文通过系统键盘语言按钮或 `Super+Space`
切换。语音朗读默认开启不变，麦克风仍由「语音」按钮控制。

Wayland 的系统键盘是 GNOME 合成层，不在 `xwininfo` 树里；验证应使用
合成器截图与实际输入事件，而非仅看窗口树或发送 socket 消息。

---

## 7. 重新编译推理服务

`native/rkllm-service` 是 **aarch64 ELF 二进制**，源码为 `native/rkllm_service.cpp`。

### 方式一：DEPLOY.sh（推荐）

```bash
cd deploy
./DEPLOY.sh --build-native            # 编译 + 推送 + 重启
```

默认用 `aarch64-linux-gnu-g++`，可覆盖：

```bash
CROSS=/opt/gcc-arm-10.3/bin/aarch64-none-linux-gnu- ./DEPLOY.sh --build-native
```

需要 `sudo apt install g++-aarch64-linux-gnu`。

### 方式二：手动

```bash
cd deploy/native

aarch64-linux-gnu-g++ -std=c++17 -O2 \
  -o rkllm-service rkllm_service.cpp \
  -I. \
  -L../../rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64 \
  -L../../rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64 \
  -lrkllmrt -lrknnrt -lpthread

adb push rkllm-service /data/rkllm-chat/native/
adb shell "chmod +x /data/rkllm-chat/native/rkllm-service"
adb shell "systemctl restart rkllm-chat"
```

二进制只依赖 `librkllmrt.so`、`librknnrt.so`、`libstdc++`、`libm`、`libgcc_s`、`libc`，
运行时通过 `LD_LIBRARY_PATH=/data/rkllm_qwen35/lib` 找到前两个。

> 注意：上面这条命令**不写入 RUNPATH**，所以脱离 systemd 直接跑会报
> `librkllmrt.so: not found`，必须自己带 `LD_LIBRARY_PATH`。仓库里的
> `rkllm-service.bak` 是旧构建，带 `RUNPATH=/data/rkllm_qwen35/lib`，两者行为不同。
> 需要可独立运行的二进制时加 `-Wl,-rpath,/data/rkllm_qwen35/lib`。

### 更完整的交叉编译环境

仓库根 `rknn-llm/examples/multimodal_model_demo/deploy/` 里有官方 demo 的 CMake 工程
（`build-linux.sh`），包含 OpenCV、RKNN、RKLLM 的完整链接配置，可作为参考。
本服务是独立单文件，不需要 OpenCV。

---

## 8. 已知问题

| 问题 | 说明 |
|---|---|
| 模型通用能力退化 | MIT 版是 weight-space edit，数学循环问题改善，但**通用知识明显变差**（勾股定理、地理常识会答错）。追求通用对话建议回滚原版。 |
| thinking 未启用 | 服务硬编码 `enable_thinking=false`。该模型的核心收益需开 thinking + 32K 上下文才体现，但单题需 3–10 分钟，不适合交互。 |
| 上下文仅 4096 | 博客评测用 32768。板端 `max_context_len` 上限 16384，当前 4096。 |
| 量化损伤 | W4A16 下 0.8B 数学能力损失大，实测 MATH-500 全错。 |
| GUI 自动化测试受限 | mutter/Xwayland 拦截 Xtst 伪造键盘事件，无法脚本点击。验证建议走 socket 协议。 |
| 截图 | `xwd -id <mutter-frame-id>` 可用；`xwd -root` 与 `ffmpeg -i :0` 全黑。 |

---

## 9. 相关路径速查

| 路径 | 内容 |
|---|---|
| `/data/rkllm-chat/ui/rkchat/` | GTK 前端 |
| `/data/rkllm-chat/native/` | 推理服务二进制 + 源码 |
| `/data/rkllm_qwen35/` | 模型、runtime lib、辅助脚本、官方 demo |
| `/run/rkllm-chat.sock` | 服务 socket |
| `/userdata/backup-qwen35-20260419-102350/` | 完整备份 |
| `/userdata/new-model/` | 测试用模型副本 + 打过 thinking/sampling 补丁的 demo |
