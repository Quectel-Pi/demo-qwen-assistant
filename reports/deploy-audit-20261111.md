# 部署环境只读审查报告

- **审查对象**：`/home/leox/RKNN-MODEL`（约 11 GB）
- **审查日期**：2026-11-11（主机时钟）
- **执行人**：repo-scout（teammate）
- **约束**：全程只读。未 apt install、未写 `/data`、未 start/stop 设备服务、未克隆仓库、未读取任何凭据文件。
- **设备可达性**：**可达**。`adb devices -l` 返回 `transport_id:1`，`/data/rkllm-chat` 存在，`rkllm-chat.service` active。所有设备侧命令均为 `cat` / `ls` / `md5sum` / `sha256sum` / `systemctl status` / `journalctl` 只读调用。
- **重要前提**：主机时钟为 2026-11-11，设备时钟为 2026-04-22（`System clock synchronized: no`，NTP 服务 active 但未同步）。设备侧文件 mtime 均落在 2026 年 4 月，本文中「设备日期」与「仓库目录命名日期」存在跨年偏移，属已知现象，不影响内容比对。

---

## 1. 环境事实表格（实际 vs 文档声称）

### 1.1 设备与运行时

| 项 | `deploy/README.md` 声称 | 实测（设备） | 结论 |
|---|---|---|---|
| 板卡 | Quectel PI M2 RK3576 | RK3576 / Cortex-A53 ×8 | ✅ 一致 |
| 内核 | Linux 6.1.118-rt36（PREEMPT_RT） | `6.1.118-rt36 #264 SMP PREEMPT_RT Thu Oct 29 14:31:00 CST 2026` | ✅ 一致 |
| 发行版 | Debian GNU/Linux 13 (trixie) | `PRETTY_NAME="Debian GNU/Linux 13 (trixie)"`，`DEBIAN_VERSION_FULL=13.6` | ✅ 一致 |
| Python | 3.13.5 | `Python 3.13.5` | ✅ 一致 |
| NPU 驱动 | 0.9.8 | `/sys/kernel/debug/rknpu/version` → `RKNPU driver: v0.9.8` | ✅ 一致 |
| rkllm runtime | 1.3.1 | `librkllmrt.so` strings → `1.3.1`；启动日志 `rkllm-runtime version: 1.3.1, rknpu driver version: 0.9.8, platform: RK3576` | ✅ 一致 |
| rknn runtime | 2.3.2 | `librknnrt.so` strings → `librknnrt version: 2.3.2 (429f97ae6b@2025-04-09T09:09:27)` | ✅ 一致 |
| 内存 | 3.9 GB | `MemTotal: 3994028 kB` ≈ 3.81 GiB | ✅ 一致（4 GB 标称） |
| 存储 `/` | 42 GB | `/dev/root 42G，已用 7.9G (20%)` | ✅ 一致 |
| 存储 `/userdata` | 8 GB | `/dev/mmcblk0p12 8.0G，已用 3.7G (47%)` | ✅ 一致 |
| 显示模式 | 1280x800 全屏 | `card0-DSI-1` enabled，modes 含 `1280x800` | ✅ 一致 |

### 1.2 库里版本（README §2 逐字节一致性声明）

| 文件 | README 声称 md5 | 设备实测 md5 | 仓库内文件 md5 | 结论 |
|---|---|---|---|---|
| `librkllmrt.so` | `2a4867ece83de3735a37f6459811eb92` | `2a4867ece83de3735a37f6459811eb92` | `2a4867ece83de3735a37f6459811eb92`（`rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/`） | ✅ 三方一致 |
| `librknnrt.so` | `a37ee1d5d664c79836bf6e35b7ef6289` | `a37ee1d5d664c79836bf6e35b7ef6289` | `a37ee1d5d664c79836bf6e35b7ef6289`（`rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64/`） | ✅ 三方一致 |

补充：设备 `/usr/lib/librknnrt.so`（7726232 B，2025-06-10）md5 也是 `a37ee1d5…`，与 `/data/rkllm_qwen35/lib/` 完全相同；`/usr/lib/librkllmrt.so` 不存在（符合预期，rkllm 只从 `LD_LIBRARY_PATH` 取）。

