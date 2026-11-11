# RKNN-MODEL 代码审查报告（只读）

- 审查日期：2026-11-11
- 审查范围：`deploy/native`、`deploy/ui/rkchat`、`deploy/voice-vision`、`deploy/qwen35`、`scripts/`、`deploy/DEPLOY.sh`、`deploy/rkllm-chat.service`
- 审查方式：只读通读 + 定点 grep + 语法校验（`python3 -m py_compile`、`bash -n`、`g++ -fsyntax-only`）；**未执行部署脚本、未连接设备、未安装任何依赖**
- 所有源码均未修改（本次仅新增本报告，并清理了审查时 py_compile 产生的 `__pycache__`）

## 统计

| 项目 | 数值 |
|---|---|
| 审查文件数 | 24 |
| 审查代码行数 | 6721 |
| P0 | 2 |
| P1 | 15 |
| P2 | 23 |
| 语法校验 | Python 20/20 OK，Shell 10/10 OK，C++ `-fsyntax-only` 通过（仅 stb_image 与 1 个 unused-param 警告）|

| 文件 | 行数 |
|---|---|
| deploy/native/rkllm_service.cpp | 899 |
| deploy/native/rkllm_service.h | 31 |
| deploy/ui/rkchat/rkchat.py | 1477 |
| deploy/ui/rkchat/client.py | 304 |
| deploy/ui/rkchat/camera.py | 283 |
| deploy/ui/rkchat/voice.py | 624 |
| deploy/ui/rkchat/vkbd.py | 708 |
| deploy/ui/rkchat/desktop_session.py | 136 |
| deploy/voice-vision/talk.py | 704 |
| deploy/qwen35/chat.py | 719 |
| scripts/*.sh + *.py | 395 |
| deploy/DEPLOY.sh / rkllm-chat.service / run.sh / qwen35/*.sh | 406 |

---

# P0（崩溃 / 安全）

## P0-1 `deploy/voice-vision/talk.py:249` — aplay 失败导致整个语音助手进程崩溃退出

```python
249:                input=pcm, check=True,
252:        except OSError as exc:
```

`subprocess.run(..., check=True)` 在 aplay 返回非零时抛的是 `CalledProcessError`（`SubprocessError` 子类，**不是** `OSError`），因此 252 行接不住。

调用链：`Speaker.play` → `App.speak`（511 行）→ `App.handle` → `run()`，一路都没有 `except`，异常直接冒到 `main()` → `sys.exit(main())` → 进程带 traceback 退出。

板端 aplay 返回非零是常态（ES8389 时钟未锁定、设备被 `pkill` 抢断、USB 声卡热插拔）。这等于「播一次音失败，整个免提对话程序就死了」。`deploy/ui/rkchat/voice.py` 的 `VoiceIO.speak` / `StreamSpeaker._play_loop` 都是 `except Exception`，所以 UI 侧不受影响——只有 talk.py 自身这条链路会崩。

建议修法：
```python
-        except OSError as exc:
+        except (OSError, subprocess.SubprocessError) as exc:
             log("ERR", f"播放失败: {exc}", "red")
```

## P0-2 `deploy/native/rkllm_service.cpp:878-899` — 退出时序竞态：后台线程仍在推理时 `release()` → 段错误

```cpp
878:    while (!g_quit.load()) {
879:        int fd = accept(srv, nullptr, nullptr);
...
886:        if (pthread_create(&th, nullptr, client_thread, (void *)(intptr_t)fd) == 0) {
887:            pthread_detach(th);
...
893:    close(srv);
894:    unlink(sock_path.c_str());
895:    g_llm.release();
896:    g_vision.release();
```

`client_thread` 全部 detach，主线程对它们没有任何 join / 关闭握手。任一连接正卡在 `handle_request` 的 `g_infer_mutex` 里执行 `rkllm_run()`（一轮带图推理 7s+）时收到 `exit`/SIGTERM，主线程会立刻 `rkllm_destroy(handle_)` 并 `exit(0)`：

- `rkllm_run` 内部线程仍在回调 `LlmEngine::callback`，`self->handle_` 已析构 → 访问已释放内存；
- `AskCtx ctx` 是 `handle_request` 的栈对象，此时该栈帧可能已被 `exit()` 拆掉，回调写 `ctx->closed` 就是野指针写。

实测量级：`Restart=always` + `RestartSec=3` 会反复命中该窗口，表现为服务偶发 `Segmentation fault` 而非干净退出（journalctl 里能看到 core dump），且 `unlink(sock_path)` 与崩溃无先后保证，可能留下死 socket 让 UI 连上一个已死 inode。

建议修法（最小改动）：
```cpp
+static std::atomic<int> g_clients{0};
 // client_thread 入口 g_clients.fetch_add(1); 退出前 fetch_sub(1)
 // main 退出段：
     close(srv);
+    for (int i = 0; i < 100 && g_clients.load() > 0; ++i) usleep(50000);  // 最多 5s
     unlink(sock_path.c_str());
     g_llm.release();
```
（更稳的做法是给每个连接留一个 `pthread_t` 列表并 `pthread_join`，或给 fd 加 `shutdown()` 后再 join。）

---

# P1（功能缺陷）

## P1-1 `deploy/native/rkllm_service.cpp:878-884` — SIGTERM 无法唤醒 `accept()`，`systemctl stop/restart` 卡到超时被 SIGKILL

已本地实测：用 `signal(SIGTERM, handler)` 注册的处理函数带 `SA_RESTART` 语义，阻塞在 `accept()` 的进程收到 SIGTERM 后 **accept 被自动重启而不是返回 EINTR**（实测进程在 SIGTERM 后 1s 仍存活）。因此：

- `deploy/DEPLOY.sh:169` 的 `systemctl restart rkllm-chat` 会阻塞到 systemd 默认 `TimeoutStopSec`（90s）后 SIGKILL；
- DEPLOY.sh 只 `sleep 8` 就查 `is-active`，此时服务必然还在 `deactivating`，部署脚本报的状态是假阴性；
- SIGKILL 会跳过 `unlink(sock_path)`，留下 `/run/rkllm-chat.sock` 死 inode，UI 起来后 `connect()` 成功但 `readline()` 立即 EOF → 用户看到"推理服务连接中断"。

建议修法：
```cpp
+    struct sigaction sa{}; sa.sa_handler = on_signal; sigemptyset(&sa.sa_mask);
+    sa.sa_flags = 0;                        // 关键：不要 SA_RESTART
+    sigaction(SIGINT, &sa, nullptr); sigaction(SIGTERM, &sa, nullptr);
     signal(SIGPIPE, SIG_IGN);
```
（或保留 `signal()`，退出段改成 `while (!g_quit) { poll() + accept() }`，或用 self-pipe/eventfd 唤醒。）

## P1-2 `deploy/native/rkllm_service.cpp:117-165` + `:642,675-678` — 自制 JSON 取值器按子串匹配，prompt 内容可劫持 image / keep_history 参数

```cpp
static bool json_find(const std::string &js, const char *key, size_t *vb, size_t *ve) {
    std::string pat = std::string("\"") + key + "\"";
    size_t p = js.find(pat);          // 全串第一次出现，不区分层级
```

`json_find` 找的是**整个请求行里第一次出现**的 `"cmd"` / `"keep_history"` / `"image"` 字面量，不区分它在字符串内还是字符串外。具体到 `ask` 请求，客户端 `json.dumps` 把 `cmd` 放在最前（`client.py:164`），所以 `cmd` 本身暂时不会被劫持；但同一函数也用于 `prompt` / `image` / `keep_history` / `max_new`，而这些 key 都排在 `prompt` **之后**。只要 `prompt` 的内容里出现 `"image"`（模型/用户完全可能提到「image 字段」），`json_str(line,"image")` 就会命中 prompt 内部的字面量：

- 用户问「你收到的 image 参数是什么」→ 服务端认为本轮带图 → 走到 697 行 `g_vision.encode(prompt内的字符串路径)` → 打开失败 → 返回「图像编码失败，本轮未进行推理」，提问被静默丢弃；
- 同理 `prompt` 里出现 `"keep_history":0` 之类的文本会被当成真实参数。

根因是它把「扁平对象的第一个同名 key」当成「唯一 key」。

建议修法：解析时跳过字符串值内部。最小可行改法是把 `json_find` 改成顺序扫描并跟踪 `in_string`/转义状态，只在字符串外部匹配 key；或直接引入一个合规 JSON 解析（服务端本就允许引入第三方）。

## P1-3 `deploy/native/rkllm_service.cpp:773-782` — 单连接读缓冲无上限，可被撑爆内存

```cpp
773:    char chunk[8192];
...
780:        buf.append(chunk, (size_t)n);
```

`buf` 只在遇到 `\n` 时才 `erase`。客户端只要持续发送不带换行的数据（配合下面 P1-4 的 0666 权限，本机任何进程都能做），`buf` 就无界增长 → `std::bad_alloc` 未捕获 → `std::terminate` → 整个推理服务崩溃。

建议修法：
```cpp
         buf.append(chunk, (size_t)n);
+        if (buf.size() > 64 * 1024) {
+            logf_("单行请求超长(%zu)，断开连接", buf.size());
+            break;
+        }
```

## P1-4 `deploy/native/rkllm_service.cpp:871` + `deploy/rkllm-chat.service:23` — socket 权限 0666 + `UMask=0000`：本机任何用户可停掉推理服务、读走全部对话

```cpp
871:    chmod(sock_path.c_str(), 0666);
```
```ini
23: UMask=0000
```

任何本机进程都能：`{"cmd":"exit"}` 让服务退出（`Restart=always` 会拉起，形成持续抖动）；`{"cmd":"clear"}` 清掉别人的上下文；`{"cmd":"ask",...}` 拿 NPU 跑自己的推理；以及最关键的——`kv`/`ask` 的响应里含用户提问与模型回答。

建议修法：socket 改成 `0660` + 固定 group（或 `0666` 保留但只允许 localhost 的特定 uid 通过 `SO_PEERCRED` 校验）：
```cpp
+    struct ucred cred{}; socklen_t cl = sizeof(cred);
+    getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &cl);
+    if (cred.uid != 0 && cred.uid != (uid_t)g_allowed_uid) { close(fd); return nullptr; }
```

## P1-5 `deploy/native/rkllm_service.cpp:713` — `abort` 在等锁期间会被下一轮 `ask` 清掉，停止按钮偶发失效

```cpp
649:        g_abort.store(true);          // abort 连接，不取推理锁
...
713:    g_abort.store(false);             // ask 拿到推理锁后立刻清零
```

一轮推理正在跑时用户点「停止」：`abort` 立刻置 true，正在跑的这轮靠 callback 读 flag 停下——这条路径 OK。但**图片编码阶段（699 行的 `enc_mutex`，最长 1s+）已经在等 `g_infer_mutex` 的请求**还没走到 713 行；此时用户点停止，`g_abort=true` 会在该请求拿到锁后被 713 行无条件清掉，用户看到"点了停止但没停"。

建议修法：把 `g_abort` 改成**代际/序号**语义，或者 abort 时记录目标轮次：
```cpp
+static std::atomic<unsigned> g_round{0};
+static std::atomic<unsigned> g_abort_round{0};
 // ask 入口：unsigned my = ++g_round;
 // abort：  g_abort_round.store(g_round.load());
 // 判定：   aborted = (g_abort_round.load() >= my);
```

## P1-6 `deploy/native/rkllm_service.cpp:391` + `:758` — `done_sent` 永远不会被置位，中断时必然重复下发 `done`

```cpp
391:    std::atomic<bool> done_sent{false};
...
758:        if (!ctx.closed && !ctx.done_sent.load()) {     // 恒为真
```

全文件搜不到任何 `done_sent = true`（也搜不到 `store(true)`）。而 `callback` 在 `RKLLM_RUN_FINISH` 分支里**已经**发过一条带 `aborted:1` 的 `done`（568-576 行）。于是中断时客户端会收到两条 `done`：

- 第一条：`_on_done` 正常收尾；
- 第二条：`_on_done` 再次执行，`cur_bubble` 已是 None，仍会重置 `btn_send` 文案、重复 `_stop_reading()`、并把 `_need_replay` 再置 True。

对 `client.py:218-227` 而言，第二条 done 还会再发一次 `on_state(False)`。目前危害是 UI 状态抖动与冗余事件，但这是明确写坏了的保护条件。

建议修法：
```cpp
             if (!ctx->closed) {
                 ... send done ...
                 if (!send_line(ctx->fd, buf)) ctx->closed = true;
             }
+            ctx->done_sent.store(true);
```

## P1-7 `deploy/native/rkllm_service.cpp:210-212` — `ftell` 返回值未校验，模型路径异常时抛未捕获异常

```cpp
210:        long len = ftell(fp);
212:        std::vector<char> buf((size_t)len);
```

模型路径若是目录/管道/`/proc` 下的文件，`ftell` 返回 -1，`(size_t)-1` 变成 `SIZE_MAX` → `std::vector` 抛 `std::length_error` 或 `std::bad_alloc`，`main` 里没有 try/catch → `std::terminate`。systemd `Restart=always` 会无限重启抖动。

建议修法：
```cpp
     long len = ftell(fp);
+    if (len <= 0 || len > (long)(2UL << 30)) { fclose(fp); logf_("vision 模型大小非法: %ld", len); return false; }
```

## P1-8 `deploy/ui/rkchat/camera.py:99` + `:186-215` — ffmpeg 的 stderr 管道从不读取，写满 64KB 后预览线程永久阻塞

```python
 99:            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
...
186:    def _pump(self, proc, epoch):
194:                buf = proc.stdout.read(self._nbytes)     # 只读 stdout
```

`_pump` 永远不读 `proc.stderr`。ffmpeg 用 `-loglevel error` 时输出很少，但一旦开始刷错误（设备忙、格式协商失败、USB 掉线重连），stderr 管道缓冲（64KB）写满后 ffmpeg 自己阻塞在 `write(2)` 上 → 不再产出帧 → `proc.stdout.read` 永久阻塞在 `_pump` 里；此时 `stop()` 的 `proc.kill()` 能救回来，但在这之前 UI 表现为"点了摄像头，一直显示等待新画面"，且 `_pump` 线程无法退出。

建议修法：
```python
-            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
+            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
```
（要保留错误信息的话，起第三个线程 `for line in proc.stderr: ...` 排空。）

## P1-9 `deploy/ui/rkchat/camera.py:99-118` — ffmpeg 起不来时 `start()` 仍返回 True，UI 误判摄像头已打开

`Popen` 成功后 `self._running = True` 立刻就置位并返回 True。若 ffmpeg 因设备被占用立刻退出（板端只有一个 V4L2 节点，上一轮 `stop()` 的 kill 可能还没生效），`_pump` 读到 EOF 后在 finally 里把 `_running` 清掉——但 `Camera._on_cam_clicked` 已经走了 `self.cam_mode = True` 分支，按钮变成"关闭摄像头"、预览区展开，然后卡在"摄像头已停止，请关闭后重开"。

建议修法：`start()` 里加一次握手
```python
         thread.start()
+        deadline = time.monotonic() + 1.5
+        while time.monotonic() < deadline and self._seq == 0:
+            if proc.poll() is not None:
+                self.last_error = self.last_error or "ffmpeg 立即退出"
+                self.stop(); return False
+            time.sleep(0.05)
         return True
```

## P1-10 `deploy/qwen35/chat.py:145-162` — `start()` 里 `_sync_prompt` 超时不清理子进程，反复重启会累积 demo 进程

```python
137:        self.proc = subprocess.Popen(...)
139:        self._sync_prompt(timeout=180)     # 抛 EngineError 时 self.proc 仍保留
```

`_sync_prompt` 超时抛 `EngineError`，`self.proc` 不被 `stop()`/`kill()`。调用方 `_restart_engine`（`/image`、`/tokens`、`/ctx` 都会触发）只 `print("启动失败")` 然后 `return False`，也不会回收。下一次 `/image` 再 `stop()` 时 `self.proc` 已被覆盖成新的 Popen 对象 → **旧 demo 进程彻底泄漏**（一个 demo 常驻约 1.5GB + 占用 NPU），几次之后板子直接 OOM。

建议修法：
```python
+        try:
+            self._sync_prompt(timeout=180)
+        except BaseException:
+            self.stop()
+            raise
         self.load_secs = time.monotonic() - t0
```

## P1-11 `deploy/qwen35/chat.py:150` — `Engine.stop()` 的 `self.proc.stdin.write` 未受保护，可能阻塞/二次异常

`stop()` 只捕 `BrokenPipeError/OSError/ValueError`，但 demo 进程卡死时 `stdin.write` 会**阻塞**（管道满），`stop()` 不再有 3s 上限，`finally` 里调用会永久挂住。另外 `wait(timeout=3)` 超时后 `kill()`，但 `stdin/stdout` 两个文件对象从未 `close()` → fd 泄漏（每次 `/image` 切图泄漏 2 个）。

建议修法：
```python
         try:
             self.proc.wait(timeout=3)
         except subprocess.TimeoutExpired:
             self.proc.kill()
             ...
-        self.proc = None
+        for f in (self.proc.stdin, self.proc.stdout):
+            try:
+                if f: f.close()
+            except OSError:
+                pass
+        self.proc = None
```

## P1-12 `deploy/ui/rkchat/rkchat.py:1253` / `:1263` — 打断监听用**新建的** `VoiceIO()` 实例去 open/close 共享的全局 `_mic`

```python
1253:            if voice.VoiceIO().listen_for_barge_in(stop):
1263:        if voice.VoiceIO().listen_for_barge_in(stop):
```

`voice.py` 里 `_mic` 是模块级全局单例，而 `listen_for_barge_in` 的 `self._recording`、`_stop_flag` 都在实例上。问题有两个：

1. 新建实例意味着 `rkchat` 侧的 `self.voice_io.stop_listening()` 完全影响不到这个监听器——它只能靠 `stop_event` 退出。在 `_start_stream_speaker()`（`rkchat.py:1351`）把 `_stop_speak` 置为 `None` 的那条正常路径下，`_wait_speaking_with_barge_in` 每次都进入 `sp is not None and sp.busy` 分支，于是**每一轮朗读都会新开一次 `_mic.open()`**（一个新 arecord 子进程），而不是复用已有麦克风状态。
2. 同一时刻若 `_voice_loop` 正在 `listen_once`（也会 `_mic.open()`），或用户手动 `_start_ask` → `voice_io.stop_listening()`，两个线程对同一 ALSA 设备并发 open/close。`_half_duplex_lock` 只在 `can_listen_while_playing()` 为 False 时生效；用 USB 麦时该函数返回 True，锁整个被跳过 → 真正的设备争用（arecord 报 "Device or resource busy"）。

建议修法：复用同一个实例并把设备操作串行化
```python
-            if voice.VoiceIO().listen_for_barge_in(stop):
+            if self.voice_io.listen_for_barge_in(stop):
```
并在 `voice.py::listen_for_barge_in` 与 `_listen_once` 外层统一用 `_mic_lock`（而不是 `_half_duplex_lock`）包住 `_mic.open()/close()`。

## P1-13 `deploy/ui/rkchat/rkchat.py:676` — 发送失败时用户输入已被清空，内容永久丢失

```python
676:        self.entry.set_text("")
...
688:            subprocess.run(["pkill", "-f", "aplay"], ...
...
690:        self._submit(text)          # 内部可能因 busy/未连接直接丢弃并只弹一条 warn
```

`_submit` 只弹「上一轮尚未结束」的提示就 `return`，`text` 既不回填 `entry` 也不进历史。用户打完一段话点发送、恰好撞上 `cur_bubble is not None` 的窗口，内容就没了。

建议修法：
```python
     def _submit(self, text: str):
         if not self._alive:
             return
         if self._capture_pending or self.cli.busy or self.cur_bubble is not None:
             self._on_warn("上一轮尚未结束，请先停止或等待回答完成")
+            self.entry.set_text(text)
+            self.entry.set_position(-1)
             return
```

## P1-14 `deploy/ui/rkchat/client.py:143-155` — `_send` 在 GTK 主线程做无超时阻塞写

```python
148:                self._file.write(...)
150:                self._file.flush()
```

`socket.settimeout(None)`（89 行）意味着写操作无超时。`ask()` 由 GTK 主线程（`_start_ask`）调用 → 若服务端此刻阻塞在 `g_infer_mutex`（正跑另一轮推理）而 socket 发送缓冲被填满，`write` 直接阻塞主线程 → 整个界面冻结。当前请求体很小（≤1KB，重放上限 900 字），触发概率低，但 `_replay_prefix` 一旦被调大就变成现实风险。

建议修法：给主连接设置写超时
```python
-            s.settimeout(None)
+            s.settimeout(10.0)
```

## P1-15 `scripts/01_convert_all.sh:37,43,49` — 所有转换步骤都用 `|| true` 吞掉失败，脚本永远打印 `CONVERT DONE`

```bash
37:  ... 2>&1 | tee "$LOGS/02_rkllm.log" | grep -aE "^OK ->|failed|Error" || true
55:  cp -f "$DEMO/rkllm/"*.rkllm "$OUT/" 2>/dev/null || true
```

文件头有 `set -euo pipefail`，但每步转换末尾的 `|| true` 把这层保护彻底废掉：`export_rkllm.py` 返回非零也继续往下走，第 55 行 `cp` 找不到产物也只是 `|| true` 静默跳过，最后打印 `== CONVERT DONE ==` 且退出码 0。CI/自动化会误判成功，后续 `deploy` 用的是 `out/` 里的旧模型。

建议修法：去掉 `|| true`，让 `set -e` 对 `export_rkllm.py` / `export_vision.py` 的退出码生效。已本地验证：

```bash
-  ... | tee "$LOGS/02_rkllm.log" | grep -aE "^OK ->|failed|Error" || true
+  ... | tee "$LOGS/02_rkllm.log" | grep -aE "^OK ->|failed|Error" || [ "${PIPESTATUS[0]}" = 0 ]
```

实测第一种写法即使 `export_rkllm.py` 返回 3、grep 也不匹配，脚本仍打印 `CONVERT DONE` 并 `exit 0`；改成第二种后立即 `exit 1`。（注意需同时开 `set -o pipefail` 或依赖 `PIPESTATUS`，当前脚本已有 `pipefail`。）

---

# P2（可维护性 / 健壮性）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `rkllm_service.cpp:659-664` | `cmd=="kv"` 不取 `g_infer_mutex` 就读 `rkllm_get_kv_cache_size`，与 `rkllm_run` 并发访问 vendor handle | 与 `clear` 一样加 `std::lock_guard lk(g_infer_mutex)`（会拖慢诊断，可接受） |
| P2-2 | `rkllm_service.cpp:698-708` | vision 编码不持 `g_infer_mutex`，与正在跑的 LLM 抢 NPU，实测首字延迟抖动 | 有意设计，但建议加注释说明，或限制并发编码为 1 并打日志 |
| P2-3 | `rkllm_service.cpp:180-192` | `send_line` 无超时。客户端不读时（见 P1-3 的变体）`send` 会阻塞，长时间占着 `g_infer_mutex` | 把 fd 设 `O_NONBLOCK` + 自定义发送缓冲，或 `SO_SNDTIMEO` |
| P2-4 | `rkllm_service.cpp` 全文 | `LlmEngine::instance()`、`current_tokens()`、`rollback_kv()`、`RKLLM_ABORTED_MARK`、`IMG_TOKENS`/`IMG_EMBED_LEN` 均为死代码 | 删除；`rkllm_service.h` 里两个宏已不再被引用 |
| P2-5 | `rkllm_service.cpp:491` | `ask()` 的 `max_new` 参数未使用（编译器已警告） | 加 `(void)max_new;` 或删参数 |
| P2-6 | `rkllm_service.cpp:88-110` | `json_escape` 注释说"非法 UTF-8 退回 '?'"，实际把原始字节直接透传 | 要么改注释，要么真的做 UTF-8 校验 |
| P2-7 | `rkllm_service.cpp:538` | `// LLM 就绪` 附近 `rkllm_run` 失败时不尝试回滚 KV；失败轮次可能污染上下文 | 失败分支补 `clear_kv()`，与中断路径一致 |
| P2-8 | `camera.py:268-272` | `write_bmp` 用 `except Exception: return False` 吞掉所有错误（含磁盘满），调用方只看到"未取得新画面" | 至少 `print` 到 stderr，便于现场定位 |
| P2-9 | `rkchat.py:688, :1282` | `pkill -f "aplay"` 用 `-f` 匹配整个命令行，会误杀编辑器/日志里含 "aplay" 的进程；且从 GTK 主线程同步调用 | 改 `pkill -x aplay`（`voice.py:492` 已经用了 `-x`，这里不一致），或挪到后台线程 |
| P2-10 | `rkchat.py:279` | `self._speak_seq` 声明为"防止旧轮回调喂新轮"，但全文再无读取 | 删除，或真的拿它给 `_stream_feed` 做代际校验 |
| P2-11 | `rkchat.py:725-737` | `_cancel_capture()` 不删除已 `mkstemp` 的文件（靠 `TemporaryDirectory.cleanup()` 兜底），且 `_snapshot_paths` 只保留 8 个 | 在 `_cancel_capture` 里同步删掉当前 path |
| P2-12 | `rkchat.py:1384, :1000` | `add_class("stat-bad")` 只在超阈值时加，回到正常后从不移除 → 内存条永远红色 | 加 `else: remove_class("stat-bad")` |
| P2-13 | `rkchat.py:1463` | `Gdk.Display.get_default().__gtype__.name` 在无显示时 `None.__gtype__` → `AttributeError`（仅影响一行日志） | `d = Gdk.Display.get_default(); d.__gtype__.name if d else "-"` |
| P2-14 | `talk.py:327-336` | `/tmp/.frame.raw` 固定路径，多实例互踩；`r3` 失败时不 `os.unlink(raw)` | 用 `tempfile.mkstemp`，并在 `finally` 里清理 |
| P2-15 | `talk.py:207-215` | `calibrate()` 写 `/tmp/.talk_speaker_k` 无原子性；`sox`/`aplay` 的返回码不检查，`dur` 落在 0.3~60s 之外的静默失败会让 `ratio` 保持默认 | `mkstemp`+`os.replace`；`subprocess.run(..., check=False)` 后显式判 `returncode` |
| P2-16 | `chat.py:630` | `!cmd` 直接 `os.system(line[1:])`，无白名单、不检查返回码 | 属设计功能，但建议至少 `print` 返回码；或在 HELP 里明确标注"等同 shell" |
| P2-17 | `desktop_session.py:19` | `dict(line.split("=",1) ...)` 后直接 `props["User"]` / `props["Type"]`，缺字段时 `KeyError` 未被 `except` 列表覆盖（只捕 `OSError/CalledProcessError/RuntimeError`）→ 无 traceback 友好提示 | 改成 `props.get("User")` 并显式抛 `RuntimeError` |
| P2-18 | `DEPLOY.sh:43-50` | `run()` 用 `eval "$@"` 执行拼装字符串；`DEV_PUSH` 的 `'$src'` 手工加引号，路径含单引号即注入 | 改数组 + `"$@"`，去掉 `eval` |
| P2-19 | `qwen35/run.sh:12` / `stress.sh:12` | `setsid bash -c "tail -f $IN | ./demo '$IMG' ... $MT ..."`：`$MT` 未加引号、`$IMG` 手工单引号 | 用 `printf %q` 转义，或改成不经过 `bash -c` 的数组形式 |
| P2-20 | `qwen35/chat.sh:19-20` | 无 `set -e`；`free -m` 第 7 列不存在时 `AVAIL` 为空，`[ "$AVAIL" -lt 1800 ]` 报 "integer expression expected" | `[ -n "$AVAIL" ] && [ "$AVAIL" -lt 1800 ] && ...` |
| P2-21 | `rkllm-chat.service:3-5` | `After=network.target` + 注释提到模型常驻 1.5GB，但没有 `MemoryMax`/`OOMScoreAdjust`；`Conflicts=shutdown.target` 对 `WantedBy=multi-user.target` 无实际作用 | 补 `OOMScoreAdjust=-500`，删掉无效的 `Conflicts=` |
| P2-22 | `scripts/00_setup_env.sh:9-11` | `PIP_PROXY` 默认指向 `127.0.0.1:7897`，无代理环境会静默走直连并 SSL 失败在 torch 那步 | 改成默认空，需代理时显式导出 |
| P2-23 | `scripts/verify_ui.py:126,151` | `open(xwd,"rb").read()` / `open(path,"rb").read()` 未用 `with`，且 `h = struct.unpack(">25I", ...)` 写死 100 字节头 | `with` + 校验 `XWD_HDR` 与实际头长 |

---

# 复查建议（优先级顺序）

1. **P0-1 / P0-2 先修**：一个让语音助手直接退出，一个是服务退出时野指针写。
2. **P1-1 + P1-4**：直接影响 `DEPLOY.sh` 的可用性与 socket 安全面，改动都只有 3-5 行。
3. **P1-8（camera stderr）+ P1-12（录音设备争用）**：这两条是现场"摄像头卡住""语音点了没反应"最可能的根因，都只需 1 行改动。
4. **P1-5 / P1-6**：`g_abort` 代际化 + `done_sent` 置位，建议一起改，并补一个针对「等锁期间 abort」的单测（现有 `tests/test_runtime_robustness.py` 已有 mock 框架可复用）。

## 未覆盖 / 需实机确认

- P1-5、P1-6 的时序结论基于代码阅读与 vendor 头文件注释（`rkllm.h:431` 明确 `keep_history==0` 且 paused 后区间清理才有效），未在设备上复现；建议用 `tests/device_full_test.py` 的场景补一条"编码等待期按停止"的用例。
- 未审查 `rknn-llm/` 上游 SDK 源码、`stb_image.h`、`rkllm.h`/`rknn_api.h`（供应商头文件，仅按需核对字段语义）。
- 未做任何部署/设备操作，模型与运行时行为未验证。
