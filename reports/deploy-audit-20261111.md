# 部署环境只读审查报告

- **审查对象**：`/home/leox/RKNN-MODEL`（约 11 GB，**已是 git 仓库**）
- **审查日期**：2026-11-11 15:00–15:45（主机时钟）
- **执行人**：ui-fixer（teammate）
- **约束遵守**：全程只读。未 apt install、未写 `/data`、未 start/stop/restart/enable 设备服务、未克隆仓库、未读取任何凭据文件。
- **设备可达性**：**可达**（`adb devices -l` → `usb:1-12.2 transport_id:1`）。所有设备侧命令均为 `cat` / `ls` / `md5sum` / `sha256sum` / `strings` / `stat` / `systemctl status|show|is-active|is-enabled` / `journalctl` 只读调用。
- **⚠️ 重要前提：审查期间仓库与设备正被并行 agent 改动。** 本次审查窗口内观察到 3 次状态跃迁（15:16 / 15:38 / 15:41），涉及 `deploy/native/rkllm-service`、`deploy/native/rkllm_service.cpp`、`deploy/rkllm-chat.service`、`deploy/ui/rkchat/*`、`deploy/qwen35/*`。**下文所有「实测」值均标注了快照时刻**；跨时刻不一致之处按「并行改动」处理，不计为文档缺陷。
- **基准快照**：`git HEAD = bc72a8e`（`fix: 修复语音播放崩溃与服务退出竞态，补充审查报告`，2026-11-11 14:17:37）。工作区有 16 项已修改、1 项未跟踪。

---

## 1. 环境事实表格（实际 vs 文档声称）

### 1.1 设备与运行时（快照 15:41）

| 项 | `deploy/README.md` 声称 | 实测（设备） | 结论 |
|---|---|---|---|
| 板卡 | Quectel PI M2 RK3576 Board（`:57`） | `/proc/device-tree/model` → `Quectel PI M2 RK3576 Board` | ✅ 一致 |
| CPU | （未声明） | 8 × Cortex-A53（`CPU part 0xd03`） | 记录 |
| 内核 | Linux 6.1.118-rt36（PREEMPT_RT）（`:58`） | `6.1.118-rt36 #264 SMP PREEMPT_RT Thu Oct 29 14:31:00 CST 2026 aarch64` | ✅ 一致 |
| 发行版 | Debian GNU/Linux 13 (trixie)（`:59`） | `PRETTY_NAME="Debian GNU/Linux 13 (trixie)"`，`DEBIAN_VERSION_FULL=13.6` | ✅ 一致 |
| Python | 3.13.5（`:60`） | `Python 3.13.5` | ✅ 一致 |
| NPU 驱动 | 0.9.8（`:61`） | `/sys/kernel/debug/rknpu/version` → `RKNPU driver: v0.9.8` | ✅ 一致 |
| rkllm runtime | 1.3.1（`:62`） | `librkllmrt.so` strings 含 `1.3.1`；启动日志 `rkllm-runtime version: 1.3.1, rknpu driver version: 0.9.8` | ✅ 一致 |
| rknn runtime | 2.3.2（`:63`） | `librknnrt version: 2.3.2 (429f97ae6b@2025-04-09T09:09:27)` | ✅ 一致 |
| 内存 | 3.9 GB（`:64`） | `MemTotal: 3994028 kB` ≈ 3.81 GiB | ✅ 一致 |
| 存储 `/` | 42 GB（`:65`） | `/dev/root 42G，7.9G 已用（20%）` | ✅ 一致 |
| 存储 `/userdata` | 8 GB（`:65`） | `/dev/mmcblk0p12 8.0G，4.4G 已用（55%）`（审查期间从 47% 升到 55%，并行部署所致） | ✅ 容量一致，占用已变 |
| 显示 | 1280x800 全屏（`:3`/§4） | UI 实跑 `--fullscreen 1280x800+0+0`（PID 3879483，user `w`） | ✅ 一致 |

### 1.2 运行时库（README §2 的「逐字节一致」声明）

| 文件 | README 声称 md5（`:69-74`） | 设备 `/data/rkllm_qwen35/lib/` | 仓库 `rknn-llm/` | 结论 |
|---|---|---|---|---|
| `librkllmrt.so` | `2a4867ece83de3735a37f6459811eb92` | 同（7674816 B） | 同 | ✅ **三方一致** |
| `librknnrt.so` | `a37ee1d5d664c79836bf6e35b7ef6289` | 同（7726232 B） | 同 | ✅ **三方一致** |

补充：设备 `/usr/lib/librknnrt.so` md5 亦为 `a37ee1d5…`；`/usr/lib/librkllmrt.so` 不存在（符合 README §5「只从 `LD_LIBRARY_PATH` 取」的设计）。

### 1.3 模型文件（README §3）

