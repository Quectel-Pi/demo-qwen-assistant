#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rkllm-chat 与常驻推理服务的通信层。

把 Unix socket 上「一行一个 JSON」的协议包成一个对象：
后台线程收事件 -> 通过 GLib.idle_add 回到 GTK 主线程 -> 回调 UI。
这样 UI 永远不会被推理阻塞（一轮带图的推理要 7 秒以上）。
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import threading
import time
try:
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    _HAS_GLIB = True
except Exception:          # 允许脱离 GTK 做命令行自测
    _HAS_GLIB = False

DEFAULT_SOCK = "/run/rkllm-chat.sock"
# 写超时：ask() 跑在 GTK 主线程，socket 无超时的话服务端卡在 g_infer_mutex
# 时一次 write 就能把整个界面冻住。只给写用，接收线程已单独置超时。
SEND_TIMEOUT = 10.0


def _dispatch(fn, *args, _use_glib=True):
    """回到主线程执行；无 GLib 或调用方不需要时同步执行。"""
    if _use_glib and _HAS_GLIB:
        # 必须包 lamba 并返回 False：GLib.idle_add 把回调返回值当
        # 「要不要再调一次」。UI 回调如果 return 了真值（比如 _add_bubble
        # 返回 Bubble 对象），会被无限重复调用把界面刷爆。
        GLib.idle_add(lambda: (fn(*args), False)[1])
    else:
        fn(*args)