### 1.3 模型文件（README §3）

| 文件 | README 声称大小 | README 声称 md5 | 设备实测大小 | 设备实测 md5 | 结论 |
|---|---|---|---|---|---|
| `Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm`（线上） | 1068 MB | `d08e1bc0b3ecd7a5fd52b4ad5f8906fe` | 1068081932 B（1019 MiB / 1068.1 MB） | `d08e1bc0b3ecd7a5fd52b4ad5f8906fe` | ✅ 一致 |
| `Qwen3.5-0.8B-MIT_vision_rk3576.rknn`（线上） | 214 MB | `25dc659db250932fd284d9781f935399` | 223784945 B（213 MiB / 223.8 MB） | `25dc659db250932fd284d9781f935399` | ✅ 一致 |
| `Qwen3.5-0.8B_w4a16_g128_rk3576.rkllm`（回滚） | 1126 MB | `e65c820dbc4e45f57a042a441851d157` | 1125915612 B | `e65c820dbc4e45f57a042a441851d157` | ✅ 一致 |
| `Qwen3.5-0.8B_vision_rk3576.rknn`（回滚） | 225 MB | `880af90aab7578b9c0e546b8d2bbf441` | 225298097 B | `880af90aab7578b9c0e546b8d2bbf441` | ✅ 一致 |

`out/` 本机产物同样验证：`out/qwen3.5-0.8b-mit_w4a16_rk3576.rkllm` = `00380f51d1de79ec7b6bf9f775850a70`（README 声称的 `optimization_level=0` 不可用版本），`out/qwen3.5_vision_rk3576.rknn` = `25dc659db250932fd284d9781f935399`（与线上一致）。

### 1.4 systemd 服务

| 项 | README 声称 | 实测 | 结论 |
|---|---|---|---|
| unit 文件内容 | README §5 摘录片段 | `deploy/rkllm-chat.service` **与设备 `/etc/systemd/system/rkllm-chat.service` 逐字节相同**（diff 无输出） | ✅ 一致 |
| 运行状态 | 推理服务常驻 | `active (running)`，PID 1768410，`ActiveEnterTimestamp=Tue 2026-04-21 17:52:04 CST`，`NRestarts=0` | ✅ 一致 |
| 开机自启 | README 树注释「推理服务开机自启」 | **`systemctl is-enabled` → `disabled`**（`UnitFilePreset=enabled`） | ❌ **不一致**，见 §2-1 |
| Socket | `/run/rkllm-chat.sock` | `srw-rw-rw- 1 root root` 存在，服务监听 | ✅ 一致 |
| 服务进程 RSS | — | rkllm-service RSS ≈ 1.09 GiB（Peak 1.2G）；rkchat.py RSS ≈ 661 MiB | 记录用 |
| 内存水位 | — | `Mem: 3.8Gi total, 2.5Gi used, 27Mi free, 2.3Gi buff/cache, 1.3Gi available`，Swap 0 | ⚠️ free 极低，见 P2 |

### 1.5 部署归档 vs 设备（一致性核对）

**UI 源码**（`deploy/ui/rkchat/` → `/data/rkllm-chat/ui/rkchat/`）：8/8 文件 md5 全部 **OK**
`rkchat.py`、`camera.py`、`client.py`、`voice.py`、`vkbd.py`、`desktop_session.py`、`style.css`、`run.sh`

**native 目录**（`deploy/native/` → `/data/rkllm-chat/native/`）：sha256 全部 **OK**
| 文件 | sha256 | 结论 |
|---|---|---|
| `rkllm-service` | `48aa0ffe…14843bca` | ✅ 本地 = 设备 = 报告归档 = 重新编译产物 |
| `rkllm-service.bak` | `5318ecc0…7f1053bc` | ✅ 本地 = 设备 = `/userdata/backup-camera-freshness-…/native/rkllm-service.bak` |
| `rkllm_service.cpp` | `51338b2b…f6cbb22196` | ✅ 本地 = 设备 |

**qwen35 脚本**（`deploy/qwen35/` → `/data/rkllm_qwen35/`）：5/5 md5 **OK**
`chat.sh`、`run.sh`、`bench.sh`、`stress.sh`、`chat.py`