| 文件 | README 声称大小（`:`）/ md5（`:84-87`） | 设备实测大小 | 设备实测 md5 | 结论 |
|---|---|---|---|---|
| `Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm`（线上） | 1068 MB / `d08e1bc0b3ecd7a5fd52b4ad5f8906fe` | 1068081932 B | `d08e1bc0…06fe` | ✅ 一致 |
| `Qwen3.5-0.8B-MIT_vision_rk3576.rknn`（线上） | 214 MB / `25dc659db250932fd284d9781f935399` | 223784945 B | `25dc659d…5399` | ✅ 一致 |
| `Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm`（回滚） | 1126 MB / `e65c820dbc4e45f57a042a441851d157` | 1125915612 B | `e65c820d…d157` | ✅ 一致 |
| `Qwen3.5-0.8B_vision_rk3576.rknn`（回滚） | 225 MB / `880af90aab7578b9c0e546b8d2bbf441` | 225298097 B | `880af90a…f441` | ✅ 一致 |

本机 `out/` 产物亦核对：`out/qwen3.5-0.8b-mit_w4a16_rk3576.rkllm` = `00380f51d1de79ec7b6bf9f775850a70`（README `:97` 声称的「`optimization_level=0` 乱码不可用版本」✅）；`out/qwen3.5_vision_rk3576.rknn` = `25dc659d…`（与线上一致 ✅）。

### 1.4 README §1 目录结构树 vs 实际文件（存在性逐项核对）

| 树中条目（README 行号） | 实际 | 大小 |
|---|---|---|
| `README.md`（`:16`） | ✅ 存在 | 14236 B |
| `DEPLOY.sh`（`:17`） | ✅ | 8784 B |
| `rkllm-chat.service`（`:18`） | ✅ | 847 B |
| `ui/rkchat/rkchat.py`（`:21`） | ✅ | 66203 B |
| `ui/rkchat/desktop_session.py`（`:22`） | ✅ | 5704 B |
| `ui/rkchat/client.py`（`:23`） | ✅ | 13222 B |
| `ui/rkchat/camera.py`（`:24`） | ✅ | 13669 B |
| `ui/rkchat/voice.py`（`:25`） | ✅ | 24800 B |
| `ui/rkchat/vkbd.py`（`:26`） | ✅ | 23314 B |
| `ui/rkchat/style.css`（`:27`） | ✅ | 12980 B |
| `ui/rkchat/run.sh`（`:28`） | ✅ | 975 B |
| `native/rkllm_service.cpp`（`:31`） | ✅ | 41346 B（审查中，并行 agent 在改） |
| `native/rkllm_service.h`（`:32`） | ✅ | 1400 B |
| `native/rkllm.h`（`:33`） | ✅ | 23464 B |
| `native/rknn_api.h`（`:34`） | ✅ | 35707 B |
| `native/stb_image.h`（`:35`） | ✅ | 263611 B |
| `native/rkllm-service`（`:36`） | ✅ | 151136→143208 B（并行重建中，见 §2.8） |
| `native/rkllm-service.bak`（`:37`） | ✅ | 151136 B |
| `qwen35/chat.sh`（`:40`） | ✅ | 1789 B |
| `qwen35/run.sh`（`:41`） | ✅ | 1438 B |
| `qwen35/bench.sh`（`:42`） | ✅ | 2170 B |
| `qwen35/stress.sh`（`:43`） | ✅ | 1414 B |
| `qwen35/chat.py`（`:44`） | ✅ | 28491 B |

**结论：树中 23 个条目全部存在于文档声明的路径。** 但树**漏列**了 3 项已入库/已存在内容，且含 2 个空目录（见 §2.5）。

### 1.5 部署路径映射自洽性

| README 声明 | DEPLOY.sh 实际 | 自洽 |
|---|---|---|
| `ui/rkchat/` → `/data/rkllm-chat/ui/rkchat/`（`:20`） | `DEPLOY.sh:119-124` 推 8 个文件 + 清 `__pycache__` | ✅ |
| `native/` → `/data/rkllm-chat/native/`（`:30`） | `:128-135` 推 5 个源文件；二进制仅 `--build-native` 时 `.new`+`mv` 原子替换 | ✅ |
| `qwen35/` → `/data/rkllm_qwen35/`（`:39`） | `:146-152` 推 5 个脚本 + `chmod +x` | ✅ |
| `rkllm-chat.service` → `/etc/systemd/system/`（§5） | `:157-162` push + `daemon-reload` | ✅ |
| （README 未提）`voice-vision/talk.py` → `/data/voice-vision/talk.py` | `:140-143` 条件推送 | ⚠️ 文档未记录（§2.5） |

`DEPLOY.sh --dry-run` 实跑通过，目标设备正确识别为 `transport_id=1`，计划动作与文档描述一致。

### 1.6 git / 仓库现状

