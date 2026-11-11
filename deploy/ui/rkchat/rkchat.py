#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rkchat —— Quectel Pi M2（RK3576）板端 Qwen3.5 桌面交互应用（GTK3）

布局（按 1920x1080 外接 HDMI 设计）：

  ┌──────────────────────────────────────────────────────────┐
  │ 顶部状态栏：标题 | 连接 | NPU | 内存 | 首字延迟 | 生成速度 │
  ├──────────────────────────────────────────────────────────┤
  │                                                          │
  │  对话区（可滚动）                                          │
  │    ┌──────────┐  用户气泡                                 │
  │    └──────────┘                                           │
  │    AI 气泡（流式逐字追加）                                 │
  │    meta: 首字 3.5s · 68 tok · 2.0 tok/s                   │
  │                                                          │
  ├──────────────────────────────────────────────────────────┤
  │ 摄像头预览（点亮时出现，默认隐藏）                          │
  ├──────────────────────────────────────────────────────────┤
  │ [摄像头] [语音] [清空]   输入框 …………          [发送/停止] │
  └──────────────────────────────────────────────────────────┘

交互：
  Enter 发送，Shift+Enter 换行
  摄像头按钮点亮后预览出画面，此时发送会自动抓帧并带图提问
  语音按钮点亮后进入免提：说完自动识别并提问；AI 回答完自动朗读
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tempfile
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import camera          # noqa: E402
import client as chatc  # noqa: E402
import voice           # noqa: E402

APP_TITLE = "Quectel Pi M2 智能助手"
WINDOW_TITLE = "rkchat"

# 气泡最大宽度占对话区可视宽度的比例。
# 为什么不用 set_max_width_chars：那是按 'M' 字符宽度(约9px)算的，
# 70 字符≈630px，而中文单字约 17px，一行只放得下约 37 个汉字，
# 在 1280 宽的屏上只用了不到一半横向空间，长句被压成很多行且右侧大片留白。
# 改成按像素随窗口宽度自适应，见 Bubble.set_avail_width()。
BUBBLE_WIDTH_RATIO = 0.78

# 预览区高度：摄像头是 640x480（4:3），按 360 宽显示
PREVIEW_H = 300
PREVIEW_W = int(PREVIEW_H * 4 / 3)

# 状态栏刷新间隔（毫秒）
STAT_INTERVAL = 1000


# ── 小工具 ───────────────────────────────────────────────────────
def read_mem() -> tuple[int, int]:
    """返回 (已用 MiB, 总量 MiB)。"""
    total = avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) // 1024
    except OSError:
        pass
    return (total - avail, total)


def npu_load() -> int | None:
    """读 NPU 占用率（%）。RKNPU 驱动在 /sys/kernel/debug/rknpu/load。"""
    for path in ("/sys/kernel/debug/rknpu/load",
                 "/sys/kernel/debug/rknpu/load_npu"):
        try:
            with open(path) as f:
                txt = f.read()
        except OSError:
            continue
        # 格式形如 "NPU load:  Core0: 12%, Core1:  8%,"
        nums = []
        for token in txt.replace(",", " ").split():
            t = token.rstrip("%")
            if t.isdigit():
                nums.append(int(t))
        if nums:
            return max(nums)
    return None


def make_pixbuf_from_rgb(arr, w: int, h: int) -> GdkPixbuf.Pixbuf | None:
    """numpy (h,w,3) uint8 -> GdkPixbuf。复用同一块内存，不拷两次。"""
    try:
        data = arr.tobytes()
        return GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB,
            False, 8, w, h, w * 3)
    except Exception:
        return None