**voice-vision**：`deploy/voice-vision/talk.py` → `/data/voice-vision/talk.py` md5 **OK**；但 `README.md` **不一致**（本地 `42a969694422ad57639679cd81d23742`，设备 `c541acfcf1905cf4045dcd7379ba473d`，同为 5943 B）——属设备侧独立更新的文档，非部署脚本管理项，P2。

**关键结论**：部署归档与设备当前运行状态**高度一致**，`deploy/` 可作为可信还原源。

### 1.6 仓库 / git 现状

| 项 | 事实 |
|---|---|
| git | **已是 git 仓库**（原任务描述「非 git 仓库」已过期）；`HEAD = a8cbe2b feat: 迁移 Quectel Pi M2 Qwen3.5-0.8B 语音助手部署工程`，父提交 `272fbb7 Initial commit` |
| remote | `origin → https://github.com/Quectel-Pi/demo-qwen-assistant.git`；当前 gh 账号 **lenonis 无 push 权限（403）**，push 受阻（本条来自 team leader 已核实信息） |
| 工作区 | `git status --porcelain` 仅 2 项未跟踪：`reports/demo-qwen-assistant-a8cbe2b.bundle`、`reports/patches/0001-*.patch`（体积治理产物，非源码脏改） |
| 跟踪文件 | 172 个；`.gitignore` 已排除 `Qwen3.5-0.8B-MIT/`(1.7G)、`rknn-llm/`(2.6G)、`out/*.rkllm|*.rknn`(1.2G)、`.venv310/`(5.3G)、`wheelhouse/`(173M) |
| 目录体积 | `deploy` 968K、`scripts` 32K、`tests` 116K、`reports` 9.4M、`logs` 1.7M、`.git` 1.9M |
| 凭据扫描 | 172 个 tracked 文件上扫描 `ghp_`/`github_pat_`/`sk-`/`AKIA`/`PRIVATE KEY`/`password=` **无命中** |

---

## 2. 不一致 / 过时项清单（带文件:行号）

### 2.1 README 树注释「推理服务开机自启」与实际 `disabled` 矛盾
- **文件**：`deploy/README.md:18`
  ```
  ├── rkllm-chat.service       # systemd unit（推理服务开机自启）
  ```
- **实测**：`systemctl is-enabled rkllm-chat` → `disabled`（`UnitFilePreset=enabled`，即「预设应启用但被显式 disable 过」）。
- **补充证据**：`/etc/xdg/autostart/` 与 `/home/w/.config/autostart`（不存在）、`/home/w/.config/systemd/user/`（不存在）均无 rkchat/rkllm 相关自启项；`multi-user.target.wants/` 中无 `rkllm-chat.service`。
- **判读**：当前 UI（PID 1784094 runuser → 1784112 rkchat.py）与推理服务均为「本次会话手工拉起」，**断电重启后不会自动启动 UI，也不会自动启动推理服务**。
- **`DEPLOY.sh` 会纠正**：`deploy/DEPLOY.sh:169` 执行 `systemctl enable rkllm-chat`，所以只要跑过一次全量部署，「开机自启」就成立；即 README 描述的是「部署后的目标状态」而非「当前状态」。

### 2.2 README §7「方式二：手动」编译命令的路径在当前仓库结构下必然失败
- **文件**：`deploy/README.md:282-287`
  ```
  cd deploy/native
  aarch64-linux-gnu-g++ … \
    -L../rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64 \
    -L../rknn-llm/examples/multimodal_model_demo/deploy/3rdparty/librknnrt/Linux/librknn_api/aarch64 \
  ```
- **实测**：`deploy/../rknn-llm` = `<repo>/deploy/../rknn-llm`，但 `rknn-llm/` 实际位于**仓库根**，不在 `deploy/` 下。按此原样执行报错：
  ```
  ld: 找不到 -lrkllmrt: 没有那个文件或目录
  ld: 找不到 -lrknnrt: 没有那个文件或目录
  ```