| 项 | 事实 |
|---|---|
| git | **已是 git 仓库**（任务描述「非 git 仓库」已过期）；`HEAD = bc72a8e`，父 `a8cbe2b`、`272fbb7` |
| 跟踪文件 | 172 个；`.gitignore` 排除 `Qwen3.5-0.8B-MIT/`(1.7G)、`rknn-llm/`(2.6G)、`out/*.rkllm|*.rknn`(1.2G)、`.venv310/`(5.3G)、`wheelhouse/`(173M) |
| 目录体积 | 总计 11 G：`.venv310` 5.3G、`rknn-llm` 2.6G、`Qwen3.5-0.8B-MIT` 1.7G、`out` 1.2G、`wheelhouse` 173M、`reports` 13M、`logs` 1.7M、`deploy` 1.2M |
| 工作区 | 审查结束时 16 项 `M`（并行 agent 改动）+ 1 项未跟踪 `reports/doc-screenshots-20261111/` |
| 凭据扫描 | 本次未主动读取任何凭据文件；`.gitignore` 已显式屏蔽 `.env`/`.netrc`/`*.pem`/`.ssh/` 等 |

### 1.7 归档 vs 设备一致性（快照 15:41:47）

| 组件 | 结论 |
|---|---|
| UI 源码 8 个文件 | **8/8 md5 一致**（HEAD 与设备一致；工作区有并行未部署改动） |
| native 源文件 5 个 | **5/5 一致**（`rkllm_service.cpp` = `aed1a334…`，工作区 = 设备） |
| qwen35 脚本 5 个 | `bench.sh` 一致；`chat.py`/`chat.sh`/`run.sh`/`stress.sh` 工作区已改但设备仍为 HEAD 版（未部署） |
| `voice-vision/talk.py` | 工作区 = 设备（`d8f1a3af…`）；HEAD 更旧（`d07748a8…`） |
| `rkllm-chat.service` | **工作区 = 设备 = `5c0d411a…`**（15:41 已同步，含 `--allow-uid 1001`/`UMask=0027`/`TimeoutStopSec=10`/`KillMode=mixed`） |
| `native/rkllm-service` 二进制 | 工作区 = 设备 = `4b42a2f7…`（143208 B，15:38 重建产物，含 `--allow-uid`/`SO_PEERCRED`） |

**关键结论**：部署归档与设备当前状态**高度一致**，`deploy/` 可作为可信还原源。审查期间的多处「不一致」均为并行 agent 的**未部署改动**，非缺陷。

---

## 2. 不一致 / 过时项清单（带 文件:行号）

### 2.1 README §7「方式二：手动」编译路径错误（必然 link 失败）

- **文件**：`deploy/README.md:284-285`
  ```bash
  -L../rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64 \
  -L../rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64 \
  ```
- **实测**：命令前提是 `cd deploy/native`（`:279`）。此时 `../rknn-llm` = `deploy/rknn-llm`，**不存在**：
  ```
  $ cd deploy/native && ls ../rknn-llm          → 没有那个文件或目录
  $ ls ../../rknn-llm/…/librkllmrt.so            → 存在（7674816 B）
  ```
- **应为** `-L../../rknn-llm/...`（两级）。
- **对照**：`deploy/DEPLOY.sh:103-104` 用 `ROOT=$(cd "$HERE/.." && pwd)`（`:25`）拼绝对路径，**正确**。即只有 README 手抄方式写错。

### 2.2 README 树注释「推理服务开机自启」与设备 `disabled` 矛盾

- **文件**：`deploy/README.md:18`
  ```
  ├── rkllm-chat.service        # systemd unit（推理服务开机自启）
  ```
- **实测（15:41）**：`systemctl is-enabled rkllm-chat` → **`disabled`**；`UnitFilePreset=enabled`（即预设应启用，被显式 disable 过）。`is-active` → `active`。
- **判读**：README 描述的是「跑过 `DEPLOY.sh` 之后的目标状态」——`deploy/DEPLOY.sh:169` 确实执行 `systemctl enable rkllm-chat`。但**当前设备**处于不自启状态，断电重启后服务与 UI 都不会自动拉起。文档未区分「当前状态」与「部署后状态」。

### 2.3 `rkllm-service.bak` 不是「上一版」，而是「两代之前」

- **文件**：`deploy/README.md:37`
  ```
  │   └── rkllm-service.bak     # 上一版二进制（回滚用）
  ```
- **实测三方对照**：

| 二进制 | size | `strings` 关键特性 | BUILD ID |
|---|---|---|---|
| `rkllm-service.bak` | 151136 B（mtime 2026-04-18 01:24） | ❌ 无 `kv_reset`/`kv_before`/`kv_after`/`embedding_hash`（即**无**视觉 KV 刷新诊断协议） | `5092ca91…` |
| `/userdata/backup-camera-freshness-…/native/rkllm-service`（真正的上一版） | — | — | sha256 `542c9fd5…` |
| 当前 `rkllm-service`（设备运行中） | 143208 B | ✅ 4 项全有；另有 `--allow-uid`/`SO_PEERCRED` | `7d243bdb…`（15:38 重建） |