class ChatClient:
    """一条长连接 + 一个接收线程。

    所有回调都在主线程被调用：
        on_token(str)                 增量文本
        on_ttft(ms)                   首字延迟
        on_done(dict)                 本轮结束（含 ms/tokens/aborted）
        on_error(str)                 错误
        on_warn(str)                  非致命告警
        on_state(bool)                True=占用中(可 abort) / False=空闲
        on_image(dict)                新图上下文刷新/图像指纹诊断
    """

    def __init__(self, sock_path: str = DEFAULT_SOCK,
                 on_token=None, on_ttft=None, on_done=None,
                 on_error=None, on_warn=None, on_state=None,
                 threaded_dispatch: bool = True, on_image=None):
        self.sock_path = sock_path
        self.on_token = on_token
        self.on_ttft = on_ttft
        self.on_done = on_done
        self.on_error = on_error
        self.on_warn = on_warn
        self.on_state = on_state
        self.on_image = on_image
        self.last_image = None
        # True: 回调经 GLib.idle_add 回 GTK 主线程（必须有 Gtk.main 在跑）
        # False: 在接收线程里直接调（命令行自测用）
        self.threaded_dispatch = threaded_dispatch

        self._sock = None
        self._file = None
        self._rx = None
        self._lock = threading.Lock()
        self._busy = False
        self._alive = False
        self._generation = 0

    # ── 连接 ────────────────────────────────────────────────────
    def connect(self, timeout: float = 3.0) -> bool:
        if self.connected:
            return True
        self.close()
        generation = self._generation
        s = None
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.sock_path)
            # 保持阻塞语义：同一个 fd 上的 reader 是 makefile 缓冲读，
            # 给 socket 设超时会在服务端停顿时抛 socket.timeout 并让缓冲对象失效，
            # 长连接的流式 token 就断了。写超时改用 select 轮询（见 _send）。
            s.settimeout(None)
            reader = s.makefile("rwb")
        except OSError as exc:
            if s is not None:
                s.close()
            self._d_for(generation, self.on_error,
                        "无法连接推理服务 %s: %s" % (self.sock_path, exc))
            return False
        with self._lock:
            if generation != self._generation:
                reader.close()
                s.close()
                return False
            self._sock, self._file = s, reader
            self._alive = True
            rx = threading.Thread(target=self._recv_loop,
                                  args=(s, reader, generation), daemon=True)
            self._rx = rx
        rx.start()
        return True

    def close(self):
        with self._lock:
            self._generation += 1
            sock, reader, rx = self._sock, self._file, self._rx
            was_busy = self._busy
            self._sock = self._file = self._rx = None
            self._alive = self._busy = False
        # shutdown wakes readline before closing the buffered file.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for handle in (reader, sock):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        if rx is not None and rx is not threading.current_thread() and rx.is_alive():
            rx.join(timeout=0.2)
        if was_busy:
            self._d(self.on_state, False)

    @property
    def connected(self) -> bool:
        return self._alive and self._sock is not None

    @property
    def busy(self) -> bool:
        return self._busy

    # ── 发送 ────────────────────────────────────────────────────
    def _send(self, obj: dict) -> bool:
        """带写超时发一行 JSON。

        为什么用 select 而不是 sock.settimeout()：超时是 fd 级的，这个 socket
        上的 `reader = s.makefile("rwb")` 会用无限阻塞读；一旦设了 sock 超时，
        服务端推理/停顿超过阈值时 readline() 会抛 socket.timeout，而且带
        makefile 的缓冲读在超时后内部状态不可再用 —— _recv_loop 会直接崩溃
        并误判连接断开，正在流式出字的这一轮就被白白丢掉。
        所以读路径完全不碰，只在写之前 select 轮询可写性，最多等
        SEND_TIMEOUT 秒。超时就把本轮判为失败返回，GTK 主线程不会冻住，
        长连接和流式收包行为与原来完全一致。
        """
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._lock:
                if not self._alive or self._file is None or self._sock is None:
                    return False
                try:
                    _, writable, _ = select.select([], [self._sock], [], SEND_TIMEOUT)
                except ValueError:
                    return False      # fd 已被 close() 收回
                if not writable:
                    raise TimeoutError(
                        "发送超时（%.0fs）：服务端可能正忙" % SEND_TIMEOUT)
                self._file.write(data)
                self._file.flush()
            return True
        except (OSError, ValueError) as exc:
            self.close()
            self._d(self.on_error, "发送失败: %s" % exc)
            return False

    def ask(self, prompt: str, image: str | None = None,
            keep_history: bool = True) -> bool:
        with self._lock:
            if self._busy or not self._alive:
                return False
            self._busy = True
        self._d(self.on_state, True)
        req = {"cmd": "ask", "prompt": prompt,
               "keep_history": 1 if keep_history else 0}
        if image:
            req["image"] = image
        ok = self._send(req)
        if not ok:
            self._busy = False
            self._d(self.on_state, False)
        return ok

    def _control(self, command: str, expected: str, timeout: float) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.sock_path)
                s.sendall((json.dumps({"cmd": command}) + "\n").encode())
                with s.makefile("rb") as reader:
                    response = json.loads(reader.readline())
                return isinstance(response, dict) and response.get("ev") == expected
        except (OSError, ValueError):
            return False

    def abort(self) -> bool:
        # 独立短连接，保证推理卡住时也能打断。
        return self._control("abort", "aborted", 2.0)

    def clear_history(self) -> bool:
        return self._control("clear", "cleared", 3.0)

    def ping(self) -> bool:
        return self._control("ping", "pong", 2.0)

    # ── 接收 ────────────────────────────────────────────────────
    def _recv_loop(self, sock, reader, generation):
        # Each receiver owns its transport. It must never read a replacement
        # connection's file or mark a new connection disconnected.
        try:
            while self._alive and generation == self._generation:
                try:
                    line = reader.readline()
                except (OSError, ValueError):
                    break
                if not line:
                    break
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                with self._lock:
                    if generation != self._generation:
                        break
                    kind = ev.get("ev")
                    if kind in ("done", "error"):
                        self._busy = False
                if kind == "tok":
                    self._d_for(generation, self.on_token, ev.get("t", ""))
                elif kind == "ttft":
                    self._d_for(generation, self.on_ttft, ev.get("ms", 0))
                elif kind == "done":
                    # Finish the old bubble before enabling the next input.
                    self._d_for(generation, self.on_done, ev)
                    self._d_for(generation, self.on_state, False)
                elif kind == "error":
                    self._d_for(generation, self.on_error, ev.get("msg", "unknown"))
                    self._d_for(generation, self.on_state, False)
                elif kind == "warn":
                    self._d_for(generation, self.on_warn, ev.get("msg", ""))
                elif kind == "image":
                    self._d_for(generation, self._image_event, ev)
        finally:
            for handle in (reader, sock):
                try:
                    handle.close()
                except OSError:
                    pass
            with self._lock:
                current = generation == self._generation
                was_busy = current and self._busy
                if current:
                    self._alive = self._busy = False
                    self._sock = self._file = self._rx = None
            if was_busy:
                self._d_for(generation, self.on_error, "推理服务连接中断")
                self._d_for(generation, self.on_state, False)

    # ── 主线程侧回调 ─────────────────────────────────────────────
    def _image_event(self, info):
        self.last_image = dict(info)
        if self.on_image is not None:
            self.on_image(info)

    def _d_for(self, generation, fn, *args):
        if fn is None:
            return
        def deliver():
            # Also fence callbacks already queued on GTK before a reconnect.
            if generation == self._generation:
                fn(*args)
        _dispatch(deliver, _use_glib=self.threaded_dispatch)

    def _d(self, fn, *args):
        self._d_for(self._generation, fn, *args)


# ── 命令行自测：python3 client.py "问题" [图片] ──────────────────────
if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else "你好"
    image = sys.argv[2] if len(sys.argv) > 2 else None

    # 回调跑在接收线程里，sys.exit() 只会结束那个线程，
    # 所以用 Event 通知主线程退出。
    finished = threading.Event()
    rc = [0]

    def tok(t):
        sys.stdout.write(t)
        sys.stdout.flush()

    def done(ev):
        print("\n-- %.0fms, %s tokens --" % (ev.get("ms", 0), ev.get("tokens")))
        finished.set()

    def err(m):
        print("ERR:", m)
        rc[0] = 1
        finished.set()

    c = ChatClient(on_token=tok, on_done=done, on_error=err,
                   threaded_dispatch=False)
    if not c.connect():
        sys.exit(1)
    if not c.ask(prompt, image):
        print("ask 发送失败")
        sys.exit(1)
    if not finished.wait(180):
        print("超时", file=sys.stderr)
        rc[0] = 124
    c.close()
    sys.exit(rc[0])