- **正确写法**应为 `-L../../rknn-llm/...`（从 `deploy/native` 往上两级）。
- **对照**：`deploy/DEPLOY.sh:105-106` 用 `ROOT=$(cd "$HERE/.." && pwd)` 拼绝对路径，**是正确的**；只有 README 的手动方式写错。用 DEPLOY 的等价命令实测 1.9 s 编译成功，产物 sha256 = `48aa0ffe…`（与归档二进制逐字节相同），证明**归档二进制可由当前源码无漂移重建**。

### 2.3 README §8「板端 max_context_len 上限 16384」与模型实际 4096 上限不符（表述歧义）
- **文件**：`deploy/README.md:310`
  ```
  | 上下文仅 4096 | 博客评测用 32768。板端 `max_context_len` 上限 16384，当前 4096。 |
  ```
- **实测**：模型在转换时就被固定为 4096 —— `logs/02_rkllm.log:9` 与 `logs/exp_opt1.log` 均含 `INFO: Setting max_context_limit to 4096`；设备启动日志 `rkllm-toolkit version: 1.3.1, max_context_limit: 4096`。
- **判读**：「16384」是 runtime/driver 侧能力上限，但**当前 .rkllm 文件本身被编译成 4096**，改 `--ctx` 无法突破 4096。README 未区分「runtime 上限」与「模型内嵌上限」，易被误读为「调参数就能到 16384」。

### 2.4 README §3 大小单位混用（MB / MiB）
- **文件**：`deploy/README.md:5`（`214 MiB`）、`:84-87`（`1068 MB` / `214 MB` / `1126 MB` / `225 MB`）
- **实测**：线上 LLM = 1019 MiB = 1068.1 MB；线上 vision = 213.4 MiB = 223.8 MB。
- **判读**：数值本身都对（MB=10^6、MiB=2^20），但同一段里 `§1` 用 MiB、`§3` 用 MB，读者按 MiB 找「214 MiB 的 vision」会与表中「214 MB」混淆。建议统一单位。

### 2.5 README 目录结构树漏列已入库文件
- **文件**：`deploy/README.md:15-44`（结构树）
- **实测树中未列但实际存在且已 git 跟踪**：
  - `deploy/ui/rkchat/STYLE_CHANGES.md`（9967 B，样式改动说明，`git ls-files` 已跟踪）
  - `deploy/voice-vision/`（`README.md` + `talk.py`；README §7 正文第 7 行有提及，但结构树里没有）
  - `deploy/audio/`（**空目录**，无任何文件）
  - `deploy/rkllm-chat/`（**空目录**，无任何文件）
- **判读**：`deploy/audio/` 与 `deploy/rkllm-chat/` 是残留空目录，建议清理（P2）；`STYLE_CHANGES.md` / `voice-vision/` 建议补进结构树。

### 2.6 README §7「需要 `sudo apt install g++-aarch64-linux-gnu`」与实际环境
- **文件**：`deploy/README.md:276`
- **实测**：主机 `/usr/bin/aarch64-linux-gnu-g++` 已存在，编译成功，无需安装。属「对新环境的提示」，不算错误，仅提示**当前机器已满足**。

### 2.7 `model-before.sha256` 第 3 行是裸 PID，非标准校验格式
- **文件**：`reports/non-model-fixes/model-before.sha256:3`、`reports/non-model-fixes/model-after.sha256:3`
  ```
  4603
  ```
- **判读**：该行记录的是推理服务 PID（用于证明「未重启服务」），不是 sha256 记录。导致 `sha256sum -c model-before.sha256` 报 `1 行格式不正确`。不影响 §5 结论（前两行模型哈希完全一致），但会让自动化校验脚本报错。建议改名或加注释（P2）。

### 2.8 `rkllm-service.bak` 并非「上一版」而是「两代之前」
- **文件**：`deploy/README.md:37`
  ```
  │   └── rkllm-service.bak    # 上一版二进制（回滚用）
  ```