- **判读**：用 `.bak` 回滚会**同时丢掉摄像头新鲜度修复**。真正可回滚的上一版在 `/userdata/backup-camera-freshness-20260421-175037/native/rkllm-service`。README 表述不准确。

### 2.4 `rkllm-service.bak` 依赖 RUNPATH，当前版不依赖（有意差异，未记录）

- **实测**：
  - `rkllm-service.bak`：`readelf -d` → `RUNPATH /data/rkllm_qwen35/lib`
  - 当前 `rkllm-service`：**无 RUNPATH**，完全依赖 unit 的 `LD_LIBRARY_PATH`
- **影响**：脱离 systemd 直接跑当前二进制会 `librkllmrt.so: not found`（`.bak` 不会）。属有意设计（`README.md:288-289` 提到「运行时通过 `LD_LIBRARY_PATH` 找到前两个」），但没说明 `.bak` 行为不同。

### 2.5 README 目录结构树漏列 + 僵尸空目录

- **文件**：`deploy/README.md:15-45`
- **树中未列但实际存在**：
  - `deploy/ui/rkchat/STYLE_CHANGES.md`（9967 B，**git 已跟踪**）
  - `deploy/voice-vision/`（`README.md` 1830 B + `talk.py`；`:7` 正文有提及，树里没有）
  - `DEPLOY.sh:140-143` 还会推送 `voice-vision/talk.py` → `/data/voice-vision/talk.py`，**文档完全未记录这条部署路径**
- **空目录（`git ls-files` 计数 = 0）**：`deploy/audio/`、`deploy/rkllm-chat/` —— 残留僵尸目录，建议删除。

### 2.6 README §5 摘录的 unit 是节选，会误导手抄

- **文件**：`deploy/README.md:174-184` vs `deploy/rkllm-chat.service`
- **摘录缺失**：`Environment=LLM_DIR=…`、`RestartSec=3`、`RuntimeDirectoryMode=0775`、`UMask=…`、`[Install] WantedBy=multi-user.target`。
- **当前实际**（15:41 已更新）：`UMask=0027`、`TimeoutStopSec=10`、`KillMode=mixed`、`--allow-uid 1001` —— 摘录落后更多。
- **影响**：照摘录手抄会得到**权限语义完全不同**的 unit（谁能连 socket 会变），且丢掉 `[Install]` 段导致 `enable` 无效。

### 2.7 README §8「max_context_len 上限 16384」未区分 runtime 上限与模型内嵌上限

- **文件**：`deploy/README.md:310`
  ```
  | 上下文仅 4096 | 博客评测用 32768。板端 `max_context_len` 上限 16384，当前 4096。 |
  ```
- **实测**：模型转换时已固定 4096 —— `logs/02_rkllm.log:9` → `INFO: Setting max_context_limit to 4096`；设备启动日志 → `rkllm-toolkit version: 1.3.1, max_context_limit: 4096`。
- **判读**：「16384」是 runtime/driver 侧能力上限，**当前 `.rkllm` 文件本身被编译成 4096**，改 `--ctx` 无法突破。易被误读为「调参数就能到 16384」。

### 2.8 README 二进制体积/校验信息已随并行重建失效

- **文件**：`deploy/README.md:36`
- **实测**：审查期间 `deploy/native/rkllm-service` 三次变化：
  | 时刻 | size | sha256 | Build ID |
  |---|---|---|---|
  | HEAD (`bc72a8e`) | 142984 B | `b23b6076…` | `c0f2851c…` |
  | 15:27（并行件） | 143336 B | `c2e6c6b6…` | `8511795d…` |
  | 15:38（当前） | 143208 B | `514ec7de…` | `7d243bdb…` |
- **判读**：README 未记录二进制 sha256（不算硬伤），但**任何文档化的校验值都会随并行重建失效**。建议改为「以 `DEPLOY.sh --build-native` 产物为准，校验值见构建报告」。

### 2.9 `deploy/DEPLOY.sh --help` 截断失效

- **文件**：`deploy/DEPLOY.sh:37`
  ```bash
  -h|--help)      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  ```
- **实测**：`bash deploy/DEPLOY.sh --help` 输出 24 行，末尾是
  ```
  set -euo pipefail

  HERE=$(cd "$(dirname "$0")" && pwd)
  ROOT=$(cd "$HERE/.." && pwd)
  ```
- **判读**：注释块实际到 `:21`，`2,25` 越界，把代码行也打出来。应改成 `'2,21p'` 或用哨兵行。

### 2.10 README §6 前置条件「板端已装 sherpa-onnx」与实际形态不符

- **文件**：`deploy/README.md:207`
  ```
  - 板端已装 `python3-gi`、GTK3、sherpa-onnx
  ```
