#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qwen35-chat —— Quectel Pi M2 (RK3576) 板端 Qwen3.5-0.8B 多模态 CLI 对话界面

在 rkllm 官方 demo 二进制之上包一层交互壳，解决它原生体验的四个问题：
  1. 必须手敲 <image> 前缀   → 自动加
  2. 输出被管道块缓冲         → stdbuf -o0 逐 token 流式
  3. 换图要重敲长参数         → /image 一条命令
  4. keep_history=0 无上下文  → 可选的会话上下文回填

用法：
    ./chat.py                          # 用 demo.jpg 开聊
    ./chat.py -i photo.jpg -t 384      # 指定图片 / 生成长度
    ./chat.py -p "图里有几个人？"       # 一次性提问（脚本用）
"""

from __future__ import annotations

import argparse
import codecs
import os
import readline
import select
import shutil
import subprocess
import sys
import time
import unicodedata

HERE = os.environ.get("LLM_DIR") or os.path.dirname(os.path.abspath(__file__))

# ── 模型与运行时常量 ────────────────────────────────────────────────
VISION_MODEL = os.path.join(HERE, "Qwen3.5-0.8B-MIT_vision_rk3576.rknn")
LLM_MODEL = os.path.join(HERE, "Qwen3.5-0.8B-MIT_w4a16_rk3576.rkllm")
DEMO_BIN = os.path.join(HERE, "demo")
LIB_DIR = os.path.join(HERE, "lib")

IMG_START = "<|vision_start|>"
IMG_END = "<|vision_end|>"
IMG_PAD = "<|image_pad|>"

IMAGE_TOKENS = 196          # 448x448 输入实测的视觉 token 数
NPU_CORES = 2               # RK3576 是 2 核 NPU；传 3 会静默退化到单核
PLATFORM = "rk3576"
MODEL_MAX_CTX = 4096        # rkllm 日志里的 max_context_limit
READ_HOLDBACK = 12          # 为检测结尾 "\\nuser: " 而暂缓输出的字节数

HISTORY_FILE = os.path.expanduser("~/.qwen35_history")

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "magenta": "\033[35m", "blue": "\033[34m",
}


# ── 小工具 ──────────────────────────────────────────────────────────
class Paint:
    """ANSI 着色；终端不支持时自动退化为纯文本。"""

    def __init__(self, enable: bool) -> None:
        self.on = enable

    def __call__(self, text: str, *styles: str) -> str:
        if not self.on:
            return text
        return "".join(C[s] for s in styles) + text + C["reset"]


def est_tokens(text: str) -> int:
    """粗估 token 数：中文约 1.6 字/token，ASCII 约 4 字符/token。"""
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4) + 1


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


# ── 引擎：管理 demo 子进程 ───────────────────────────────────────────
class EngineError(RuntimeError):
    pass


class Engine:
    """把 rkllm demo 当成一个可对话的子进程来驱动。

    demo 的交互协议（读源码 main.cpp 得到，官方 README 与实际不符）：
        argv: demo <img> <img_enc> <llm> <max_new> <max_ctx> <cores> <platform>
                    <img_start> <img_end> <img_content>
        stdin : 每行一个问题；特殊行 "clear" 清 KV cache，"exit" 退出
        stdout: 启动横幅 + 菜单 → "\nuser: " → 输入后打印 "robot: " + 逐 token 文本
                → 结束打印 "\n" → 再打印 "\nuser: "
    """

    def __init__(self, image: str, max_new: int, max_ctx: int,
                 timeout: float = 600.0) -> None:
        self.image = os.path.abspath(image)
        self.max_new = max_new
        self.max_ctx = max_ctx
        self.timeout = timeout
        self.proc: subprocess.Popen[bytes] | None = None
        self.load_secs = 0.0

    # -- 生命周期 --------------------------------------------------
    def _check_assets(self) -> None:
        for path in (DEMO_BIN, VISION_MODEL, LLM_MODEL, self.image):
            if not os.path.exists(path):
                raise EngineError(f"缺少文件: {path}")
        if not os.path.isdir(LIB_DIR):
            raise EngineError(f"缺少运行库目录: {LIB_DIR}")

    def start(self) -> None:
        self._check_assets()
        cmd = [
            "stdbuf", "-o0", DEMO_BIN,
            self.image,
            VISION_MODEL,
            LLM_MODEL,
            str(self.max_new),
            str(self.max_ctx),
            str(NPU_CORES),
            PLATFORM,
            IMG_START, IMG_END, IMG_PAD,
        ]
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")
        t0 = time.monotonic()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, cwd=HERE, env=env,
        )
        # 超时/异常时必须回收子进程，否则 self.proc 会被下一次 start() 覆盖，
        # 旧 demo 常驻 ~1.5GB + 占 NPU，反复 /image 会 OOM。
        try:
            self._sync_prompt(timeout=180)
        except BaseException:
            self.stop()
            raise
        self.load_secs = time.monotonic() - t0

    def stop(self) -> None:
        proc, self.proc = self.proc, None   # 先摘下引用，避免重入/半死状态
        if proc is None:
            return
        try:
            stdin = proc.stdin
            if proc.poll() is None and stdin is not None and not stdin.closed:
                stdin.write(b"exit\n")
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        # 无论是否正常退出都关掉管道，否则每次换图泄漏 2 个 fd
        for f in (proc.stdin, proc.stdout):
            if f is None:
                continue
            try:
                f.close()
            except (OSError, ValueError):
                pass

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def _fd(self) -> int:
        assert self.proc is not None and self.proc.stdout is not None
        return self.proc.stdout.fileno()

    # -- 底层读写 --------------------------------------------------
    def _read_chunk(self, wait: float) -> bytes:
        readable, _, _ = select.select([self._fd], [], [], wait)
        if not readable:
            return b""
        data = os.read(self._fd, 65536)
        if not data:
            raise EngineError("demo 进程已退出（可能被 OOM kill）")
        return data

    def _sync_prompt(self, timeout: float = 30.0) -> None:
        """吞掉输出直到看见等待输入的 "user: " 提示符。"""
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._read_chunk(0.25)
            if not chunk:
                continue
            buf += chunk
            if len(buf) > 8192:
                buf = buf[-512:]
            if buf.rfind(b"user:") >= len(buf) - READ_HOLDBACK:
                return
        raise EngineError(f"等待提示符超时（{timeout:.0f}s）")

    def _send(self, line: str) -> None:
        if not self.alive:
            raise EngineError("demo 进程不在运行")
        assert self.proc is not None and self.proc.stdin is not None
        # getline 按行读取，所以内部换行必须折叠
        flat = " ".join(line.splitlines()).strip()
        try:
            self.proc.stdin.write(flat.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EngineError(f"写入失败: {exc}") from exc

    # -- 对外接口 --------------------------------------------------
    def clear_kv(self) -> None:
        if not self.alive:
            return
        self._send("clear")
        self._sync_prompt(timeout=20)

    def ask(self, prompt: str, on_token=None, timeout: float | None = None):
        """发送一轮提问，流式回调每个文本片段。

        返回 dict: text / ttft / total / chars / est_tokens
        """
        if not self.alive:
            self.start()

        t0 = time.monotonic()
        limit = timeout if timeout is not None else self.timeout
        self._send(prompt)

        dec = codecs.getincrementaldecoder("utf-8")("replace")
        pieces: list[str] = []
        buf = b""
        started = False
        ttft = 0.0

        def emit(raw: bytes, final: bool = False) -> None:
            nonlocal ttft
            text = dec.decode(raw, final)
            if not text:
                return
            # demo 在调用 rkllm_run() 之前就打印了 "robot: "，
            # 所以首字延迟只能以第一个非空文本片段为准。
            if ttft == 0.0 and text.strip():
                ttft = time.monotonic() - t0
            pieces.append(text)
            if on_token is not None:
                on_token(text)

        try:
            while True:
                if time.monotonic() - t0 > limit:
                    raise EngineError(f"生成超时（{limit:.0f}s）")
                chunk = self._read_chunk(0.25)
                if not chunk:
                    continue
                buf += chunk

                if not started:
                    idx = buf.find(b"robot:")
                    if idx < 0:
                        # 可能是 clear 这类无输出的命令，只等提示符回来
                        tail = buf.rfind(b"user:")
                        if tail >= 0 and tail >= len(buf) - READ_HOLDBACK:
                            return {"text": "", "ttft": 0.0,
                                    "total": time.monotonic() - t0,
                                    "chars": 0, "est_tokens": 0}
                        if len(buf) > 8192:
                            buf = buf[-512:]
                        continue
                    buf = buf[idx + len(b"robot:"):]
                    if buf.startswith(b" "):
                        buf = buf[1:]
                    started = True

                tail = buf.rfind(b"\nuser:")
                if tail >= 0 and tail >= len(buf) - READ_HOLDBACK:
                    emit(buf[:tail], final=True)
                    break

                if len(buf) > READ_HOLDBACK:
                    emit(buf[:-READ_HOLDBACK])
                    buf = buf[-READ_HOLDBACK:]
        except KeyboardInterrupt:
            raise

        total = time.monotonic() - t0
        text = "".join(pieces)
        if ttft == 0.0 and text.strip():
            ttft = total
        return {
            "text": text,
            "ttft": ttft,
            "total": total,
            "chars": len(text),
            "est_tokens": est_tokens(text),
        }


# ── 交互界面 ────────────────────────────────────────────────────────
def _dw(text: str) -> int:
    """字符显示宽度：中日韩字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _box(lines: list[str]) -> str:
    """画一个能对齐中文的方框。"""
    width = max(_dw(line) for line in lines) + 2
    out = ["  \u256d" + "\u2500" * width + "\u256e"]
    for line in lines:
        out.append("  \u2502 " + line + " " * (width - 1 - _dw(line)) + "\u2502")
    out.append("  \u2570" + "\u2500" * width + "\u256f")
    return "\n".join(out)


