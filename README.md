# demo-qwen-assistant

> 在 **Quectel Pi M2（RK3576）** 上运行的 **Qwen3.5-0.8B 多模态语音助手** —— 离线语音对话 + 摄像头看图问答。

一块开发板、一颗 NPU、一个 0.8B 模型，做一个能听、能说、能看，并且**完全离线**的桌面语音助手：
全屏 GTK 界面、常驻推理服务、系统键盘输入、USB 麦克风拾音、TTS 朗读，支持
「说话提问 → 拍照 → 看图回答 → 文字追问」多轮交互。

本仓库收录该项目的**源码、部署归档、测试脚本与真机验证报告**。
模型权重与 Rockchip 官方 SDK 体积过大（合计 4 GB+），不在仓库内，见
[获取模型文件](#模型文件)。

---

## 目录

- [特性](#特性)
- [硬件与运行时版本](#硬件与运行时版本)
- [仓库结构](#仓库结构)
- [快速开始](#快速开始)
- [模型文件](#模型文件)
- [架构与运行机制](#架构与运行机制)
- [测试与报告索引](#测试与报告索引)
- [已知限制](#已知限制)
- [仓库体积说明](#仓库体积说明)

---

## 特性

| 能力 | 实现 |
|---|---|
| 语音输入 | sherpa-onnx ASR（流式），USB 麦克风优先，避免与 ES8389 同卡录放音争用 |
| 语音输出 | sherpa-onnx TTS，流式播放，默认开启自动朗读 |
| 看图问答 | RKNN ViT（448×448）编码 + 常驻 RKLLM 推理，换图只需重跑 vision encoder（~0.8 s） |
| 多轮对话 | 常驻 KV cache，文字追问保留最近一张图的上下文 |
| 交互 | GTK3 全屏 UI（1280×800），系统键盘（GNOME 48 OSK + IBus libpinyin 中文输入） |
| 常驻服务 | C++ `rkllm-service` + Unix socket 流式 token，systemd 开机自启 |
| 打断 | `abort` / `clear` 指令，随时停止生成、清空 KV |

**关键设计**：Rockchip 官方多模态 demo 把图片路径固定在启动参数里，换图必须重启进程（约 7 秒）。
本项目把 `rkllm-service` 做成常驻服务，**每轮单独构造 `RKLLMInput`**，图片编码结果以
`image_embed` 形式传入，因此 LLM 权重常驻内存，换图开销降到一次 vision encoder。

---

## 硬件与运行时版本

| 项 | 值 |
|---|---|
| 板卡 | Quectel Pi M2 RK3576 Board |
| 内核 | Linux 6.1.118-rt36（PREEMPT_RT） |
| 发行版 | Debian GNU/Linux 13 (trixie) |
| 桌面 | GNOME 48.7，Wayland |
| Python | 3.13.5（板端） / 3.10（转换环境 `.venv310`） |
| NPU 驱动 | 0.9.8 |
| rkllm runtime | **1.3.1**（`librkllmrt.so`） |
| rknn runtime | **2.3.2**（`librknnrt.so`） |
| 内存 | 3.9 GB |
| 存储 | `/` 42 GB，`/userdata` 8 GB |

> **运行时版本约束**：模型由 `rkllm-toolkit 1.3.1` 转换，板端 runtime **必须 ≥ 1.3.1**。
> 若降级到 1.3.0，加载时报
> `The rkllm-runtime version is lower than the rkllm-toolkit version`。

板端实测性能（RK3576，2 NPU core，ctx 4096）：

| 场景 | TTFT | 总耗时 | tokens |
|---|---|---|---|
| 纯文本 | 495 ms | 944 ms | 7 |
| 带图 448×448 | 3486 ms | 6081 ms | 24 |

服务冷启动：LLM 约 2.8–4.1 s + vision 约 1.3 s，合计约 4–5.4 s。

---

## 仓库结构

```
.
├── README.md                 # 本文件
├── deploy/                   # ★ 板端部署环境归档（部署从这里开始）
│   ├── README.md             #   部署详解：架构、协议、性能、交叉编译、已知问题
│   ├── DEPLOY.sh             #   幂等部署脚本（--dry-run / --no-restart / --build-native）
│   ├── rkllm-chat.service    #   systemd unit
│   ├── ui/rkchat/            #   GTK3 前端：rkchat.py / camera.py / voice.py / client.py
│   ├── native/               #   推理服务（C++）：rkllm_service.cpp + 交叉编译产物
│   ├── qwen35/               #   板端辅助脚本：chat.sh / run.sh / bench.sh / stress.sh
│   └── voice-vision/         #   语音后端补充源码（talk.py）
├── scripts/                  # PC 侧：环境准备、模型转换、交叉编译
│   ├── 00_setup_env.sh       #   建 .venv310 + 装 rkllm/rknn toolkit
│   ├── 01_convert_all.sh     #   校准集 → rkllm → onnx → rknn（产物到 out/）
│   ├── 02_build_deploy.sh    #   交叉编译官方 C++ demo 并打包
│   ├── export_rkllm.py       #   修正版导出脚本（官方版本写死了 optimization_level）
│   └── verify_ui.py          #   UI 静态/结构校验
├── tests/                    # 真机与单元测试脚本
├── reports/                  # 真机验证报告（*.md + JSON/JSONL/日志证据）
└── logs/                     # 转换与调试过程日志（含键盘问题排查记录）
```

`.dev-flow/` 下是每一轮修复/测试的**任务清单（MANIFEST.md）**：授权范围、方案、build/runtime
结论、遗留项。可作为变更审计线索。

---

## 快速开始

### 前置条件

- 板端已有 `/data/rkllm_qwen35/` 目录与模型文件（见下一节）
- 板端已装 `python3-gi`、GTK3、sherpa-onnx
- 主机侧 `adb devices` 能看到设备
- 主机侧需要 `g++-aarch64-linux-gnu` 才能重新编译推理服务

### 一键部署

```bash
cd deploy
./DEPLOY.sh --dry-run        # 先看计划，不碰设备
./DEPLOY.sh                  # 实际部署并重启服务
./DEPLOY.sh --no-restart     # 只推 UI 源码，不重启服务
./DEPLOY.sh --build-native   # 顺便交叉编译 rkllm-service 再推送
```

脚本是**幂等**的，可反复执行：推送 UI 源码 → 推送服务端源码与头文件 → 推送辅助脚本 →
推送 systemd unit → `daemon-reload` → `enable + restart` → 打印服务状态。

### 启动 UI

```bash
adb shell
cd /data/rkllm-chat/ui/rkchat
./run.sh                      # 桌面用户 + 原生 Wayland 会话，默认系统键盘，全屏
./run.sh --windowed           # 窗口模式（调试）
./run.sh --keyboard builtin   # 仅无系统键盘的环境才显式用后备键盘
```

> **不要**强制 `GDK_BACKEND=x11` 或以 root 直接运行窗口。
> `desktop_session.py` 会自动发现已登录图形会话、切到桌面用户、设置
> `GDK_BACKEND=wayland`、`GTK_IM_MODULE=wayland` 及其 D-Bus/runtime 路径。
> 中文输入由 GNOME 内部的 IBus libpinyin 处理。

更多内容（socket 协议、图片上下文规则、systemd 配置、交叉编译、已知问题）见
**[deploy/README.md](deploy/README.md)**。

---

## 模型文件

模型权重体积过大，**不在本仓库内**。板端 `/data/rkllm_qwen35/` 需要的文件：

| 文件 | 大小 | md5 | 说明 |
|---|---|---|---|
| `Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm` | 1068 MB | `d08e1bc0b3ecd7a5fd52b4ad5f8906fe` | 当前线上，W4A16 |
| `Qwen3.5-0.8B-MIT_vision_rk3576.rknn` | 214 MB | `25dc659db250932fd284d9781f935399` | 当前线上，ViT 448×448 |
| `Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm` | 1126 MB | `e65c820dbc4e45f57a042a441851d157` | 原版 base 模型（回滚用） |
| `Qwen3.5-0.8B_vision_rk3576.rknn` | 225 MB | `880af90aab7578b9c0e546b8d2bbf441` | 原版 vision（回滚用） |

### 自行复现

```bash
# 1) 基础模型：RikiyaT/Qwen3.5-0.8B-weight-space-programming
#    （weight-space edit，只改了 layers 15/19 的 down_proj 共 131,072 个权重，用于打断思考死循环）
#    克隆/下载到 ./Qwen3.5-0.8B-MIT/

# 2) 官方 SDK：Rockchip rknn-llm，克隆到 ./rknn-llm/

# 3) 准备转换环境（Ubuntu 22.04 主机，Python 3.10）
bash scripts/00_setup_env.sh

# 4) 转换：校准集 -> rkllm -> onnx -> rknn，产物落在 out/
bash scripts/01_convert_all.sh

# 5) 交叉编译官方 C++ demo 并打包
bash scripts/02_build_deploy.sh
```

> ⚠️ **必须用 `optimization_level=1`**。
> `optimization_level=0` 的产物（941 MB，md5 `00380f51...`）**输出乱码不可用**；
> 线上用的是 `optimization_level=1` 重新转换的 1068 MB 版本。
> 官方自带的 `export_rkllm.py` 把该参数写死为 1、dataset 固定 `data/inputs.json`，
> 本仓库改用 `scripts/export_rkllm.py`（并把 `data/inputs.json` 软链到 `llm_inputs.json`）。

---

## 架构与运行机制

```
┌─────────────────────────────────────────────┐
│  rkchat.py (GTK3 前端, 全屏 1280x800)        │
│   对话气泡 / 摄像头预览 / TTS / 系统键盘       │
└───────────────┬─────────────────────────────┘
                │ Unix socket  /run/rkllm-chat.sock
                │ 一行一个 JSON，流式 token
┌───────────────▼─────────────────────────────┐
│  rkllm-service (C++, 常驻)                   │
│   ├── RKNN: vision encoder (~0.8 s/图)      │
│   └── RKLLM: LLM 权重常驻内存                │
└─────────────────────────────────────────────┘
```

Socket 协议（一行一个 JSON）：

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
```

完整协议说明与「当前画面与上下文规则」见 [deploy/README.md §4](deploy/README.md)。

---

## 测试与报告索引

测试脚本在 `tests/`，覆盖范围包括：全场景真机测试、运行时健壮性、摄像头新鲜度、
音频增益/路由对比、USB 麦克风预热、跟随追问、非模型回归、系统键盘。

| 报告 | 内容 |
|---|---|
| [`reports/full-test/20261109-164746/REPORT.md`](reports/full-test/20261109-164746/REPORT.md) | 当前模型综合真机测试：55 条记录，核心链路 PASS，回答质量/声学识别部分 FAIL |
| [`reports/non-model-fixes/REPORT.md`](reports/non-model-fixes/REPORT.md) | 非模型工程问题修复：语音队列/静音/冷启动、GTK 线程、连接生命周期、音频设备争用 |
| [`reports/camera-freshness/REPORT.md`](reports/camera-freshness/REPORT.md) | 摄像头关闭与旧画面回答修复：逐请求独立快照 + 服务端视觉 KV 重置 |

每份报告目录内附带原始证据：`SUMMARY.json`、`results.jsonl`、`SCENARIOS.csv`、
`runner.log`、`supervisor.json`、模型前后 `sha256`、以及（未入库的）合成器截图与声学回环录音。

> 这些报告如实记录了结论边界：测试执行 COMPLETED 不等于产品质量全部通过。
> 当前模型存在明确的数学错误、无关拒答、名字记忆失败、中英混杂等**模型能力**问题，
> 与工程链路问题分开评价。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 模型通用能力退化 | MIT 版是 weight-space edit，数学循环改善，但通用知识明显变差。追求通用对话建议回滚原版模型。 |
| thinking 未启用 | 服务硬编码 `enable_thinking=false`。该模型核心收益需 thinking + 32K 上下文，但单题 3–10 分钟，不适合交互。 |
| 上下文仅 4096 | 板端 `max_context_len` 上限 16384，当前 4096。 |
| 量化损伤 | W4A16 下 0.8B 数学能力损失大，实测 MATH-500 全错。 |
| GUI 自动化受限 | mutter/Xwayland 拦截 Xtst 伪造键盘事件，无法脚本点击；验证建议走 socket 协议。 |
| 截图 | `xwd -id <mutter-frame-id>` 可用；`xwd -root` 与 `ffmpeg -i :0` 全黑。 |

---

## 仓库体积说明

为保持仓库轻量（当前 < 2 MB），以下内容被 `.gitignore` 排除：

| 排除项 | 体积 | 原因 |
|---|---|---|
| `Qwen3.5-0.8B-MIT/` | 1.7 GB | 基础模型权重 |
| `rknn-llm/` | 2.6 GB | Rockchip 官方 SDK |
| `out/*.rkllm`、`out/*.rknn`、`out/demo_Linux_aarch64/` | 1.2 GB | 转换产物 |
| `wheelhouse/` | 173 MB | 离线 wheels |
| `.venv310/` | 5.3 GB | Python 虚拟环境 |
| `reports/**/*.png|bmp|wav|pid` | — | 截图、录音与运行期临时文件（结论保留在 `*.md` 与 JSON 证据中） |
| `logs/**/*.png|jpg` | — | 调试截图 |

**例外（有意入库）**：`deploy/native/rkllm-service` 与 `deploy/native/rkllm-service.bak`
是 aarch64 交叉编译产物（各约 140 KB）。它们体积小、且是**部署归档与回滚**的一部分，
因此在 `.gitignore` 中被显式白名单放行。若需要纯净源码树，可直接重新编译：
`cd deploy && ./DEPLOY.sh --build-native`。

---

## 许可与致谢

本项目（本仓库内的源码、脚本与文档）采用 **Apache License 2.0**，
完整条款见 [`LICENSE`](LICENSE)。

```
Copyright 2026 Quectel Wireless Solutions Co., Ltd.
Licensed under the Apache License, Version 2.0.
```

- Qwen3.5-0.8B 权重与相关版权归其原作者所有，**不在本许可证范围内**；
  本项目仅做板端量化与部署工程。
- Rockchip `rknn-llm` / `rknn-toolkit2` 遵循其官方许可，不在本许可证范围内。
- `deploy/native/stb_image.h` 为公有领域（Public Domain）单头文件库。