- **实测**：
  - `python3 -c "import sherpa_onnx"` → **`ModuleNotFoundError: No module named 'sherpa_onnx'`**（未做系统级安装）
  - 但 `sys.path.insert(0,"/data/voice-vision/py"); import sherpa_onnx` → **OK**
  - 即 sherpa-onnx 是**vendored 在 `/data/voice-vision/py/sherpa_onnx` + `sherpa_onnx.libs`**，不是 pip/apt 装的
- **判读**：`talk.py:41-42` 自己 `sys.path.insert(PY_DIR)`，所以运行正常；但「已装 sherpa-onnx」会误导重建者去 `pip install`。应改成「sherpa-onnx 已 vendored 在 `/data/voice-vision/py/`」。
- 附：`gi` 3.50.0 ✅、`numpy` 2.2.4 ✅ 均在位。

### 2.11 文档化的模型大小单位混用（MB / MiB）

- **文件**：`deploy/README.md:5`（`214 MiB`）、`:84-87`（`1068 MB` / `214 MB` / `1126 MB` / `225 MB`）
- **实测**：线上 LLM = 1068081932 B = 1018.6 MiB = 1068.1 MB；线上 vision = 223784945 B = 213.4 MiB = 223.8 MB。
- **判读**：数值都对（MB=10⁶、MiB=2²⁰），但同段内混用，按 MiB 找「214 MiB 的 vision」会与表里「214 MB」矛盾。

### 2.12 `model-*.sha256` 第 3 行是裸 PID，破坏标准校验格式

- **文件**：`reports/non-model-fixes/model-before.sha256:3` 与 `model-after.sha256:3`、`reports/camera-freshness/model-*.sha256`（后者仅 2 行，无此问题）
  ```
  4603
  ```
- **实测**：该行是推理服务 PID（用于证明「未重启服务」）。`sha256sum -c` 会报「格式不正确」。前两行模型哈希不受影响。

### 2.13 设备时钟未同步（跨文件时间比对陷阱）

- **实测**：设备 `Wed 2026-04-22 23:19 CST`，`System clock synchronized: no`，`NTP service: active`（未生效）；主机 `2026-11-11`。设备侧文件 mtime 多落在 2026 年 4 月。
- **影响**：跨设备/主机比对 mtime 会误判「谁更新」。本次所有「新旧」判断均以 sha256/md5 + 特性标记为准，不依赖 mtime。

### 2.14 `voice-vision/README.md` 本地与设备已分叉

- **实测**：本地 `42a969694422ad57639679cd81d23742` vs 设备 `c541acfcf1905cf4045dcd7379ba473d`，同为 5943 B。`DEPLOY.sh` 不同步任何 README（`grep README deploy/DEPLOY.sh` 无命中），属设备侧独立更新的文档。

### 2.15 设备 `/data/rkllm-chat/ui/` 下残留多份历史备份目录

- **实测**：`rkchat.bak-1793867566`、`rkchat.bak-css-1793870438`、`rkchat.bak-final-1776658097`、`rkchat.bak-speak-1776684243`
- **判读**：`DEPLOY.sh` 不清理这些；每次手工备份累积。不阻塞部署，但会误导「当前生效的是哪份」。

---

## 3. 风险分级

### P0（阻断交付 / 数据安全）

**审查时刻无 P0。** 模型文件未被改动（§5）、服务正常运行、无凭据泄漏。

> 但在审期间观察到一次**权限模型变更**（15:41 已生效），若未经验证即交付，可能升至 P1 —— 见 P1-5。

### P1（影响可复现性 / 会误导重建者）

| # | 问题 | 文件:行 | 说明 |
|---|---|---|---|
| **P1-1** | README 手动编译 `-L../rknn-llm` 路径错误，照抄必然 link 失败 | `deploy/README.md:284-285` | 实测 `../rknn-llm` 不存在，应为 `../../rknn-llm`。`DEPLOY.sh:103-104` 正确 |
| **P1-2** | `systemctl is-enabled` = `disabled`，断电重启后服务与 UI 均不自动拉起，而 README 声称「开机自启」 | `deploy/README.md:18` + 设备实测 | 需跑一次 `DEPLOY.sh`（`:169` 会 `enable`）或手工 enable。**当前设备不自启** |
| **P1-3** | `rkllm-service.bak` 语义错误：不是「上一版」而是两代前（缺全部视觉 KV 刷新诊断串） | `deploy/README.md:37` | 用它回滚会同时回退摄像头修复。真正上一版：`/userdata/backup-camera-freshness-20260421-175037/native/rkllm-service`（`542c9fd5…`） |
| **P1-4** | README §5 unit 摘录缺 `UMask`/`RuntimeDirectoryMode`/`[Install]`/`LLM_DIR`/`RestartSec`，手抄会导致 socket 权限与 enable 行为不同 | `deploy/README.md:174-184` | 现 unit 已演进到 `--allow-uid 1001` + `UMask=0027` + `TimeoutStopSec=10` + `KillMode=mixed`，摘录滞后更多 |
| **P1-5** | socket 权限模型变更（15:41）后 `srw-rw----  root:w`，**其他用户在 `--allow-uid`/`SO_PEERCRED` 逻辑未被独立验证前若服务端行为有偏差，UI 可能连不上** | `deploy/rkllm-chat.service:17,26,30,31` + 设备实测 | 变更本身是安全改进（防任意用户 `{"cmd":"exit"}` 抖动服务）；但本轮**未做连接回归**，需一次 UI→socket 实连验证后才能算闭环 |
| **P1-6** | SIGTERM 历史上无法唤醒 `accept()`，`systemctl restart` 吃满 `TimeoutStopUSec=20s` 后被 SIGKILL（日志有 1 次实证） | `journalctl -u rkllm-chat` → `Apr 21 17:51:40 State 'stop-sigterm' timed out. Killing.` / `Failed with result 'timeout'` | **当前工作区代码已用 self-pipe + `poll()` 修复**（`deploy/native/rkllm_service.cpp:737` 声明 self-pipe、`:742` 信号处理里 write、`:1060` 主循环 `poll(pfds, 2, -1)`；unit 加 `TimeoutStopSec=10`/`KillMode=mixed`）。但设备在审期间重启了 2 次且 `NRestarts=0`，**尚未在设备上复现验证「restart 不再等 20s」** |