BANNER = "\n" + _box(["Qwen3.5-0.8B  \u00b7  Quectel Pi M2 NPU  \u00b7  \u591a\u6a21\u6001\u5bf9\u8bdd"]) + "\n"

# demo 自带的两个预设问题（否则用户得自己写）
PRESETS = {
    "0": "What is in the image?",
    "1": "这张图片中有什么？",
}

HELP = """\
提问：直接输入中文/英文，会自动带上 <image>。
      模型看不到别的图 —— 图是启动时加载的，换图用 /image。

命令：
  /image <路径>     换一张图（重新加载模型，约 7 秒）
  /tokens <n>       设置单轮最多生成多少 token
  /ctx <n>          设置上下文长度上限（越大越占内存）
  /history [on|off] 开关「把前几轮对话拼进提示词」
                    —— demo 的 keep_history=0，每轮本就独立，这个开关是软件补的上下文
  /clear            清空模型 KV cache
  /reset            清空对话记录 + KV cache
  /stats            看上一轮耗时
  /save [路径]      把本次对话存成 markdown
  /help             这份帮助
  /quit             退出（Ctrl-D 同效）

技巧：
  Ctrl-C  中断当前生成（会重启引擎，下一问需重新加载模型）
  ↑ ↓     翻历史提问（存在 ~/.qwen35_history）
  0 / 1   直接用 demo 原生预设问题（英文 / 中文）
  !<命令> 在 shell 里执行命令（等价于直接跑 shell，无沙箱），如 !free -m
"""