- **实测时间线**：
  | 二进制 | size | 编译时间/mtime | sha256 | 关键 strings |
  |---|---|---|---|---|
  | `rkllm-service.bak` | 151136 B | 2026-04-18 01:24 | `5318ecc0…` | GCC Debian 14.2.0；**无** `ev=image`、**无** `ev=kv`、**无** `ctx:"reset"`、**无** `ev=warn` 之外的图像诊断；`done` 无 `%s%s` 后缀；有 `RUNPATH=/data/rkllm_qwen35/lib` |
  | `/userdata/backup-camera-freshness-…/native/rkllm-service`（中间版） | 151296 B | 2026-04-18 02:22 | `542c9fd5…` | 有 `ev=kv`、`done…ctx:"reset"`、`ev=image failed`，**无** `ev=image`（视觉KV刷新诊断） |
  | `rkllm-service`（当前） | 138208 B | 2026-11-10 10:15 | `48aa0ffe…` | GCC **Ubuntu 11.4.0**；有 `ev=image`（`kv_reset/kv_before/kv_after/embedding_hash`）、`ev=kv`、`ev=warn`、`clear failed`/`empty prompt` 分项错误、无 RUNPATH |
- **结论**：`.bak` 是**摄像头修复之前**的版本（缺 `ev=image` 视觉KV刷新协议），设备上真正可回滚的「上一版」是 `/userdata/backup-camera-freshness-20260421-175037/native/rkllm-service`（`542c9fd5…`）。README 的「上一版」表述**不准确**，应改为「摄像头修复前的基线版」。
- **配套事实**：`.bak` 依赖 `RUNPATH`，而当前版**移除了 RUNPATH**，完全依赖 unit 里的 `LD_LIBRARY_PATH`。若有人脱离 systemd 直接跑二进制，当前版会报 `librkllmrt.so: not found`（`ldd` 实测确认），而 `.bak` 不会。这是**有意的行为差异**，但 README 未记录。

### 2.9 README §5 摘录的 unit 是节选，缺 3 个对正确性敏感的字段
- **文件**：`deploy/README.md:172-185` vs 实际 `deploy/rkllm-chat.service`
- **缺失项**：`Environment=LLM_DIR=/data/rkllm_qwen35`、`RestartSec=3`、`RuntimeDirectoryMode=0775`、`UMask=0000`、`[Install] WantedBy=multi-user.target`。
- **判读**：其中 `UMask=0000` + `RuntimeDirectoryMode=0775` 是**桌面用户 w(1001) 能连 root 的 socket 的关键**，README 未说明；照摘录手抄 unit 可能导致 UI 连不上 socket。建议标注「完整内容见 unit 文件」。

### 2.10 服务停止超时（实测发现，文档未记录）
- **证据**：`journalctl -u rkllm-chat` → `Apr 21 17:51:40 … State 'stop-sigterm' timed out. Killing.` / `Failed with result 'timeout'` / `status=9/KILL`；`TimeoutStopUSec=20s`。
- **代码定位**：`deploy/native/rkllm_service.cpp:635-638` `on_signal()` 只置 `g_quit`，但主循环 `:878 while (!g_quit.load()) { :879 int fd = accept(srv, …) }` 会在 `accept()` 上阻塞，`SIGTERM` 后**没有事件可唤醒 accept**，必须等下一次连接或被 `SIGKILL`。
- **影响**：`systemctl restart` 需等满 20 s 超时后被 SIGKILL，期间模型 1.1 GiB 内存被强杀（非优雅释放），且 `deploy/README.md` 与 unit 均未记录此现象。

---

## 3. 风险分级

### P0（阻断交付 / 数据安全）
> 无。部署归档与设备状态一致，模型未被改动，服务正常运行，无凭据泄漏。

### P1（影响可复现性 / 会误导重建者）