# ── 消息气泡 ─────────────────────────────────────────────────────
class Bubble(Gtk.Box):
    """一条消息。role = user / ai / error / system"""

    def __init__(self, role: str, text: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.role = role

        self.label = Gtk.Label()
        self.label.set_xalign(0.0)
        self.label.set_yalign(0.0)
        self.label.set_line_wrap(True)
        self.label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_selectable(True)
        # 宽度由外部按像素设定（见 set_avail_width），不用字符数限制。
        # get_width_chars 默认为 -1，配合 wrap 就是「自然宽度 + 不超上限」。
        self._avail_px = 0
        self.label.set_text(text)
        # GTK3 CSS 不支持 line-height，行间距只能用 Pango layout 的 spacing 设。
        # 中文长文本行距不足会糊成一团，正文加到 3px。
        self._setup_line_spacing()

        style = {"user": "bubble-user", "ai": "bubble-ai",
                 "error": "bubble-err", "system": "bubble-sys"}.get(role, "bubble-ai")
        # GtkEventBox 上挂 CSS 的 padding 在 GTK3 里不生效（实测气泡左右
        # padding 只有 1px，文字贴着边框）。所以外层 EventBox 只负责背景色
        # 和圆角，真实的内边距由一个内部 Box 的 border-width 来提供。
        self.frame = Gtk.EventBox()
        self.frame.get_style_context().add_class(style)
        pad = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        pad.get_style_context().add_class("bubble-pad")
        pad.add(self.label)
        self.frame.add(pad)

        # 用户靠右，其余靠左
        self.row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        if role == "user":
            self.row.pack_end(self.frame, False, False, 0)
        else:
            self.row.pack_start(self.frame, False, False, 0)
        self.pack_start(self.row, False, False, 0)

        self.meta = Gtk.Label()
        self.meta.set_xalign(0.0 if role != "user" else 1.0)
        self.meta.get_style_context().add_class("meta")
        self.meta.set_no_show_all(True)
        self.pack_start(self.meta, False, False, 0)

    def _setup_line_spacing(self):
        """给气泡正文加行间距（GTK3 CSS 的 line-height 无效）。"""
        try:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_rise_new(0))
            spacing = 3 if self.role in ("ai", "user") else 1
            attrs.insert(Pango.attr_letter_spacing_new(0))
            self.label.set_attributes(attrs)
            # 真正的行距：改 layout 的 spacing（单位 Pango 单位 = 1/1024 px）
            layout = self.label.get_layout()
            if layout is not None:
                layout.set_spacing(spacing * Pango.SCALE)
        except Exception:
            pass

    def set_avail_width(self, px: int):
        """按对话区可用像素宽度约束气泡最大宽度（取代写死的 70 字符）。

        GtkLabel 开了 line-wrap 后，自然宽度 = 不换行时的整行宽度，
        容器用 pack_start(expand=False) 时会直接给它自然宽度 → 长句把窗口撑爆。
        所以才要 max-width-chars 封顶。但字符数不等于像素：
        GTK 按 'M'(约 9px) 换算，而中文约 17px/字，70 字符只剩一半可用宽度。
        这里改成由调用方传入实测像素宽，内部再换算成等价的字符上限。
        """
        px = max(200, int(px * BUBBLE_WIDTH_RATIO))
        if px == self._avail_px:
            return
        self._avail_px = px
        # +1 是为了不让取整误差把最后一行挤掉
        self.label.set_max_width_chars(max(12, px // 9 + 1))

    def append(self, chunk: str):
        self.label.set_text(self.label.get_text() + chunk)

    def set_text(self, text: str):
        self.label.set_text(text)

    def get_text(self) -> str:
        return self.label.get_text()

    def set_meta(self, text: str):
        self.meta.set_text(text)
        self.meta.show()

    def add_thumb(self, path: str):
        """在气泡右边加一张缩略图。"""
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 220, 165, True)
        except Exception:
            return
        img = Gtk.Image.new_from_pixbuf(pb)
        img.get_style_context().add_class("thumb")
        self.row.pack_start(img, False, False, 8)


# ── 主窗口 ───────────────────────────────────────────────────────
class ChatApp:
    def __init__(self, sock: str = chatc.DEFAULT_SOCK,
                 dev: str = camera.DEFAULT_DEV, fullscreen: bool = False,
                 keyboard: str = "system"):
        if keyboard not in ("system", "builtin"):
            raise ValueError("keyboard must be 'system' or 'builtin'")
        self.keyboard = keyboard
        self._ui_thread_id = threading.get_ident()
        self.sock_path = sock
        self.dev = dev
        self.fullscreen = fullscreen

        self.cam = camera.PreviewStream(dev=dev)
        self.voice_io = voice.VoiceIO()
        self.voice_mode = False        # 免提是否开启（麦克风录入 + 自动朗读）
        # 自动朗读开关：与 voice_mode 解耦。开启后，无论文字提问还是
        # 语音提问，AI 回答都会逐句朗读。想静音时点「朗读」按钮切换。
        self.auto_speak = True
        self.cam_mode = False          # 摄像头是否点亮
        self._cam_closing = False
        self._capture_pending = False
        self._capture_token = 0
        self._capture_worker = None
        self._snapshot_dir = tempfile.TemporaryDirectory(
            prefix="rkchat-frames-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        self._snapshot_paths = []
        self._last_snapshot = None
        self._last_visual_event = None
        self._active_user_bubble = None
        self.voice_thread: threading.Thread | None = None
        self.speak_thread: threading.Thread | None = None

        # 对话历史：[{"q":用户话, "a":模型答复或 None}]
        # a 为 None 表示这轮被打断。
        # 服务端能自己维护 KV，但一旦发生中断就只能整段清 cache，
        # 所以客户端必须留一份拷贝，下轮重放（实测每 237 字约 +1.5s）。
        self.history: list[dict] = []
        self._need_replay = False      # 服务端上下文已失效，下轮要带历史
        self._replay_used = False
        self.replay_budget = 900       # 重放历史最多这么多字（约 +6s prefill）

        self.cur_bubble: Bubble | None = None
        # 最后一个已收尾的 AI 气泡。朗读时被打断要用它来标“已中断”，
        # 因为此时 cur_bubble 已经是 None（回答早就生成完了）。
        self.last_ai_bubble: Bubble | None = None
        self.round_t0 = 0.0
        self.ttft_ms = 0
        self.last_speed = 0.0
        self._voice_busy = False
        self._voice_loop_thread: threading.Thread | None = None
        self._interrupt_reason: str | None = None
        self.vkb = None                # 虚拟键盘，_build_input 之后才创建
        self._stop_speak: threading.Event | None = None   # 让朗读线程尽快停下
        # 流式播报：token 一凑成整句就合成朗读，不等整段答案
        self._stream_spk: voice.StreamSpeaker | None = None
        self._speak_buf = ""           # 已收到但还没凑成整句的尾巴
        # 第几轮（供调试/日志定位）。旧轮回调是靠 _start_stream_speaker()
        # 换掉 _stream_spk 对象拦的，不靠这个计数器。
        self._speak_seq = 0
        # 窗口销毁后，GLib.timeout_add 的回调还会继续被调用；
        # 那时控件已失效，再碰就会段错误（实测销毁后 100% segfault）。
        self._alive = True

        self._build_ui()
        if self.keyboard == "system":
            # GtkEntry 首次触摸未聚焦时可能只获取焦点；预先聚焦，让首次
            # 触摸即可请求 GNOME 输入面板，而不是需要再点一次。
            GLib.idle_add(self._focus_system_entry)

        # 语音模型加载要 10 秒左右，放后台预先加载，
        # 否则用户第一次点「语音」要白等。失败不影响文字/看图。
        self.preload_thread = threading.Thread(
            target=self._preload_voice, daemon=True)
        self.preload_thread.start()

        # 推理客户端
        self.cli = chatc.ChatClient(
            sock_path=sock,
            on_token=self._on_token,
            on_ttft=self._on_ttft,
            on_done=self._on_done,
            on_error=self._on_error,
            on_warn=self._on_warn,
            on_state=self._on_state,
            on_image=self._on_image,
        )

    # ── UI 构建 ─────────────────────────────────────────────────
    @staticmethod
    def _screen_size() -> tuple[int, int]:
        """当前显示器的分辨率。拿不到就退回 1280x800（这块板子的 DSI 屏）。"""
        try:
            disp = Gdk.Display.get_default()
            if disp is not None:
                mon = disp.get_primary_monitor() or disp.get_monitor(0)
                if mon is not None:
                    geo = mon.get_geometry()
                    if geo.width > 0 and geo.height > 0:
                        return geo.width, geo.height
        except Exception:
            pass
        return 1280, 800

    def _build_ui(self):
        self.win = Gtk.Window(title=WINDOW_TITLE)
        # 按实际屏幕尺寸设默认值，不要硬编码 1920x1080：
        # 这块板子当前是 DSI-1 1280x800，硬编码会让 GTK 按 1920x1080 布局，
        # 再被 Xwayland 缩放，导致控件坐标"超出窗口"、键盘被裁掉。
        sw, sh = self._screen_size()
        self.win.set_default_size(sw, sh)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        if self.fullscreen:
            # kiosk 场景：铺满整屏，无边框标题栏
            self.win.fullscreen()
        self.win.connect("destroy", self._on_destroy)
        self.win.connect("key-press-event", self._on_key)
        # 窗口大小变化（全屏/旋转/接外屏）时，让气泡宽度跟着变，
        # 否则启动那一刻算出的宽度会被一直沿用。
        self.win.connect("size-allocate", self._on_size_allocate)

        css = Gtk.CssProvider()
        css_path = os.path.join(HERE, "style.css")
        try:
            css.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception as e:
            print("样式加载失败:", e, file=sys.stderr)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.win.add(root)

        root.pack_start(self._build_topbar(), False, False, 0)
        root.pack_start(self._build_chat(), True, True, 0)

        # ── 摄像头预览区 ──
        # 为什么用 Gtk.Revealer：
        #   只用 hide() + pack_start(expand=True) 时，隐藏的控件仍然参与
        #   空间分配 —— 实测预览区隐藏后仍占 483px，对话区名义 662px 但
        #   实际只有顶部 179px 能画内容，后面全是空白。
        #   Revealer 在 reveal_child=False 时会把子控件的 natural size 归零，
        #   彻底不占位（GTK3 专为这个场景提供的控件）。
        # 也不能用 set_no_show_all(True)：它会让 show_all() 对它永久失效，
        # 连子控件都显示不出来（之前就踩过这个坑）。
        self.preview_box = self._build_preview()
        self.preview_revealer = Gtk.Revealer()
        self.preview_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_UP)
        self.preview_revealer.set_transition_duration(180)
        self.preview_revealer.add(self.preview_box)
        self.preview_revealer.set_reveal_child(False)
        # 注意 expand 必须为 False（实测对比）：
        #   expand=True, fill=True  -> 收起后仍占 220px（剩余空间会被它拿走）
        #   expand=False            -> 收起后 1px，对话区拿满剩余高度
        root.pack_start(self.preview_revealer, False, False, 0)

        # 默认只用 GNOME 系统键盘，不构造、不弹出应用自带键盘。
        # 仅显式指定 --keyboard builtin 时才启用后备键盘。
        if self.keyboard == "builtin":
            import vkbd
            self.vkb = vkbd.VirtualKeyboard(
                on_text=self._vkb_text,
                on_backspace=self._vkb_backspace,
                on_enter=self._on_send_clicked,
                on_hide=self._hide_vkb,
            )
            root.pack_start(self.vkb.widget, False, False, 0)
            self.vkb.widget.set_no_show_all(True)
            self.vkb.widget.hide()

        root.pack_start(self._build_input(), False, False, 0)

        self.win.show_all()
        # show_all() 会把 revealer 及其子控件都置为 visible，
        # 所以要在它之后再显式收起一次。
        self._set_preview_visible(False)

        # 定时刷新状态栏 / 预览
        GLib.timeout_add(STAT_INTERVAL, self._tick_stats)
        GLib.timeout_add(66, self._tick_preview)     # ~15fps 刷新预览

    def _build_topbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("topbar")

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        t = Gtk.Label(label=APP_TITLE)
        t.set_xalign(0.0)
        t.get_style_context().add_class("app-title")
        s = Gtk.Label(label="Qwen3.5-0.8B W4A16 · Quectel Pi M2 NPU ×2")
        s.set_xalign(0.0)
        s.get_style_context().add_class("app-sub")
        left.pack_start(t, False, False, 0)
        left.pack_start(s, False, False, 0)
        bar.pack_start(left, False, False, 0)

        spacer = Gtk.Box()
        bar.pack_start(spacer, True, True, 0)

        self.lbl_conn = self._stat_label("● 连接中", "stat-warn")
        self.lbl_npu = self._stat_label("NPU --", "stat")
        self.lbl_mem = self._stat_label("内存 --", "stat")
        self.lbl_ttft = self._stat_label("首字 --", "stat")
        self.lbl_speed = self._stat_label("速度 --", "stat")
        for w in (self.lbl_conn, self.lbl_npu, self.lbl_mem,
                  self.lbl_ttft, self.lbl_speed):
            bar.pack_start(w, False, False, 0)
        return bar

    @staticmethod
    def _stat_label(text: str, cls: str) -> Gtk.Label:
        lb = Gtk.Label(label=text)
        lb.get_style_context().add_class(cls)
        return lb

    def _build_chat(self) -> Gtk.Widget:
        self.chat_scroll = Gtk.ScrolledWindow()
        self.chat_scroll.get_style_context().add_class("chat-scroll")
        self.chat_scroll.set_policy(Gtk.PolicyType.NEVER,
                                    Gtk.PolicyType.AUTOMATIC)
        self.chat_scroll.set_vexpand(True)

        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                spacing=14)
        self.chat_box.get_style_context().add_class("chat-box")
        # 不要用 set_valign(START)：ScrolledWindow 里的 box 一旦 valign 不是
        # FILL，后续追加的子控件拿不到尺寸协商，气泡 label 会停在 1x1 @(-1,-1)，
        # 文字就完全不渲染（已实测复现）。内容少时本来就不会被拉伸，
        # 因为 pack_start(..., False, False, 0) 已保证子控件只取自然高度。
        self.chat_scroll.add(self.chat_box)

        self._add_bubble("system",
                         "已就绪。输入文字提问，或点亮「摄像头」让 AI 看图，"
                         "点亮「语音」进入免提对话。")
        return self.chat_scroll

    def _build_preview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.get_style_context().add_class("preview-frame")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.lbl_preview = Gtk.Label(label="摄像头预览")
        self.lbl_preview.get_style_context().add_class("preview-label")
        self.lbl_preview.set_xalign(0.0)
        head.pack_start(self.lbl_preview, False, False, 8)

        self.lbl_voice = Gtk.Label(label="")
        self.lbl_voice.get_style_context().add_class("mic-live")
        head.pack_start(self.lbl_voice, False, False, 12)
        box.pack_start(head, False, False, 0)

        self.preview_img = Gtk.Image()
        # 不能只用 set_size_request 固定 640x480：在小屏（如 1280x800）上
        # 外层 Box 不 expand 时会被压成 1px，preview_img 拿到 1x1 @(-1,-1)，
        # 表现就是"点了摄像头但看不到画面"。这里同时设最小尺寸并允许扩展。
        self.preview_img.set_size_request(PREVIEW_W, PREVIEW_H)
        self.preview_img.set_hexpand(True)
        self.preview_img.set_vexpand(True)
        wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        wrap.pack_start(self.preview_img, True, True, 12)
        box.pack_start(wrap, True, True, 6)
        # 注意：不要在外层 box 上 set_size_request 固定高度。
        # 旧代码写 box.set_size_request(-1, PREVIEW_H + 48) = 348px，导致
        # 即使 Revealer 收起（alloc 只有 1px），布局协商时仍会把这 348px
        # 算进需求，把对话区挤成 218px、并在 inputbar 上方留 180px 空白。
        # 高度交给内部 preview_img 的 size_request（300px）自然决定即可。
        return box

    def _build_input(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.get_style_context().add_class("inputbar")

        self.btn_cam = Gtk.Button(label="摄像头")
        self.btn_cam.get_style_context().add_class("btn-cam")
        self.btn_cam.connect("clicked", self._on_cam_clicked)
        bar.pack_start(self.btn_cam, False, False, 0)

        self.btn_voice = Gtk.Button(label="语音")
        self.btn_voice.connect("clicked", self._on_voice_clicked)
        bar.pack_start(self.btn_voice, False, False, 0)

        # 自动朗读开关：默认开。点一下静音，再点恢复。
        self.btn_speak = Gtk.Button(label="朗读中")
        self.btn_speak.get_style_context().add_class("btn-speak-on")
        self.btn_speak.connect("clicked", self._on_speak_clicked)
        bar.pack_start(self.btn_speak, False, False, 0)

        self.btn_clear = Gtk.Button(label="清空")
        self.btn_clear.connect("clicked", self._on_clear_clicked)
        bar.pack_start(self.btn_clear, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("entry")
        self.entry.set_placeholder_text("说点什么…（点这里输入）")
        self.entry.connect("activate", self._on_send_clicked)
        # 原生 Wayland GtkEntry 向合成器请求输入面板；IBus 在 GNOME 内部
        # 处理拼音。不要用不存在的 ATK API，也不要同时弹两套键盘。
        self.entry.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        if self.keyboard == "builtin":
            self.entry.connect("focus-in-event", self._on_entry_focus)
            self.entry.connect("button-press-event", self._on_entry_focus)
        bar.pack_start(self.entry, True, True, 0)

        self.btn_send = Gtk.Button(label="发送")
        self.btn_send.get_style_context().add_class("btn-primary")
        self.btn_send.connect("clicked", self._on_send_clicked)
        bar.pack_start(self.btn_send, False, False, 0)
        return bar

    # ── 对话区操作 ──────────────────────────────────────────────
    def _add_bubble(self, role: str, text: str = "",
                    image: str | None = None) -> Bubble:
        b = Bubble(role, text)
        b.set_avail_width(self._chat_avail_width())
        if image:
            b.add_thumb(image)
        self.chat_box.pack_start(b, False, False, 0)
        b.show_all()
        if not text:
            b.meta.hide()
        self._scroll_to_bottom()
        return b

    def _chat_avail_width(self) -> int:
        """对话区当前可用像素宽度。窗口未完成布局时用屏宽兑底。"""
        w = self.chat_scroll.get_allocated_width()
        if w <= 1:
            sw, _ = self._screen_size()
            w = sw
        # 减去 .chat-box 的左右 padding（CSS 里 26px 各边）与滚动条
        return max(240, w - 26 * 2 - 12)

    def _apply_bubble_width(self):
        """窗口尺寸变化时，把新宽度同步给所有已有气泡。"""
        if not self._alive:
            return False
        avail = self._chat_avail_width()
        for child in self.chat_box.get_children():
            if isinstance(child, Bubble):
                child.set_avail_width(avail)
        return False

    def _scroll_to_bottom(self):
        def do_scroll():
            if not self._alive:
                return False
            adj = self.chat_scroll.get_vadjustment()
            if adj is None:      # 窗口刚建/刚销毁时可能还没有 adjustment
                return False
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        # 等一帧，让新气泡完成布局再滚，否则滚不到位
        GLib.idle_add(do_scroll)

    # ── 发送 / 中断 ─────────────────────────────────────────────
    def _stop_reading(self):
        """Cancel this round's audio and discard its buffered text."""
        self._speak_buf = ""
        if self._stop_speak is not None:
            self._stop_speak.set()
        if self._stream_spk is not None:
            self._stream_spk.stop()
            self._stream_spk = None

    def _is_speaking(self) -> bool:
        """是否正在朗读（流式播报器 或 旧的整段朗读线程）。"""
        # 流式播报：只要播放线程还活着就算在读
        sp = self._stream_spk
        if sp is not None and sp.busy:
            return True
        th = self.speak_thread
        if th is None or not th.is_alive():
            return False
        if self._stop_speak is not None and self._stop_speak.is_set():
            return False
        return True

    # ── 键盘 ────────────────────────────────────────────────────
    def _focus_system_entry(self):
        if self._alive and self.entry.get_sensitive():
            self.entry.grab_focus()
        return False

    def _on_entry_focus(self, *_):
        """点输入框（或触摸让它获得焦点）时弹出键盘。"""
        if self.vkb is not None and not self.vkb.visible:
            self._show_vkb()
        return False

    def _show_vkb(self):
        if self.vkb is None:
            return
        # 按窗口高度选按键尺寸档位（紧凑/标准）
        wh = self.win.get_size()[1] or 800
        self.vkb.fit_to_height(wh)
        self.vkb.show()
        self.vkb.widget.show_all()
        # 键盘遮住了下半屏，把最新消息滚到可见区域
        self._scroll_to_bottom()
        # show_all 会把候选栏也显示出来，未输入拼音时应隐藏
        if not self.vkb.pinyin:
            self.vkb.cand_box.hide()
        self.vkb.refresh_hint()
        self._scroll_to_bottom()

    def _hide_vkb(self):
        if self.vkb is not None:
            self.vkb.hide()
            self._scroll_to_bottom()

    def _vkb_text(self, s: str):
        """键盘输入一个字符/汉字，插到光标处。"""
        # 只在焦点丢时才抢焦点：每次按键都 grab_focus() 会重复
        # 触发 focus-in-event→_show_vkb 的检查链路，实测按键手感发滞。
        if not self.entry.has_focus():
            self.entry.grab_focus()
        pos = self.entry.get_position()
        if pos < 0:
            pos = len(self.entry.get_text())
        self.entry.insert_text(s, pos)
        self.entry.set_position(pos + len(s))

    def _vkb_backspace(self):
        if not self.entry.has_focus():
            self.entry.grab_focus()
        pos = self.entry.get_position()
        txt = self.entry.get_text()
        if pos <= 0 or not txt:
            return
        # 删一个字符（中文一个字也算一个）
        self.entry.delete_text(pos - 1, pos)
        self.entry.set_position(pos - 1)

    def _on_send_clicked(self, *_):
        text = self.entry.get_text().strip()

        # 按钮语义：
        #   - 输入框有内容 -> 发送（即使正在朗读，也允许打断后立刻提新问题）
        #   - 输入框为空   -> 当作「停止」，中断生成/朗读
        # 之前只要 busy 或正在朗读就一律打断，导致"想接着问下一句"
        # 被当成停止，输入的内容被丢掉。
        if text:
            pass                                  # 下面正常发送
        elif self._capture_pending:
            self._cancel_capture()
            self._add_bubble("system", "已取消本轮拍照，问题未发送。")
            return
        elif self.cli.busy or self._is_speaking():
            self._interrupt("手动停止")
            self.btn_send.set_label("中断中…")
            return
        else:
            return
        self.entry.set_text("")
        if self.vkb is not None:
            self._hide_vkb()
        # 正在朗读时插入新问题：先掐掉当前播报，避免两段声音叠在一起
        if self._is_speaking():
            if self._stream_spk is not None:
                self._stream_spk.stop()
            try:
                if self._stop_speak is not None:
                    self._stop_speak.set()
            except Exception:
                pass
            subprocess.run(["pkill", "-x", "aplay"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        self._submit(text)

    def _submit(self, text: str):
        """Single request gate for both typed and speech questions."""
        if not self._alive:
            return
        if self._capture_pending or self.cli.busy or self.cur_bubble is not None:
            self._on_warn("上一轮尚未结束，请先停止或等待回答完成")
            # 调用方（_on_send_clicked）已经清空输入框了。这里不回填的话，
            # 用户刚打的一段话会因为撞上 busy 窗口而永久丢失。
            self.entry.set_text(text)
            self.entry.set_position(-1)      # 光标放到末尾，方便接着改
            return
        if self.speak_thread is not None and not self.speak_thread.is_alive():
            self.speak_thread = None
            self._stop_speak = None
        if self.cam_mode:
            self._begin_capture(text)
        else:
            self._send_round(text, None)

    def _send_round(self, text: str, image: str | None):
        bubble = self._add_bubble("user", text, image=image)
        self._active_user_bubble = bubble
        if image:
            bubble.show_all()
            if self._last_snapshot:
                bubble.set_meta("本轮快照 · 帧 %d · 采集延迟 %.0fms" % (
                    self._last_snapshot["seq"], self._last_snapshot["age_ms"]))
        self.history.append({"q": text, "a": None, "img": bool(image)})
        self._start_ask(text, image)

    def _begin_capture(self, text: str):
        # Never launch a second ffmpeg against an already-open exclusive device.
        self._capture_pending = True
        self._capture_token += 1
        token, epoch = self._capture_token, self.cam.epoch
        _, minimum_seq = self.cam.latest()
        fd, path = tempfile.mkstemp(prefix="frame-", suffix=".bmp", dir=self._snapshot_dir.name)
        os.close(fd)
        self._set_controls_busy(True)
        self.lbl_preview.set_text("正在获取本轮新画面…")
        def capture():
            try:
                info = self.cam.fresh_snapshot(path, min_seq=minimum_seq, epoch=epoch, timeout=1.5)
            except Exception:
                info = None
            GLib.idle_add(self._capture_ready, token, text, path, info)
        self._capture_worker = threading.Thread(target=capture, daemon=True)
        self._capture_worker.start()

    @staticmethod
    def _remove_snapshot(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _capture_ready(self, token, text, path, info):
        if not self._alive or token != self._capture_token or not self._capture_pending:
            self._remove_snapshot(path)
            return False
        self._capture_pending = False
        if info is None or not self.cam_mode:
            self._remove_snapshot(path)
            self._set_controls_busy(False)
            self._add_bubble("error", "未取得新画面，本轮没有发送。请等待预览更新后重试。")
            return False
        self._last_snapshot = dict(info, path=path)
        self._snapshot_paths.append(path)
        while len(self._snapshot_paths) > 8:
            self._remove_snapshot(self._snapshot_paths.pop(0))
        self.lbl_preview.set_text("摄像头预览")
        self._send_round(text, path)
        return False

    def _cancel_capture(self):
        self._capture_token += 1
        self._capture_pending = False
        self._set_controls_busy(self.cli.busy)

    def _on_image(self, info):
        self._last_visual_event = dict(info)
        if info.get("kv_reset"):
            # A new scene replaces earlier visual history; text-only follow-ups
            # still use this most recent scene until another image is sent.
            self.history[:] = self.history[-1:]
            self._need_replay = False
            if self._active_user_bubble is not None:
                self._active_user_bubble.set_meta("本轮新画面已读取 · 旧画面上下文已刷新")

    def _replay_prefix(self) -> str:
        """把已完成的历史拼成纯文本前缀。

        只用于服务端 context 被清掉之后的那一轮（中断/清空/重启）。

        格式很重要：实测把历史写成“用户：…\n助手：…”这种标签式，
        模型会把整段当成一条用户消息，完全不把它当上下文（答不上名字）。
        改成“我说…你答…”的叙述式就能正确利用（实测名字+城市都对）。
        带图的轮次不重放图片内容（要重跑 vision encoder，太慢），
        只保留文字。
        """
        lines: list[str] = []
        for h in self.history[:-1]:
            a = h.get("a")
            if not a:                 # 被打断 / 无回答的轮次直接跳过
                continue
            q = h["q"]
            if h.get("img"):
                q = "（并发来一张图片）" + q
            lines.append("我说：%s 你答：%s" % (q, a))
        if not lines:
            return ""
        # 太长时只保留最近的（prefill 开销随字数线性上涨，
        # 实测 237 字 +1.5s、948 字 +6s）
        out = "\n".join(lines)
        if len(out) > self.replay_budget:
            out = out[-self.replay_budget:]
            p = out.find("\n")
            if p > 0:
                out = out[p + 1:]
        return ("下面是我们之前的对话记录：\n" + out +
                "\n现在请回答我的新问题，不要复述上面内容。")

    def _start_ask(self, text: str, img: str | None):
        # 有图要在 prompt 里带 <image>，服务端据此插入图像占位
        prompt = text if not img else (
            text if "<image>" in text else "<image>" + text)
        # 服务端上下文失效时，把历史重放在本轮 prompt 里。
        # 注意：历史文本里可能出现模型自己写的“<image>”，
        # 那会让服务端误走多模态分支（本轮没图→拿不到 embed），所以剔掉。
        if img:
            # Never prepend descriptions of an old picture to a new scene.
            self._need_replay = False
        elif self._need_replay:
            pre = self._replay_prefix()
            pre = pre.replace("<image>", "[图片]")
            if pre:
                prompt = pre + "\n" + prompt
            self._need_replay = False
        # 开启自动朗读：开一轮流式播报器，token 一凑成句就开始朗读，
        # 不必等整段答案生成完（之前要等全部出完才开始播，长回答差十几秒）
        self._speak_seq += 1        # 仅作日志/排障用的轮次标记
        if self.auto_speak:
            # A typed question cancels any in-progress dictation too. Otherwise
            # the recorder can submit a stale partial utterance over this round.
            self.voice_io.stop_listening()
            self._start_stream_speaker()
        self._speak_buf = ""
        self.cur_bubble = self._add_bubble("ai", "")
        self.round_t0 = time.time()
        self.ttft_ms = 0
        # 本地先切成忙碌态，不等服务端回 idle_add，避免按钮滞后一帧
        self._set_controls_busy(True)
        if not self.cli.connected:
            if not self.cli.connect():
                self._on_error("无法连接推理服务 /run/rkllm-chat.sock")
                return
        if not self.cli.ask(prompt, img):
            self._on_error("发送失败（可能上一轮还没结束）")

    def _on_token(self, t: str):
        if self.cur_bubble is not None:
            self.cur_bubble.append(t)
            self._scroll_to_bottom()
        # 自动朗读：边生成边朗读
        if self.auto_speak and self._stream_spk is not None:
            self._stream_feed(t)

    def _stream_feed(self, chunk: str):
        """把 token 累积到缓冲，凑成整句就立刻送去合成。

        为什么按句而不是按 token：TTS 需要完整句子才自然，
        逐 token 合成会又慢又难听。

        切句规则（增量）：只把"已确定结束"的句子送出去，
        最后一个没有句末标点的片段留在 _speak_buf 里等后续 token。
        """
        self._speak_buf += chunk

        # 首句要尽早吐出去：TTS 合成在推理进行中会被拖慢一倍
        # （实测 1.77s -> 3.58s），所以第一句不等句号，
        # 凑到 12 字或遇到逗号就先合成，让用户尽快听到声音。
        first_fast = not self._stream_spk.has_data

        cutoff = max(self._speak_buf.rfind(ch)
                     for ch in "。！？；!?;\n")
        if cutoff < 0:
            # 没有句末标点：首句降到逗号/12 字即切，之后按 60 字兜底
            limit = 12 if first_fast else 60
            if len(self._speak_buf) < limit:
                return
            cand = max(self._speak_buf.rfind(ch) for ch in "，、,；")
            cut = cand + 1 if cand > 0 else limit
            ready = self._speak_buf[:cut]
            self._speak_buf = self._speak_buf[cut:]
            if ready.strip():
                self._stream_spk.feed(ready)
            return
        ready = self._speak_buf[:cutoff + 1]
        self._speak_buf = self._speak_buf[cutoff + 1:]
        if ready.strip():
            self._stream_spk.feed(ready)

    def _on_ttft(self, ms: int):
        self.ttft_ms = ms
        self.lbl_ttft.set_text("首字 %.2fs" % (ms / 1000.0))
        self.lbl_ttft.get_style_context().remove_class("stat")
        self.lbl_ttft.get_style_context().add_class("stat-ok")

    def _on_done(self, ev: dict):
        total_ms = ev.get("ms", 0)
        tokens = ev.get("tokens", 0)
        aborted = ev.get("aborted", 0)
        # 首字之后的速度才是真实出字速度，去掉预填充时间
        gen_ms = max(1, total_ms - self.ttft_ms)
        speed = tokens / (gen_ms / 1000.0) if tokens else 0.0
        self.last_speed = speed
        self.lbl_speed.set_text("速度 %.1f tok/s" % speed)

        if self.cur_bubble is not None:
            if self._interrupt_reason:
                # 用户主动打断：保留打断原因，不被统计数据覆盖
                self.cur_bubble.set_meta("已中断 · %s" % self._interrupt_reason)
            else:
                meta = "首字 %.2fs · %d tok · %.1f tok/s · 总 %.2fs%s" % (
                    self.ttft_ms / 1000.0, tokens, speed, total_ms / 1000.0,
                    " · 已中断" if aborted else "")
                self.cur_bubble.set_meta(meta)
            if not self.cur_bubble.get_text().strip():
                self.cur_bubble.set_text("（没有输出）")
            answer = self.cur_bubble.get_text()
            self.last_ai_bubble = self.cur_bubble
            self.cur_bubble = None
        else:
            answer = ""

        aborted = aborted or bool(self._interrupt_reason)
        self._interrupt_reason = None

        if aborted:
            # 服务端已整段清掉 KV cache，下一轮要自己带历史
            self._need_replay = True
            # 打断的那一轮不进历史（a 保持 None），模型下一轮看不到
            if self.history and self.history[-1].get("a") is None:
                self.history[-1]["a"] = None
        else:
            # 正常完成：把回答记进历史，下轮需要重放时用
            if self.history and self.history[-1].get("a") is None:
                self.history[-1]["a"] = answer
            # 服务端自己维护 KV，客户端无需重放
            self._need_replay = False

        self.btn_send.set_label("发送")
        self._set_controls_busy(False)

        if aborted or not self.auto_speak:
            self._stop_reading()
        elif self._stream_spk is not None:
            tail = self._speak_buf.strip()
            if tail:
                self._stream_spk.feed(tail)
            self._speak_buf = ""
            self._stream_spk.finish()
        elif answer.strip():
            # Fallback is allowed only while automatic reading is enabled.
            self._speak_async(answer)

    def _on_error(self, msg: str):
        self._stop_reading()
        self._need_replay = True
        if self.cur_bubble is not None:
            self.cur_bubble.set_meta("请求失败")
        self._add_bubble("error", str(msg))
        self.cur_bubble = None
        self.btn_send.set_label("发送")
        self._set_controls_busy(False)

    def _on_warn(self, msg: str):
        self._add_bubble("system", "提示: " + str(msg))

    def _on_state(self, busy: bool):
        busy = busy or self._capture_pending
        self._set_controls_busy(busy)
        if not busy:
            self.btn_send.set_label("发送")
            self._update_conn_label()

    def _set_controls_busy(self, busy: bool):
        busy = busy or self._capture_pending
        # Closing is always available, opening is deferred until the request ends.
        self.btn_cam.set_sensitive(not self._cam_closing and (self.cam_mode or not busy))
        self.btn_clear.set_sensitive(not busy)
        self.entry.set_sensitive(not busy)
        if busy:
            self.btn_send.set_label("停止")
            self.btn_send.get_style_context().remove_class("btn-primary")
            self.btn_send.get_style_context().add_class("btn-stop")
        else:
            self.btn_send.set_label("发送")
            self.btn_send.get_style_context().remove_class("btn-stop")
            self.btn_send.get_style_context().add_class("btn-primary")
            if self.keyboard == "system":
                GLib.idle_add(self._focus_system_entry)

    def _update_conn_label(self):
        ctx = self.lbl_conn.get_style_context()
        if self.cli.connected:
            self.lbl_conn.set_text("● 已连接")
            ctx.remove_class("stat-warn")
            ctx.remove_class("stat-bad")
            ctx.add_class("stat-ok")
        else:
            self.lbl_conn.set_text("● 未连接")
            ctx.remove_class("stat-ok")
            ctx.add_class("stat-bad")

    def _on_clear_clicked(self, *_):
        if self.cli.busy or self._capture_pending:
            self._on_warn("请先停止生成，再清空对话")
            return
        self._stop_reading()
        if not self.cli.clear_history():
            self._on_warn("服务端清空失败，对话已保留，请稍后重试")
            return
        for child in self.chat_box.get_children():
            self.chat_box.remove(child)
        self.history.clear()
        self._need_replay = False
        for path in self._snapshot_paths:
            self._remove_snapshot(path)
        self._snapshot_paths.clear()
        self._last_snapshot = None
        self._last_visual_event = None
        self._add_bubble("system", "已清空对话历史。")

    # ── 键盘 ────────────────────────────────────────────────────
    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            if self.cli.busy:
                self.cli.abort()
                return True
        return False

    # ── 摄像头 ──────────────────────────────────────────────────
    def _on_cam_clicked(self, *_):
        if self._cam_closing:
            return
        ctx = self.btn_cam.get_style_context()
        if self.cam_mode:
            self.cam_mode = False
            self._cam_closing = True
            if self._capture_pending:
                self._cancel_capture()
                self._add_bubble("system", "摄像头已关闭，本轮拍照已取消，问题未发送。")
            elif self.cli.busy:
                self._add_bubble("system", "摄像头已关闭，当前回答仍基于发送时的快照。")
            ctx.remove_class("btn-cam-on")
            self._set_preview_visible(False)
            self.preview_img.clear()
            self.btn_cam.set_label("摄像头")
            self._set_controls_busy(self.cli.busy)
            def close():
                self.cam.stop()
                GLib.idle_add(self._camera_stopped)
            threading.Thread(target=close, daemon=True).start()
            return
        if self.cli.busy or self._capture_pending:
            self._on_warn("回答生成中暂不能重新打开摄像头，请先停止或等待完成")
            return
        if not self.cam.start():
            self._add_bubble("error", "摄像头启动失败: %s" % (self.cam.last_error or "未知"))
            return
        self.cam_mode = True
        self._last_snapshot = None
        ctx.add_class("btn-cam-on")
        self.preview_box.show_all()
        self._set_preview_visible(True)
        self.btn_cam.set_label("关闭摄像头")

    def _camera_stopped(self):
        if self._alive:
            self._cam_closing = False
            self._set_controls_busy(self.cli.busy)
        return False

    def _set_preview_visible(self, on: bool):
        """显示/收起摄像头预览区，收起时必须完全不占纵向空间。

        为什么 reveal_child=False 还不够：实测 revealer 收起但自身仍 visible 时，
        GTK 会为过渡动画预留 312px，对话区只能拿到 350px，中间整片空白。
        必须再把容器 hide() 掉，布局才会彻底释放（实测对话区从 350 → 662px）。
        所以这里是双控：reveal_child 负责动画，hide/show 负责真正的空间回收。
        """
        if not self._alive:
            return
        if on:
            self.preview_box.show_all()
            self.preview_revealer.show()
            self.preview_revealer.set_reveal_child(True)
        else:
            self.preview_revealer.set_reveal_child(False)
            self.preview_revealer.hide()
            # 光 hide 外层 Revealer 不够：内部 preview_box 自己仍 visible，
            # 它的 size_request（preview_img 300px + 头部 ≈342px）仍会参与
            # 上游布局协商，把对话区白白吃掉 ~180px。
            # 必须把预览框自身也藏起来，需求高度才会真正归零。
            self.preview_box.hide()

    def _tick_preview(self):
        """把最新一帧画到预览控件上。~15fps。"""
        if not self._alive:
            return False          # 返回 False 才会注销这个定时器
        if not self.cam_mode:
            return True
        arr, _seq = self.cam.latest()
        age = self.cam.frame_age
        if arr is None or age is None or age > 1.0:
            self.preview_img.clear()
            message = "等待摄像头新画面…" if self.cam.running else "摄像头已停止，请关闭后重开"
            if self.lbl_preview.get_text() != message:
                self.lbl_preview.set_text(message)
            return True
        if not self._capture_pending:
            self.lbl_preview.set_text("摄像头预览")
        pb = make_pixbuf_from_rgb(arr, self.cam.w, self.cam.h)
        if pb is not None:
            self.preview_img.set_from_pixbuf(pb)
        return True

    # ── 语音 ──────────────────────────────────────────���─────────
    def _preload_voice(self):
        ok, msg = voice.ensure_ready()
        # 注意必须包成 lambda：GLib.idle_add 把回调返回值当「是否继续调用」，
        # 而 _add_bubble 返回 Bubble（真值）—— 直接传会无限循环加气泡，
        # 实测能刷到 2900+ 条把整个布局撑爆（气泡 label 全变成 1x1）。
        if not ok:
            GLib.idle_add(lambda: (self._add_bubble(
                "system", "语音不可用（不影响文字/看图）: %s" % msg), False)[1])
        else:
            GLib.idle_add(lambda: (self._add_bubble(
                "system", "语音引擎已就绪"), False)[1])

    def _on_speak_clicked(self, *_):
        """朗读开关：控制 AI 回答是否自动读出声音。

        与「语音」按钮解耦：那个管麦克风录入（免提对话），
        这个只管“回答要不要念出来”。默认开。
        """
        self.auto_speak = not self.auto_speak
        ctx = self.btn_speak.get_style_context()
        if self.auto_speak:
            self.btn_speak.set_label("朗读中")
            ctx.add_class("btn-speak-on")
            if self.cur_bubble is not None and self.cli.busy:
                self._speak_buf = ""
                self._start_stream_speaker()
        else:
            self.btn_speak.set_label("已静音")
            ctx.remove_class("btn-speak-on")
            self._stop_reading()
        return False

    def _on_voice_clicked(self, *_):
        """语音按钮是开关：第一次开，再点一次关。

        以前“听的时候点一下”只会停止当次录音、模式仍开着，
        表现就是“按了没关掉”。现在不再区分，任何时刻点都是切换。
        """
        if self.voice_mode:
            self._set_voice_mode_off()
        else:
            self._start_voice_mode()
        return False

    def _start_voice_mode(self):
        self.voice_mode = True
        self.btn_voice.get_style_context().add_class("btn-mic-on")
        self.btn_voice.set_label("语音中")
        # 语音不再自动弹摄像头（两者已解耦，要看图自己点摄像头）
        if not voice.is_ready():
            self._voice_state("加载语音模型…")
        self._voice_loop_thread = threading.Thread(
            target=self._voice_loop, daemon=True)
        self._voice_loop_thread.start()

    def _voice_loop(self):
        """免提循环：听 -> 提问 -> （回答完触发朗读）。

        朗读期间也会监听：用户一开口就打断并直接进入下一轮，
        不再等当前回答读完。
        """
        ok, msg = voice.ensure_ready()
        if not ok:
            self._voice_state("语音不可用: %s" % msg)
            self._set_voice_mode_off()
            return
        self._voice_state("就绪，请说话")

        while self.voice_mode:
            # 朗读中：监听打断，而不是干等它读完
            if self._is_speaking():
                if self._wait_speaking_with_barge_in():
                    continue      # 被用户打断，直接回去听下一句
                continue

            # 推理中：等服务端回 idle
            if self.cli.busy:
                time.sleep(0.15)
                continue

            self._voice_busy = True
            self._voice_state("麦克风准备中…")
            try:
                text = self.voice_io.listen_once(on_state=self._voice_state)
            finally:
                self._voice_busy = False
            if not self.voice_mode:
                break
            if not text:
                self._voice_state("没听到，继续…")
                time.sleep(0.3)
                continue
            self._voice_state("🗣 %s" % text)
            # 必须在主线程调 _submit（碰控件），且回调必须返回假值，
            # 否则 GLib 会把返回值真值当成"再调一次"，无限重复提交。
            GLib.idle_add(lambda t=text: (self._submit(t), False)[1])

            # 等这一轮真的开始（_submit 走 idle 队列，是异步的）。
            # 若不等就开始等 busy==False，会在提交生效前就跳过等待，
            # 300ms 后直接进下一轮录音，把刚发出的问题盖掉 —— 
            # 表现就是"说什么模型都只回上一轮的答案"。
            t0 = time.time()
            while not self.cli.busy and time.time() - t0 < 5.0:
                if not self.voice_mode:
                    break
                time.sleep(0.02)
            # 再等推理结束
            while self.cli.busy and time.time() - t0 < 120 and self.voice_mode:
                time.sleep(0.15)
            time.sleep(0.3)

        self._voice_state("")

    def _wait_speaking_with_barge_in(self) -> bool:
        """朗读期间监听人声。返回 True 表示被用户打断。

        流式播报没有固定的"读完"时刻（边生成边读），所以这里
        另起一个哨兵：播报器不再 busy 时置位 stop，让监听退出。
        """
        # The onboard ES8389 cannot safely record and play simultaneously.
        # USB capture permits barge-in; same-card fallback is half-duplex.
        if not voice.can_listen_while_playing():
            while self.voice_mode and self._is_speaking():
                time.sleep(0.05)
            return False
        sp = self._stream_spk
        if sp is not None and sp.busy:
            stop = threading.Event()

            def watch():
                # 播报器播完（且合成队列空了）就通知监听退出
                t0 = time.time()
                while sp.busy and time.time() - t0 < 300:
                    time.sleep(0.1)
                stop.set()

            threading.Thread(target=watch, daemon=True).start()
            if self.voice_io.listen_for_barge_in(stop):
                self._interrupt("语音打断")
                return True
            return False

        stop = self._stop_speak
        if stop is None or stop.is_set():      # 已经读完了
            return False
        if not self._is_speaking():
            return False
        if self.voice_io.listen_for_barge_in(stop):
            self._interrupt("语音打断")
            return True
        return False

    def _interrupt(self, reason: str = "已中断"):
        """打断当前生成 + 朗读，并丢弃该轮上下文。

        丢弃规则（用户确认）：气泡留在屏幕上标“已中断”，
        但不进历史，下一轮模型看不到它。
        """
        # 1) 停止朗读（流式播报器和整段朗读都要停）
        try:
            if self._stop_speak is not None:
                self._stop_speak.set()
        except Exception:
            pass
        if self._stream_spk is not None:
            self._stream_spk.stop()
        subprocess.run(["pkill", "-x", "aplay"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 2) 停止推理。标记 _interrupted，让 _on_done 不覆盖气泡 meta。
        #    注意区分两种打断：
        #      generating: 模型还在出字 -> 半截回答必须丢弃
        #      speaking:   回答已完整，只是还在朗读 -> 回答是完整的，不该丢
        generating = self.cli.busy
        self._interrupt_reason = reason if generating else None
        if generating:
            self.cli.abort()

        # 3) 标气泡。两种时机都要覆盖：
        #    a) 正在生成 -> cur_bubble
        #    b) 已经生成完、正在朗读 -> cur_bubble 为 None，用 last_ai_bubble
        target = self.cur_bubble if self.cur_bubble is not None else self.last_ai_bubble
        if target is not None:
            target.set_meta("已中断 · %s" % reason)
            if self.cur_bubble is not None:
                # 不置 None：让 _on_done 还能把已生成的部分收尾
                #（它会识别到 _interrupt_reason，不覆盖这句 meta）
                pass

        # 只有「生成被打断」才丢弃上下文。
        # 朗读阶段被打断时回答是完整的，历史照常保留，下一轮直接接着聊。
        if generating:
            # 兜底：即使 _on_done 没回来（连接断了），下轮也要带历史
            self._need_replay = True
            if self.history:
                self.history[-1]["a"] = None

        self._voice_state("已打断，重新听…")
        return False

    def _voice_state(self, text: str):
        if not self._alive:
            return False
        if threading.get_ident() != self._ui_thread_id:
            GLib.idle_add(self._voice_state, text)
            return False
        self.lbl_voice.set_text(text)
        return False

    def _set_voice_mode_off(self):
        if not self._alive:
            return False
        if threading.get_ident() != self._ui_thread_id:
            GLib.idle_add(self._set_voice_mode_off)
            return False
        self.voice_mode = False
        self.btn_voice.set_label("语音")
        self.btn_voice.get_style_context().remove_class("btn-mic-on")
        self.voice_io.stop_listening()
        if not self.auto_speak:
            self._stop_reading()
        self.lbl_voice.set_text("")
        return False

    def _start_stream_speaker(self):
        """启动一轮流式播报器（上一轮的先停掉）。"""
        prev = self._stream_spk
        if prev is not None:
            prev.stop()
        spk = voice.StreamSpeaker(on_state=lambda message: GLib.idle_add(
            self._on_speech_warning, spk, message))
        self._stream_spk = spk
        spk.start()
        # 让打断监听知道"这一轮在朗读"，用 speak_thread 占位使
        # _wait_speaking_with_barge_in 的存活判断走流式分支
        self._stop_speak = None

    def _on_speech_warning(self, speaker, message: str):
        if self._alive and self._stream_spk is speaker:
            self._add_bubble("system", "朗读提示: " + message)
        return False

    def _speak_async(self, text: str):
        if not self.auto_speak or not text or not text.strip():
            return
        self._stop_speak = threading.Event()
        stop = self._stop_speak

        def run():
            try:
                # 分句朗读，每句之间检查是否被打断，打断延迟很短
                for piece in voice.split_for_speech(text):
                    if stop.is_set():
                        break
                    self.voice_io.speak(piece)
            finally:
                # 通知监听方“已经读完”，让它退出监听开麦的状态
                stop.set()
        self.speak_thread = threading.Thread(target=run, daemon=True)
        self.speak_thread.start()

    # ── 状态栏 ──────────────────────────────────────────────────
    def _tick_stats(self) -> bool:
        if not self._alive:
            return False
        used, total = read_mem()
        self.lbl_mem.set_text("内存 %d/%d MB" % (used, total))
        mem_ctx = self.lbl_mem.get_style_context()
        if total and used / total > 0.9:
            mem_ctx.add_class("stat-bad")
        else:
            # 回到正常区间必须摘掉，否则内存条一旦红过就永远是红的
            mem_ctx.remove_class("stat-bad")

        load = npu_load()
        if load is None:
            self.lbl_npu.set_text("NPU n/a")
        else:
            self.lbl_npu.set_text("NPU %d%%" % load)
        self._update_conn_label()
        return True

    # ── 退出 ────────────────────────────────────────────────────
    def _on_size_allocate(self, _w, alloc):
        """窗口尺寸变化 → 重算所有气泡的可用宽度。"""
        if not self._alive:
            return
        # size-allocate 在布局阶段会密集触发，防抖到布局稳定后再改，
        # 否则边循环改 label 宽度、边触发重排，会拖慢拖拽缩放。
        if alloc.width == getattr(self, "_last_alloc_w", -1):
            return
        self._last_alloc_w = alloc.width
        GLib.idle_add(self._apply_bubble_width)

    def _on_destroy(self, *_):
        self._alive = False          # 让所有 timeout 回调自行注销
        self.voice_mode = False
        speaker = self._stream_spk
        self._stop_reading()
        if speaker is not None:
            speaker.join(timeout=5.0)
        self.voice_io.stop_listening()
        self._capture_token += 1
        self._capture_pending = False
        self.cam.stop()
        if self._capture_worker is not None and self._capture_worker.is_alive():
            self._capture_worker.join(timeout=2)
        self._snapshot_dir.cleanup()
        self.cli.close()
        # 等朗读线程收尾：它在调 aplay，进程中途被回收会在退出时崩溃
        th = self.speak_thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        # 语音模型预加载线程持有 1GB+ ONNX 内存。若进程退出时它还在初始化，
        # onnxruntime 会在析构阶段抛 bad_alloc 并 segfault（已实测复现）。
        pt = getattr(self, "preload_thread", None)
        if pt is not None and pt.is_alive():
            pt.join(timeout=15.0)
        Gtk.main_quit()

    def run(self):
        self.cli.connect()
        self._update_conn_label()
        Gtk.main()


def main():
    # 兼容直接从 adb 运行 python3 rkchat.py：先切到正确桌面用户及输入法
    # 环境。导入 ChatApp 做测试时不触发重启。
    if os.environ.get("RKCHAT_DESKTOP_SESSION") != "1":
        from desktop_session import launch
        return launch(sys.argv[1:])
    sock = chatc.DEFAULT_SOCK
    dev = camera.DEFAULT_DEV
    fullscreen = False
    keyboard = "system"
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--sock" and i + 1 < len(args):
            sock = args[i + 1]; i += 2
        elif args[i] == "--dev" and i + 1 < len(args):
            dev = args[i + 1]; i += 2
        elif args[i] == "--fullscreen":
            fullscreen = True; i += 1
        elif args[i] == "--keyboard" and i + 1 < len(args):
            keyboard = args[i + 1]; i += 2
        else:
            i += 1
    app = ChatApp(sock=sock, dev=dev, fullscreen=fullscreen, keyboard=keyboard)
    _disp = Gdk.Display.get_default()
    print("[rkchat] keyboard=%s backend=%s uid=%d" % (
        keyboard, _disp.__gtype__.name if _disp is not None else "-",
        os.getuid()), flush=True)
    app.run()
    # 主循环退出后，守护线程可能还在跑（摄像头读帧/朗读）。
    # 这里显式停一遍并稍等，避免解释器终结时与它们竞争导致 segfault。
    try:
        app.cam.stop()
        app.cli.close()
    except Exception:
        pass
    time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