class Session:
    def __init__(self, args: argparse.Namespace) -> None:
        self.p = Paint(not args.no_color and sys.stdout.isatty())
        self.image = os.path.abspath(args.image)
        self.max_new = args.tokens
        self.max_ctx = args.ctx
        self.use_history = args.history
        self.turns: list[tuple[str, str]] = []
        self.last_stats: dict | None = None
        self.engine = Engine(self.image, self.max_new, self.max_ctx)

    # -- 展示 ------------------------------------------------------
    def _header(self) -> None:
        p = self.p
        width = min(shutil.get_terminal_size((80, 24)).columns, 78)
        print(p(BANNER, "cyan"))
        print(p("─" * width, "dim"))
        rows = [
            ("模型", "Qwen3.5-0.8B-MIT W4A16"),
            ("平台", f"Quectel Pi M2 (RK3576) NPU {NPU_CORES} 核 @950MHz"),
            ("图片", self.image),
            ("生成上限", f"{self.max_new} token"),
            ("上下文", f"{self.max_ctx} token（模型上限 {MODEL_MAX_CTX}）"),
            ("可用内存", f"{mem_available_mb()} MB"),
            ("上下文回填", "开" if self.use_history else "关（每轮独立）"),
        ]
        for key, val in rows:
            print(f"  {p(key.ljust(6), 'dim')}  {val}")
        print(p("─" * width, "dim"))
        print(p("  /help 看命令    Ctrl-D 退出", "dim"))
        print()

    def _prompt_text(self) -> str:
        mark = self.p("你", "bold", "cyan")
        return f"{mark} › "

    # -- 命令 ------------------------------------------------------
    def _restart_engine(self, why: str) -> bool:
        """重建引擎。demo 把 max_new / max_ctx 固化在启动参数里，改它们必须重启。"""
        print(self.p(f"重启引擎以应用{why}…", "dim"))
        self.engine.stop()
        self.engine = Engine(self.image, self.max_new, self.max_ctx)
        try:
            self.engine.start()
        except EngineError as exc:
            print(self.p(f"启动失败: {exc}", "red"))
            return False
        print(self.p(f"已就绪（加载 {self.engine.load_secs:.1f}s）", "green"))
        return True

    def _cmd_image(self, arg: str) -> None:
        if not arg:
            print(self.p(f"当前图片: {self.image}", "yellow"))
            return
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.exists(path):
            print(self.p(f"文件不存在: {path}", "red"))
            return
        self.image = path
        if self._restart_engine("新图片"):
            self.turns.clear()
            print(self.p(f"已切换 → {os.path.basename(path)}（对话记录已清空）", "green"))

    def _cmd_tokens(self, arg: str) -> None:
        if not arg:
            print(self.p(f"当前上限 {self.max_new} token", "yellow"))
            return
        try:
            n = int(arg)
            if not 1 <= n <= 2048:
                raise ValueError
        except ValueError:
            print(self.p("用法: /tokens <1-2048>", "red"))
            return
        self.max_new = n
        if self._restart_engine(f"生成上限 {n}"):
            print(self.p(f"生成长度 → {n}", "green"))

    def _cmd_ctx(self, arg: str) -> None:
        if not arg:
            print(self.p(f"当前上下文 {self.max_ctx}", "yellow"))
            return
        try:
            n = int(arg)
            if not 512 <= n <= MODEL_MAX_CTX:
                raise ValueError
        except ValueError:
            print(self.p(f"用法: /ctx <512-{MODEL_MAX_CTX}>", "red"))
            return
        need = IMAGE_TOKENS + self.max_new + 64
        if n < need:
            print(self.p(f"太小：图像 {IMAGE_TOKENS} + 生成 {self.max_new} 至少需要 {need}", "red"))
            return
        self.max_ctx = n
        if self._restart_engine(f"上下文 {n}"):
            print(self.p(f"上下文 → {n}", "green"))

    def _cmd_history(self, arg: str) -> None:
        arg = arg.strip().lower()
        if arg not in ("on", "off"):
            print(self.p(f"上下文回填: {'开' if self.use_history else '关'}", "yellow"))
            return
        self.use_history = arg == "on"
        tip = ("已开启 —— 每轮会重算前文，首字延迟会变长" if self.use_history
               else "已关闭 —— 每轮独立，最快")
        print(self.p(tip, "green"))

    def _cmd_clear(self) -> None:
        try:
            self.engine.clear_kv()
            print(self.p("KV cache 已清空", "green"))
        except EngineError as exc:
            print(self.p(f"失败: {exc}", "red"))

    def _cmd_reset(self) -> None:
        self.turns.clear()
        self.last_stats = None
        try:
            self.engine.clear_kv()
        except EngineError:
            pass
        print(self.p("对话记录 + KV cache 已清空", "green"))

    def _cmd_stats(self) -> None:
        s = self.last_stats
        if not s:
            print(self.p("还没有对话记录", "yellow"))
            return
        decode = max(s["total"] - s["ttft"], 1e-6)
        print(f"  首字延迟   {s['ttft']*1000:8.0f} ms")
        print(f"  总耗时     {s['total']:8.2f} s")
        print(f"  文本       {s['chars']:8d} 字符（约 {s['est_tokens']} token）")
        print(f"  生成速度   {s['chars']/decode:8.1f} 字符/秒"
              f"  ≈ {s['est_tokens']/decode:.1f} token/秒")
        print(f"  可用内存   {mem_available_mb():8d} MB")

    def _cmd_save(self, arg: str) -> None:
        if not self.turns:
            print(self.p("没有可保存的对话", "yellow"))
            return
        path = os.path.abspath(os.path.expanduser(
            arg or time.strftime("qwen35-%Y%m%d-%H%M%S.md")))
        lines = [
            "# Qwen3.5-0.8B 对话记录", "",
            f"- 图片：`{self.image}`",
            f"- 模型：Qwen3.5-0.8B-MIT W4A16",
            f"- 平台：Quectel Pi M2 (RK3576) NPU {NPU_CORES} 核",
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "", "---", "",
        ]
        for idx, (question, answer) in enumerate(self.turns, 1):
            lines += [f"## 第 {idx} 轮", "", f"**问：** {question}", "",
                      f"**答：**", "", answer.strip(), ""]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            print(self.p(f"已保存 → {path}（{len(self.turns)} 轮）", "green"))
        except OSError as exc:
            print(self.p(f"保存失败: {exc}", "red"))

    def handle_command(self, line: str) -> bool:
        """返回 False 表示要退出。"""
        parts = line[1:].split(None, 1)
        name = parts[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if name in ("quit", "exit", "q"):
            return False
        elif name in ("help", "h", "?"):
            print(HELP)
        elif name == "image":
            self._cmd_image(arg)
        elif name in ("tokens", "token", "t"):
            self._cmd_tokens(arg)
        elif name == "ctx":
            self._cmd_ctx(arg)
        elif name == "history":
            self._cmd_history(arg)
        elif name == "clear":
            self._cmd_clear()
        elif name == "reset":
            self._cmd_reset()
        elif name == "stats":
            self._cmd_stats()
        elif name == "save":
            self._cmd_save(arg)
        else:
            print(self.p(f"未知命令: /{name}   （/help 看列表）", "red"))
        return True

    # -- 组装提示词 ------------------------------------------------
    def build_prompt(self, question: str) -> tuple[str, int]:
        """返回 (提示词, 因超限被丢弃的轮数)。"""
        if not self.use_history or not self.turns:
            return f"<image>{question}", 0

        budget = self.max_ctx - IMAGE_TOKENS - self.max_new - 32
        kept, dropped = [], 0
        for q, a in reversed(self.turns):
            block = f"用户：{q}\n助手：{a.strip()}\n"
            if est_tokens(
                    "".join(f"用户：{x}\n助手：{y.strip()}\n" for x, y in kept) + block
            ) > budget:
                dropped += 1
                continue
            kept.insert(0, (q, a))
        if not kept:
            return f"<image>{question}", len(self.turns)

        ctx = "".join(f"用户：{q}\n助手：{a.strip()}\n" for q, a in kept)
        return (f"<image>以下是之前的对话，请结合图片继续回答：\n{ctx}"
                f"\n用户：{question}\n助手："), dropped

    # -- 主循环 ----------------------------------------------------
    def ask_and_print(self, question: str) -> None:
        question = PRESETS.get(question.strip(), question)
        prompt_text, dropped = self.build_prompt(question)
        if dropped:
            print(self.p(f"（上下文超限，丢弃 {dropped} 轮旧对话）", "dim"))

        if not self.engine.alive:
            print(self.p("引擎已重启，重新加载模型…", "dim"))
            try:
                self.engine.start()
            except EngineError as exc:
                print(self.p(f"启动失败: {exc}", "red"))
                return

        sys.stdout.write(self.p("AI  › ", "bold", "green"))
        sys.stdout.flush()

        def on_token(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        try:
            stats = self.engine.ask(prompt_text, on_token=on_token)
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            print(self.p("已中断（引擎已停止，下一问会自动重启）", "yellow"))
            self.engine.stop()
            return
        except EngineError as exc:
            print()
            print(self.p(f"出错: {exc}", "red"))
            self.engine.stop()
            return

        sys.stdout.write("\n")
        self.last_stats = stats
        if stats["chars"]:
            self.turns.append((question, stats["text"]))
            decode = max(stats["total"] - stats["ttft"], 1e-6)
            print(self.p(
                f"  ⏱ 首字 {stats['ttft']*1000:.0f}ms · "
                f"≈{stats['est_tokens']/decode:.1f} tok/s · "
                f"{stats['chars']} 字 · 内存 {mem_available_mb()}MB", "dim"))

    def run(self) -> int:
        self._header()
        print(self.p("加载模型…（约 7 秒）", "dim"))
        try:
            self.engine.start()
            print(self.p(f"就绪，加载耗时 {self.engine.load_secs:.1f}s\n", "green"))
        except EngineError as exc:
            print(self.p(f"启动失败: {exc}", "red"))
            return 1

        try:
            while True:
                try:
                    line = input(self._prompt_text()).strip()
                except KeyboardInterrupt:
                    print()
                    continue
                except EOFError:
                    print()
                    break

                if not line:
                    continue
                if line.startswith("!"):
                    cmd = line[1:].strip()
                    if cmd:
                        # 等价于直接跑 shell（无沙箱）；至少把退出码告诉用户
                        status = os.system(cmd)
                        try:
                            rc = os.waitstatus_to_exitcode(status)
                        except ValueError:
                            rc = status
                        if rc != 0:
                            print(self.p(f"  退出码 {rc}", "dim"))
                    continue
                if line.startswith("/"):
                    if not self.handle_command(line):
                        break
                    continue

                self.ask_and_print(line)
        finally:
            self.engine.stop()
            print(self.p("再见。", "dim"))
        return 0


# ── 入口 ────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="chat.py",
        description="Qwen3.5-0.8B 多模态 CLI 对话（Quectel Pi M2 NPU）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  ./chat.py\n"
               "  ./chat.py -i /oem/photo.jpg -t 384\n"
               "  ./chat.py -p '图里有几个人？'\n",
    )
    ap.add_argument("-i", "--image", default=os.path.join(HERE, "demo.jpg"),
                    help="要分析的图片（默认 demo.jpg）")
    ap.add_argument("-t", "--tokens", type=int, default=256,
                    help="单轮最多生成 token 数（默认 256）")
    ap.add_argument("--ctx", type=int, default=MODEL_MAX_CTX,
                    help=f"上下文长度上限（默认 {MODEL_MAX_CTX}）")
    ap.add_argument("--history", action="store_true",
                    help="开启会话上下文回填（每轮更慢但能追问）")
    ap.add_argument("--no-color", action="store_true", help="关闭颜色")
    ap.add_argument("-p", "--prompt", help="一次性提问，回答完就退出")
    return ap.parse_args(argv)


def setup_readline() -> None:
    try:
        readline.read_history_file(HISTORY_FILE)
    except OSError:
        pass
    readline.set_history_length(200)

    commands = ["/image", "/tokens", "/ctx", "/history on", "/history off",
                "/clear", "/reset", "/stats", "/save", "/help", "/quit"]

    def completer(text: str, state: int) -> str | None:
        options = [c for c in commands if c.startswith(text)]
        return options[state] if state < len(options) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" ")
    readline.parse_and_bind("tab: complete")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.image and not os.path.isabs(args.image):
        args.image = os.path.join(os.getcwd(), args.image) \
            if os.path.exists(os.path.join(os.getcwd(), args.image)) else args.image

    setup_readline()
    session = Session(args)

    if args.prompt:
        try:
            session.engine.start()
        except EngineError as exc:
            print(f"启动失败: {exc}", file=sys.stderr)
            return 1
        try:
            session.ask_and_print(args.prompt)
        finally:
            session.engine.stop()
        return 0 if session.last_stats else 1

    try:
        code = session.run()
    finally:
        try:
            readline.write_history_file(HISTORY_FILE)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