| # | 问题 | 文件:行 | 说明 |
|---|---|---|---|
| P1-1 | README 手动编译 `-L../rknn-llm` 路径错误，照抄必然 link 失败 | `deploy/README.md:284-285` | 实测报 `找不到 -lrkllmrt`。正确为 `-L../../rknn-llm/…`。DEPLOY.sh 无此问题 |
| P1-2 | `systemctl is-enabled = disabled`，断电重启后服务与 UI 均不自动拉起，而 README 声称「开机自启」 | `deploy/README.md:18`；设备实测 | 需跑一次 `DEPLOY.sh`（会 `enable`）或手工 `systemctl enable rkllm-chat`。**当前设备处于「不自启」状态** |
| P1-3 | `rkllm-service.bak` 语义错误：不是「上一版」而是两代前（缺 `ev=image` 视觉KV刷新协议） | `deploy/README.md:37` | 若按 README 用 `.bak` 回滚，会同时回退摄像头修复。真正上一版在 `/userdata/backup-camera-freshness-…/native/rkllm-service` |
| P1-4 | SIGTERM 无法唤醒 `accept()`，`systemctl restart` 必然吃满 20 s 超时后被 SIGKILL | `deploy/native/rkllm_service.cpp:635-638, 878-879`；unit 无 `TimeoutStopSec` | 每次重启多花 20 s；强杀不优雅释放 1.1 GiB NPU/模型内存。日志已出现 1 次实证 |

### P2（文档准确性 / 可维护性）

| # | 问题 | 文件:行 |
|---|---|---|
| P2-1 | 「max_context_len 上限 16384」未说明模型已固定为 4096，改 `--ctx` 无效 | `deploy/README.md:310`（对照 `logs/02_rkllm.log:9`） |
| P2-2 | §5 unit 摘录缺 `UMask=0000`/`RuntimeDirectoryMode=0775`/`LLM_DIR`/`RestartSec`/`[Install]`，照抄可能导致 UI 连不上 socket | `deploy/README.md:172-185` |
| P2-3 | 结构树漏列 `STYLE_CHANGES.md`、`voice-vision/`；存在空目录 `deploy/audio/`、`deploy/rkllm-chat/` | `deploy/README.md:15-44` |
| P2-4 | MB / MiB 混用，同段内单位不统一 | `deploy/README.md:5, 84-87` |
| P2-5 | `model-*.sha256` 第 3 行为裸 PID，破坏 `sha256sum -c` 格式 | `reports/{non-model-fixes,camera-freshness}/model-*.sha256:3` |
| P2-6 | 内存余量告急：`free 27Mi`、`available 1.3Gi`、无 Swap；服务 RSS 1.09 GiB + UI 661 MiB | 设备实测 |
| P2-7 | 仓库未推上去（`lenonis` 对 `Quectel-Pi/demo-qwen-assistant` 无 push 权限，403） | team leader 已核实 |
| P2-8 | `/data/voice-vision/README.md` 与 `deploy/voice-vision/README.md` md5 不同（5943 B，内容已分叉） | 设备 vs 本地 |
| P2-9 | 设备时钟未同步（`System clock synchronized: no`，设备 2026-04-22 vs 主机 2026-11-11），跨文件时间比对易误判 | 设备实测 |
| P2-10 | `deploy/DEPLOY.sh:37` `--help` 用 `sed -n '2,25p'` 截断，实际会多打印到 `set -euo pipefail` 及 `HERE=…` 等代码行 | `deploy/DEPLOY.sh:37` |

---

## 4. 已完成工作项汇总（4 个 MANIFEST + 3 份 REPORT）

| 轮次 | 目录 | 结论 | 关键指标 | 备份 |
|---|---|---|---|---|
| ① 系统键盘 | `.dev-flow/system-keyboard/` + `logs/keyboard-fix/RESULT.md` | **PASS** | 7/7 单测；首次触摸弹 GNOME 48.7 OSK；真实按键输入 hello、IBus libpinyin 候选上屏「你好」；输入框下沿 522 < 键盘顶部 533 无遮挡；`builtin_keyboard=false` | `/userdata/backup-system-keyboard-20260420-232815`（316K） |
| ② 综合运行时 | `.dev-flow/comprehensive-runtime/` + `reports/full-test/20261109-164746/REPORT.md` | **部分通过**：链路 COMPLETED，核心功能 PASS，**回答质量/声学识别 FAIL** | 55 条记录（44 主轮 + 11 补测），raw PASS 39 / FAIL 14 / MANUAL 2；纯文本 TTFT 中位 520 ms，含图 TTFT 中位 3645 ms；声学回环 CER 4/13 ≈ 30.8%；UI RSS 稳定 565.4 MiB ×5 | 无（只测不改） |
| ③ 非模型工程修复 | `.dev-flow/non-model-fixes/` + `reports/non-model-fixes/REPORT.md` | **PASS** | 修复前复现 **11 项失败**（`before-tests.log`）；本地 35/35、设备 35/35 单测；最终真机 16/16；连续 20 次重连仅剩 1 接收线程；USB 采集削波 2.18% → 0% | `/userdata/backup-non-model-fixes-20260421-005845`（396K） |
| ④ 摄像头新鲜度 | `.dev-flow/camera-freshness/` + `reports/camera-freshness/REPORT.md` | **PASS** | 本地 45/45、设备 45/45 单测；最终真机 15/15（`device-final/summary.json`）；额外真实内核触摸关闭验证 PASS（`device-touch/summary.json`，busy=true 时按钮 enabled=true，点击后 cam_mode=false）；红→蓝→红 embedding `7dbdfd51…`/`1ecd2442…`/`7dbdfd51…` 可复现 | `/userdata/backup-camera-freshness-20260421-175037`（996K） |

