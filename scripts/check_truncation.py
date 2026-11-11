#!/usr/bin/env python3
"""在 rkchat 进程内检测长文本气泡是否被截断。

原理：构造一条长文本气泡，读它实际分配到的宽度。
- 修复前 set_max_width_chars(70)：气泡宽度被卡在 ~560px
- 修复后：应能占到窗口宽度的大部分（>900px）

用法（设备上，需 DISPLAY/GDK_BACKEND=x11）:
    python3 check_truncation.py
"""
import os
import sys

sys.path.insert(0, "/data/rkllm-chat/ui/rkchat")
os.chdir("/data/rkllm-chat/ui/rkchat")
os.environ.setdefault("GDK_BACKEND", "x11")

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import rkchat

LONG = ("这是一段用于验证文字显示是否被截断的很长的中文文本。" * 12)


def pump(n=60):
    for _ in range(n):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        import time
        time.sleep(0.01)


def main():
    app = rkchat.ChatApp(sock="/run/rkllm-chat.sock",
                         dev=rkchat.camera.DEFAULT_DEV, fullscreen=True)
    app.win.resize(1280, 800)
    app.win.show_all()
    pump(120)

    b = app._add_bubble("ai", LONG)
    b.show_all()
    pump(240)

    win_w = app.win.get_allocated_width()
    frame_w = b.frame.get_allocated_width()
    label_w = b.label.get_allocated_width()
    lines = b.label.get_layout().get_line_count()
    text_len = len(b.get_text())

    max_chars = b.label.get_max_width_chars()

    print(f"window_width      = {win_w}")
    print(f"bubble_frame_width= {frame_w}")
    print(f"label_width       = {label_w}")
    print(f"layout_lines      = {lines}")
    print(f"text_len          = {text_len}")
    print(f"max_width_chars   = {max_chars}")
    print(f"width_ratio       = {frame_w / win_w:.2f}")

    # 判定：气泡应能用到窗口 70% 以上宽度，且长文本必须换行成多行
    ok_width = frame_w > win_w * 0.70
    ok_wrap = lines >= 3
    print(f"PASS_WIDTH={ok_width} PASS_WRAP={ok_wrap}")
    print("PASS" if (ok_width and ok_wrap) else "FAIL")
    return 0 if (ok_width and ok_wrap) else 1


if __name__ == "__main__":
    sys.exit(main())