### P2（文档准确性 / 可维护性）

| # | 问题 | 文件:行 |
|---|---|---|
| P2-1 | 「max_context_len 上限 16384」未说明模型已固定 4096，改 `--ctx` 无效 | `deploy/README.md:310`（对照 `logs/02_rkllm.log:9`） |
| P2-2 | §6 前置条件「板端已装 sherpa-onnx」不符：实为 vendored 在 `/data/voice-vision/py/`，`import sherpa_onnx` 直接失败 | `deploy/README.md:207` |
| P2-3 | 结构树漏列 `STYLE_CHANGES.md`、`voice-vision/`；未记录 `DEPLOY.sh:140-143` 的 `/data/voice-vision/talk.py` 推送路径；存在空目录 `deploy/audio/`、`deploy/rkllm-chat/` | `deploy/README.md:15-45`；`DEPLOY.sh:140-143` |
| P2-4 | `--help` 用 `sed -n '2,25p'` 越界，多打印 4 行代码 | `deploy/DEPLOY.sh:37` |
| P2-5 | MB / MiB 混用 | `deploy/README.md:5, 84-87` |
| P2-6 | `model-*.sha256` 第 3 行为裸 PID，破坏 `sha256sum -c` | `reports/non-model-fixes/model-*.sha256:3` |
| P2-7 | `.bak` 有 `RUNPATH`、当前版无，行为差异未记录 | `deploy/native/rkllm-service.bak` vs `rkllm-service` |
| P2-8 | 内存余量偏低：`available 1489 MiB`、`free 263 MiB`、Swap 0；服务 RSS 1.09 GiB + UI RSS 479 MiB | 设备实测（15:19） |
| P2-9 | 设备时钟未同步（`System clock synchronized: no`），跨端 mtime 比对易误判 | 设备实测 |
| P2-10 | `voice-vision/README.md` 本地/设备 md5 已分叉 | 本地 vs `/data/voice-vision/README.md` |
| P2-11 | `/data/rkllm-chat/ui/` 残留 4 份 `rkchat.bak-*` 历史目录 | 设备实测 |
| P2-12 | 仓库未推送（`origin` 无 push 权限，403）—— 来自 team leader 已核实信息，本次未重复验证 | git 配置 |
| P2-13 | `/userdata` 占用审查期间从 47% → 55%（多次部署叠加），备份目录已 4 份 | 设备实测 |

---

## 4. 已完成工作项汇总（4 个 MANIFEST + 3 份 REPORT）

| 轮次 | 目录（均只有 MANIFEST.md 1 个文件） | 结论 | 关键指标 | 备份 |
|---|---|---|---|---|
| ① 系统键盘 | `.dev-flow/system-keyboard/` + `logs/keyboard-fix/RESULT.md` | **PASS** | 7/7 单测；首次触摸弹 GNOME 48.7 OSK；真实按键输入 hello、IBus libpinyin 上屏「你好」；输入框下沿 522 < 键盘顶部 533 无遮挡 | `/userdata/backup-system-keyboard-20260420-232815` |
| ② 综合运行时 | `.dev-flow/comprehensive-runtime/` + `reports/full-test/20261109-164746/REPORT.md` | **部分通过**：链路 COMPLETED，核心功能 PASS；**回答质量/声学识别 FAIL** | 55 条记录（44 主 + 11 补）；纯文本 TTFT 中位 520 ms，含图 3645 ms；声学回环 CER 4/13 ≈ 30.8%；UI RSS 稳定 565.4 MiB ×5 | 无（只测不改） |
| ③ 非模型工程修复 | `.dev-flow/non-model-fixes/` + `reports/non-model-fixes/REPORT.md` | **PASS** | 修复前复现 11 项失败；本地 35/35、设备 35/35 单测；真机 16/16；连续 20 次重连仅剩 1 接收线程；USB 采集削波 2.18% → 0% | `/userdata/backup-non-model-fixes-20260421-005845` |
| ④ 摄像头新鲜度 | `.dev-flow/camera-freshness/` + `reports/camera-freshness/REPORT.md` | **PASS** | 本地 45/45、设备 45/45；真机 15/15；真实内核触摸关闭 PASS；红→蓝→红 embedding `7dbdfd51…`/`1ecd2442…`/`7dbdfd51…` 可复现 | `/userdata/backup-camera-freshness-20260421-175037` |