**四轮共同守住的约束**（均已在 MANIFEST 声明并实测验证）：
- 不换模型、不改 max-new / context / 量化参数、不开启 thinking；
- 未 commit / push（前三轮明确「用户未请求」）；
- 每轮回滚前均做备份；
- LLM / Vision 权重 SHA256 前后一致。

**遗留风险（来自各 MANIFEST 的 Open Items）**：
- 模型质量本身：数学 `12×13→154`、`3个苹果+2→15` 错误；开放式问题无关拒答；名字记忆失败（问名字答「你好！」）；格式指令不遵循（要求 `pong` 得 `Pong.`）；长回答中英混杂/跑题。**均未在工程修复范围内**，属模型能力问题。
- 声学 ASR 错字未修（USB 预热后 3 句仍有 2/41 字符错误，小样本）。
- 未覆盖：24 小时老化、断电重启恢复、拔网线、真人免提/语音打断、画面语义完全人工核对。
- GUI 自动化受限：mutter/Xwayland 拦截 Xtst 伪造事件，改用内核 `uinput` 输入事件（临时设备在 `finally` 销毁）。

---

## 5. 模型完整性核对（任务第 5 项）

| 文件 | before | after | 结论 |
|---|---|---|---|
| `reports/non-model-fixes/model-before.sha256` vs `model-after.sha256` | 2 行模型哈希 + 1 行 PID `4603` | 完全相同 | ✅ `diff` 无输出，**模型未被改动** |
| `reports/camera-freshness/model-before.sha256` vs `model-after.sha256` | 2 行模型哈希 | 完全相同 | ✅ `diff` 无输出，**模型未被改动** |
| 设备当前实测（2026-11-11 复核） | — | `883a67e7…87d07`（LLM）、`02337a5…63be3`（vision） | ✅ **与 4 月记录的 before/after 完全一致**，跨月未漂移 |

**双重确认**：报告记录（4 月）+ 本次实测（11 月）的 sha256 完全相同，模型文件自始至终未被修改。

---

## 6. 遗留待办（建议优先级排序）