**四轮共同守住的约束**（MANIFEST 声明 + 实测）：
- 不换模型、不改 max-new/context/量化、不开 thinking；
- 每轮回滚前均做备份；
- LLM / Vision 权重 SHA256 前后一致；
- 前三轮「未 commit/push」（用户未请求）。

**遗留风险（来自各 MANIFEST 的 Open Items）**：
- **模型能力**：数学 `12×13→154`、`3个苹果+2→15`；开放问题无关拒答；名字记忆失败；格式不遵循（要求 `pong` 得 `Pong.`）；长回答中英混杂/跑题。**不在工程修复范围**，属产品决策项。
- **声学 ASR**：USB 预热后 3 句仍有 2/41 字符错误（小样本，未声称修复）。
- **未覆盖**：24 小时老化、断电重启恢复、拔网线、真人免提/真人语音打断、画面语义完全人工核对。
- **GUI 自动化受限**：mutter/Xwayland 拦截 Xtst 伪造事件，改用内核 `uinput`（临时设备在 `finally` 销毁）。

---

## 5. 模型完整性核对（模型未被改动）

| 校验对 | 结果 |
|---|---|
| `reports/non-model-fixes/model-before.sha256` vs `model-after.sha256` | `diff` 无输出 → **IDENTICAL** |
| `reports/camera-freshness/model-before.sha256` vs `model-after.sha256` | `diff` 无输出 → **IDENTICAL** |
| 设备当前实测（2026-11-11 复核，快照 15:19/15:41） | LLM `883a67e78759699e76517097d3d78c0f738d6e25792dde53ee94818017487d07`、vision `02337a56ec305f86e5c78b52f55dc5c1588bb9d3aa5bf0a8ec6b6ea330663be3` |

**结论**：4 月记录值 = 11 月实测值 → **模型自始至终未被修改**，跨月零漂移。两份 before/after 文件的 2 行模型哈希完全一致（第 3 行 PID 见 P2-6，不影响结论）。

---

## 6. 遗留待办（按优先级）

1. **【P1】修 README §7 手动编译路径**：`deploy/README.md:284-285` 的 `-L../rknn-llm` → `-L../../rknn-llm`（2 处）。
2. **【P1】明确「开机自启」状态**：`systemctl enable rkllm-chat`（本次只读未执行），或在 README `:18` 注明「需跑一次 DEPLOY.sh 才会 enable」。
3. **【P1】修正 `.bak` 定位描述**：`deploy/README.md:37` 改为「摄像头修复前的基线版」，并补一句真正上一版在 `/userdata/backup-camera-freshness-20260421-175037/native/`。
4. **【P1】验证 socket 权限变更后的连通性**：新 unit 的 `UMask=0027` + `--allow-uid 1001` + `SO_PEERCRED` 需一次「桌面用户 w 的 UI 实连 socket 并完成一轮 ask」回归，确认非 root 桌面用户不被误拒。
5. **【P1】验证 SIGTERM 修复在设备上生效**：跑一次 `systemctl restart rkllm-chat` 并确认耗时 < 10 s（不再吃满 20 s 超时被 SIGKILL）。
6. **【P1】README §5 改为「完整 unit 见 `deploy/rkllm-chat.service`」**，并说明 `UMask`/`RuntimeDirectoryMode`/`--allow-uid` 的作用，避免手抄。
7. **【P2】README §6 前置条件改写**：「sherpa-onnx 已 vendored 在 `/data/voice-vision/py/`，无需 pip 安装」。
8. **【P2】README §8 补「模型内嵌 max_context_limit=4096，改 --ctx 无效」**。
9. **【P2】结构树补 `STYLE_CHANGES.md`、`voice-vision/`、`/data/voice-vision/talk.py` 推送路径；删除空目录 `deploy/audio/`、`deploy/rkllm-chat/`**。
10. **【P2】修 `deploy/DEPLOY.sh:37`** 的 `sed -n '2,25p'` → `'2,21p'`。
11. **【P2】统一 MB/MiB；`model-*.sha256` 第 3 行加 `# pid 4603` 前缀或移到 `.pid` 文件**。
12. **【P2】清理设备 `/data/rkllm-chat/ui/rkchat.bak-*` 4 份历史目录；同步 `voice-vision/README.md`**。
13. **【P2】设备内存余量仅 1.49 GiB available、无 Swap**，建议评估 zram 或降低 UI 缓存水位。
14. **【P2】设备时钟未同步**：`timedatectl set-ntp true` 并排查 NTP 源。
15. **【P2】解决 push 权限**（`origin` 403），或改用有权限账号 / fork + PR。
16. **【产品决策】模型质量问题（数学/拒答/记忆/格式）与 ASR 错字**：不在工程修复范围，需回滚原版模型或接受现状，应单独立项。