1. **【P1】修 README §7 手动编译路径**：`-L../rknn-llm` → `-L../../rknn-llm`（2 处）。
2. **【P1】明确「开机自启」状态**：或在 README 注明「需跑一次 DEPLOY.sh 才会 enable」，或直接 `systemctl enable rkllm-chat`（需授权，本次只读未执行）。
3. **【P1】修正 `rkllm-service.bak` 的定位描述**：改为「摄像头修复前的基线版」，并在 README 补一句「真正上一版在 `/userdata/backup-camera-freshness-20260421-175037/native/`」。
4. **【P1】修 SIGTERM 无法唤醒 accept**：`client_thread` / 主循环改为 `poll()` + 自我管道唤醒，或 unit 加 `KillMode=mixed` + `TimeoutStopSec=5`，避免每次 restart 白等 20 s 并强杀。
5. **【P2】README §8 补充「模型内嵌 max_context_limit=4096，改 --ctx 无效」**。
6. **【P2】README §5 改为「完整 unit 见 `deploy/rkllm-chat.service`」，并说明 `UMask=0000` 的作用**。
7. **【P2】清理空目录 `deploy/audio/`、`deploy/rkllm-chat/`；结构树补 `STYLE_CHANGES.md`、`voice-vision/`**。
8. **【P2】统一 MB/MiB；`model-*.sha256` 第 3 行 PID 加注释前缀（如 `# pid 4603`）或移到单独 `.pid` 文件**。
9. **【P2】同步 `/data/voice-vision/README.md` 与本地版本（md5 已分叉）**。
10. **【P2】解决 push 权限**（`lenonis` 对 `Quectel-Pi/demo-qwen-assistant` 403），或改用有权限的账号 / fork + PR。
11. **【P2】设备内存余量仅 1.3 GiB available 且无 Swap**，建议评估是否需 swap/zram 或调低 UI 缓存水位。
12. **【P2】设备时钟未同步**，建议 `timedatectl set-ntp true` 并排查 NTP 源，避免后续取证时间错乱。
13. **【遗留】模型质量问题（数学/拒答/记忆/格式）与 ASR 错字**：不在工程修复范围，如需改善需回滚原版模型或接受现状，应作为产品决策项单独立项。

---

## 附录 A：本次审查执行的只读命令清单（节选）

```
adb devices -l
adb -t 1 shell 'uname -a; cat /etc/os-release; python3 -V'
adb -t 1 shell 'cat /sys/kernel/debug/rknpu/version; free -h; df -h / /data /userdata'
adb -t 1 shell 'md5sum /data/rkllm_qwen35/lib/*.so'
adb -t 1 shell 'sha256sum /data/rkllm_qwen35/Qwen3.5-0.8B-MIT_*.{rkllm,rknn}'
adb -t 1 shell 'systemctl is-active/is-enabled/status rkllm-chat'
adb -t 1 shell 'journalctl -u rkllm-chat --since ...'
adb -t 1 shell 'cat /etc/systemd/system/rkllm-chat.service'
adb -t 1 shell 'sha256sum /proc/1768410/exe; grep rkllmrt /proc/1768410/maps'
adb -t 1 shell 'ldd /data/rkllm-chat/native/rkllm-service'
md5sum/sha256sum 逐文件比对 deploy/* ↔ 设备
file / stat / sha256sum / strings / readelf -d / nm -D  deploy/native/rkllm-service{,.bak}
aarch64-linux-gnu-g++ ... -o /tmp/rkllm-service-rebuild   # 验证可重建（不覆盖归档）
bash deploy/DEPLOY.sh --dry-run                            # 只打印，未碰设备
git status / git ls-files / git log / git bundle verify
```

**未执行（遵守硬约束）**：任何 `apt install`、任何对 `/data` 或 `/userdata` 的写操作、任何 `systemctl start/stop/restart/enable`、任何 `git clone`、任何凭据文件读取。

## 附录 B：设备关键运行时快照

```
kernel   : 6.1.118-rt36 #264 SMP PREEMPT_RT Thu Oct 29 14:31:00 CST 2026 aarch64
os       : Debian GNU/Linux 13 (trixie) / DEBIAN_VERSION_FULL=13.6
cpu      : Cortex-A53 ×8 (online 0-7)
mem      : 3994028 kB total / 2.5Gi used / 27Mi free / 1.3Gi available / Swap 0
rootfs   : /dev/root 42G 20% used
userdata : /dev/mmcblk0p12 8.0G 47% used
npu      : RKNPU driver v0.9.8
rkllm    : runtime 1.3.1, toolkit 1.3.1, max_context_limit 4096, npu_core_num 2, W4A16, mrope
vision   : 输入 448x448x3, 输出 196 x 1024
service  : active(running) PID 1768410, since 2026-04-21 17:52:04, NRestarts=0, RSS 1.09 GiB
ui       : PID 1784094 (runuser -u w) → 1784112 rkchat.py --fullscreen 1280x800+0+0, RSS 661 MiB
sock     : /run/rkllm-chat.sock srw-rw-rw- root:root
thermal  : 47~50 °C (6 zones)
uptime   : 2 days 3:01, load 2.31/2.58/2.40
```