---

## 附录 A：本次执行的只读命令清单（节选）

```
adb devices -l
adb -t 1 shell 'uname -a; cat /etc/os-release; python3 -V'
adb -t 1 shell 'cat /sys/kernel/debug/rknpu/version; free -m; df -h / /userdata'
adb -t 1 shell 'md5sum /data/rkllm_qwen35/lib/*.so'
adb -t 1 shell 'sha256sum /data/rkllm_qwen35/Qwen3.5-0.8B-MIT_*.{rkllm,rknn}'
adb -t 1 shell 'md5sum /data/rkllm_qwen35/*.rkllm *.rknn'
adb -t 1 shell 'systemctl is-active/is-enabled/status/show rkllm-chat'
adb -t 1 shell 'journalctl -u rkllm-chat --no-pager'
adb -t 1 shell 'cat /etc/systemd/system/rkllm-chat.service'
adb -t 1 shell 'sha256sum /proc/<pid>/exe; ls -l /run/rkllm-chat.sock'
adb -t 1 shell 'stat /data/rkllm-chat/native/rkllm-service'
md5sum 逐文件比对 deploy/* ↔ 设备（8 UI + 5 native + 5 qwen35 + talk.py + unit）
file / stat / sha256sum / strings / readelf -n / readelf -d  deploy/native/rkllm-service{,.bak}
bash deploy/DEPLOY.sh --dry-run          # 只打印计划，未碰设备
bash deploy/DEPLOY.sh --help             # 验证 P2-4
diff <(git show HEAD:...) /tmp/dev-*      # HEAD vs 工作区 vs 设备三方比对
```

**未执行（遵守硬约束）**：任何 `apt install`、任何对 `/data` 或 `/userdata` 的写操作、任何 `systemctl start/stop/restart/enable`、任何 `git clone`、任何凭据文件读取、任何 adb push。

## 附录 B：设备运行时快照（2026-11-11 15:41，主机时钟）

```
host clock : 2026-11-11 15:41 CST（设备时钟 2026-04-22 23:19，未同步）
kernel     : 6.1.118-rt36 #264 SMP PREEMPT_RT Thu Oct 29 14:31:00 CST 2026 aarch64
cpu        : 8 × Cortex-A53 (0xd03)，online 0-7
os         : Debian GNU/Linux 13 (trixie) / DEBIAN_VERSION_FULL=13.6
python     : 3.13.5；gi 3.50.0；numpy 2.2.4；sherpa-onnx 仅 vendored(/data/voice-vision/py)
npu        : RKNPU driver v0.9.8；load Core0 0% Core1 0%
mem        : 3994028 kB total / 2410 MiB used / 263 MiB free / 1489 MiB available / Swap 0
storage    : / /dev/root 42G 7.9G(20%)；/userdata /dev/mmcblk0p12 8.0G 4.4G(55%)
/data      : rkllm-chat 2.3M；rkllm_qwen35 2.5G；voice-vision 664M
service    : active(running)；is-enabled = disabled；NRestarts=0；RSS ≈ 1.09 GiB
             执行的命令行含 --ctx 4096 --max-new 256 --cores 2 --allow-uid 1001
ui         : PID 3879483 user w，rkchat.py --fullscreen 1280x800+0+0，RSS ≈ 479 MiB
sock       : /run/rkllm-chat.sock  srw-rw----  root:w
thermal    : 49.9 / 51.8 °C
uptime     : 2 days 5:01，load 2.08/2.30/2.34
backups    : /userdata/backup-{qwen35-20260419-102350, system-keyboard-20260420-232815,
             non-model-fixes-20260421-005845, camera-freshness-20260421-175037}
linux bin  : /data/rkllm-chat/native/rkllm-service 143208 B sha256 4b42a2f7…162980（审期间第 3 次重建）
```

## 附录 C：判定口径说明

1. **「一致」判据**：优先 md5/sha256 全等；无法取哈希时用 size + `strings` 特性标记；**不依赖 mtime**（设备时钟未同步）。
2. **并行改动隔离**：审查窗口内工作区/设备分别取快照，凡是「工作区已改、设备未部署」一律标为**未部署改动**，不计文档缺陷；凡「设备已部署、工作区已同步」才视为当前事实。
3. **未验证即不宣称**：`--allow-uid`/`SO_PEERCRED` 连接回归、`KillMode=mixed` 停止耗时，本轮**只读**无法验证，故列 P1 待办而非「已修复」。
