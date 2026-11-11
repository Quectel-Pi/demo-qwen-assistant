#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音免提：麦克风采集 + Silero VAD 切句 + SenseVoice ASR + Matcha TTS。

不重写语音引擎，直接复用板端 /data/voice-vision/talk.py 里已经调好的实现：
  - Mic / Speaker / VoiceEngine 三个类
  - Speaker 内部带 ES8389 播放速率补偿（DEFAULT_SPEAKER_K≈1.897），
    否则板子上语速快近 2 倍，几乎听不清

talk.py 的真实接口（不是猜的）：
    mic = talk.Mic(); mic.open(); x = mic.read(512)   # 返回 float32[-1,1]，超时 None
    engine.vad.accept_waveform(x);                    # Silero 必须 512 样本一块
    while not engine.vad.empty():
        seg = engine.vad.front.samples; engine.vad.pop()
    engine.transcribe(seg)   -> (text, cost)
    engine.synthesize(text)  -> (pcm, rate, cost)
    speaker.play(pcm, rate)  -> cost

对外只暴露 VoiceIO，采集线程靠回调把结果交给 UI。
"""

from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

VOICE_DIR = "/data/voice-vision"
SAMPLE_RATE = 16000
FRAME = 512                    # Silero VAD 要求固定 512 样本

_engine_lock = threading.Lock()
# Stream workers from an interrupted round may still be synthesizing when the
# next round starts. Sherpa TTS must not be entered concurrently.
_tts_lock = threading.Lock()
_playback_lock = threading.Lock()
# ES8389 capture and playback share a clock and must not run together.
_half_duplex_lock = threading.Lock()
# 板端只有一个采集设备（即使是 USB 麦，ALSA 也不允许两个 arecord 同时打开）：
# _voice_loop 的 listen_once 和朗读期间的 listen_for_barge_in 是两个线程，
# 必须靠这把锁串行化 _mic.open()/close()，否则后开的那个直接
# "Device or resource busy"。不能用 _half_duplex_lock：用 USB 麦时
# can_listen_while_playing() 为 True，那把锁整个被跳过。
_mic_lock = threading.Lock()
_engine = None
_mic = None
_speaker = None
_import_error: str | None = None
_ready = False


def _bootstrap():
    global _import_error
    if VOICE_DIR not in sys.path:
        sys.path.insert(0, VOICE_DIR)
    try:
        import talk
        return talk
    except Exception as e:
        _import_error = "%s: %s" % (type(e).__name__, e)
        return None


def capture_device(proc_asound: Path = Path("/proc/asound")) -> str:
    """Prefer an independent USB capture clock; never override explicit config."""
    if os.environ.get("MIC_DEVICE"):
        return os.environ["MIC_DEVICE"]
    for card in sorted(proc_asound.glob("card[0-9]*")):
        if (card / "usbid").is_file() and (card / "pcm0c").is_dir():
            identifier = (card / "id").read_text().strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                return "plughw:CARD=%s,DEV=0" % identifier
    return "hw:0,0"


def _card_id(device: str, proc_asound: Path = Path("/proc/asound")):
    match = re.match(r"(?:plug)?hw:(?:CARD=)?([^,]+)", device)
    if match is None:
        return None
    identifier = match.group(1)
    if identifier.isdigit():
        try:
            return (proc_asound / ("card" + identifier) / "id").read_text().strip()
        except OSError:
            return identifier
    return identifier


def can_listen_while_playing() -> bool:
    if _mic is None or _speaker is None:
        return False
    capture = _card_id(getattr(_mic, "device", ""))
    playback = _card_id(getattr(_speaker, "device", ""))
    return capture is not None and playback is not None and capture != playback


def ensure_ready(sr_threads: int = 4, tts_threads: int = 4) -> tuple[bool, str]:
    """加载 ASR/TTS/VAD 模型 + 打开设备。首次约 2-4 秒。"""
    global _engine, _mic, _speaker, _ready
    with _engine_lock:
        if _ready:
            return True, "就绪"
        talk = _bootstrap()
        if talk is None:
            return False, "导入 talk.py 失败: %s" % (_import_error or "未知")
        try:
            if _engine is None:
                e = talk.VoiceEngine(sr_threads=sr_threads,
                                     tts_threads=tts_threads)
                e.load()
                _engine = e
            if _mic is None:
                _mic = talk.Mic(device=capture_device())
            if _speaker is None:
                _speaker = talk.Speaker()
            _ready = True
            return True, "就绪"
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)


def is_ready() -> bool:
    return _ready


def _synthesize(text: str, stop: threading.Event | None = None):
    with _tts_lock:
        if stop is not None and stop.is_set():
            return None, 0, 0.0
        return _engine.synthesize(text)


def _play_pcm(pcm, rate: int, stop: threading.Event | None = None):
    # Even with an independent USB mic there is only one output device.
    with _playback_lock:
        guard = nullcontext() if can_listen_while_playing() else _half_duplex_lock
        with guard:
            if stop is not None and stop.is_set():
                return None
            return _speaker.play(pcm, rate)


# ── TTS 前的文本清洗 ────────────────────────────────────────────
# sherpa-onnx 的 matcha 中文模型用有限词表，遇到英文单词、markdown 标记、
# emoji 都会走 OOV 分支往 stderr 打一行 "OOV word skipped: 'xxx'"。
# 模型回答里这类内容很多（Qwen / Python / ** / 👍），一次朗读能刷几十行，
# 把 journalctl 彻底淹没。这里先剔掉，只留中文和基本标点。
_TTS_KEEP = re.compile(r"[\u4e00-\u9fff0-9，。？！、：；（）《》“”‘’—…\s]")
_TTS_PUNCT_ONLY = re.compile(r"^[\s，。？！、：；（）《》“”‘’—…]*$")


def clean_for_tts(text: str) -> str:
    """剔掉 TTS 读不了的内容：markdown 标记、英文、emoji。

    不追求完美，只求不刷屏、且朗读内容连贯：
      - 去掉 ``` 代码块、** 加粗、# 标题、- 列表等标记
      - 去掉英文单词和 emoji（中文模型读不了）
      - 合并多余空白
    全是可剔内容时返回空串，调用方据此跳过朗读。
    """
    if not text:
        return ""
    s = text
    # 代码块整段去掉（读代码没有意义，还全是英文）
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"`[^`]*`", " ", s)
    # markdown 标记
    s = re.sub(r"\*\*|__|~~|\*|#+|>\s*", " ", s)
    s = re.sub(r"^\s*[-+•]\s*", " ", s, flags=re.M)
    # 链接保留文字部分
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # 只留中文/数字/中文标点
    s = "".join(ch if _TTS_KEEP.match(ch) else " " for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or _TTS_PUNCT_ONLY.match(s):
        return ""
    return s


_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\n])")


def split_for_speech(text: str, max_len: int = 48) -> list[str]:
    """按句子切开，方便朗读时逐句检查打断。

    TTS 一次合成整段时，打断要么等一下，要么直接用 pkill 刮断 aplay。
    分句后打断延迟大约只剩一句的朗读时间（1-3s）。
    过短的句子合并，避免频繁合成开销。
    """
    s = clean_for_tts(text)
    if not s:
        return []
    parts = [p.strip() for p in _SPLIT_RE.split(s)]
    out: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if len(buf) + len(p) <= max_len:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out


class VoiceIO:
    """录音->识别、文本->朗读，两个阻塞动作，调用方负责放进线程。"""

    def __init__(self, max_seconds: float = 15.0):
        self.max_seconds = max_seconds
        self._stop_flag = threading.Event()
        self._recording = False
        self._speaking = False
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def speaking(self) -> bool:
        return self._speaking

    # ── 录音 + 识别 ─────────────────────────────────────────────
    def listen_once(self, on_state=None) -> str | None:
        self._stop_flag.clear()
        ok, message = ensure_ready()
        if not ok:
            if on_state:
                on_state("语音不可用: %s" % message)
            return None
        guard = nullcontext() if can_listen_while_playing() else _half_duplex_lock
        with guard, _mic_lock:
            if self._stop_flag.is_set():
                return None
            return self._listen_once(on_state)

    def _listen_once(self, on_state=None) -> str | None:
        """录一句话并识别整句。返回文本；静音/失败返回 None。

        说完整句才返回：VAD 每检测到一个语音段就收集起来，
        等到 VAD 静音超过设定时长（vad 内部规则）再一起识别。
        """
        self._recording = True
        try:
            _mic.open()
            if not self._wait_for_capture():
                _mic.close()
                self._recording = False
                if on_state and not self._stop_flag.is_set():
                    on_state("麦克风没有送来音频，请检查连接和输入设备")
                return None
        except Exception as e:
            _mic.close()
            self._recording = False
            if on_state:
                on_state("麦克风打开失败: %s" % e)
            return None

        if on_state:
            on_state("在听…")
        segs: list = []
        deadline = time.time() + self.max_seconds
        tail_deadline = None      # 拿到第一段后的收尾期限
        try:
            while time.time() < deadline and not self._stop_flag.is_set():
                if tail_deadline is not None and time.time() > tail_deadline:
                    break
                frame = _mic.read(FRAME, timeout=0.3)
                if frame is None:
                    continue
                try:
                    _engine.vad.accept_waveform(frame)
                except Exception:
                    # VAD 异常就退化成整段录音，不至于完全不可用
                    segs.append(frame)
                    continue
                got = 0
                while not _engine.vad.empty():
                    segs.append(_engine.vad.front.samples)
                    _engine.vad.pop()
                    got += 1
                if got:
                    # 检测到一段人声。再等 1.2s 看是否还有下文，
                    # 没下文就认为说完了；有下文则继续延长收尾期限。
                    tail_deadline = time.time() + 1.2
        finally:
            try:
                _mic.close()
            except Exception:
                pass
            self._recording = False

        if self._stop_flag.is_set():
            return None
        if not segs:
            if on_state:
                on_state("没听到声音")
            return None
        try:
            import numpy as np
            pcm = np.concatenate(segs).astype("float32")
        except Exception:
            pcm = segs[0]
        if on_state:
            on_state("识别中…")
        try:
            text, cost = _engine.transcribe(pcm)
        except Exception as e:
            if on_state:
                on_state("识别失败: %s" % e)
            return None
        text = (text or "").strip()
        if not text:
            if on_state:
                on_state("没识别出内容")
            return None
        if on_state:
            on_state("听到: %s" % text)
        return text

    def _wait_for_capture(self, timeout: float = 2.0) -> bool:
        """Show 'listening' only after PCM arrives and startup transients settle."""
        deadline = time.monotonic() + timeout
        ready_at = None
        while time.monotonic() < deadline and not self._stop_flag.is_set():
            frame = _mic.read(FRAME, timeout=0.1)
            if frame is not None:
                if ready_at is None:
                    ready_at = time.monotonic() + 0.25
                if time.monotonic() >= ready_at:
                    return True
        return False

    def stop_listening(self):
        self._stop_flag.set()

    # ── 声控打断：朗读期间监听人声 ──────────────────────────────
    def listen_for_barge_in(self, stop_event: threading.Event,
                            threshold: float = 0.06,
                            window: int = 15,
                            ratio: float = 0.4,
                            settle: float = 0.6) -> bool:
        """朗读期间监听，用户一开口就返回 True。

        板端实测：播放时麦克风 rms 中位 0.0022（p95 0.0042），
        人说话时约 0.27 —— 扬声器声音不会被麦收回，
        所以不需要 AEC，能量门槛就能可靠区分。

        但 Mic.open() 后前 5 帧是硬件上电瞬态：
        实测 rms 0.797 -> 0.646 -> 0.604 -> 0.31 -> 0.125 才落到 0.002。
        所以开麦后先丢 settle 秒数据，否则每次开听都会把自己打断。

        判定用**滑动窗口比例**而不是「连续 N 帧」：
        单帧只有 512/16000 = 32ms，要求连续 8 帧就等于要求连续 256ms 有声。
        而正常说话每 200-400ms 就有一次字间停顿，永远凑不满 —— 实测会失灵。
        改成「最近 window(=15 帧≈480ms) 里过阈帧占比 >= ratio(0.4)」：
          - 说话占比 >=40% 就触发（覆盖带停顿的正常说话）
          - 短促尖峰（<=6 帧）和开麦瞬态（5 帧）都不足 40%，不误触发
        """
        global _mic
        ok, _ = ensure_ready()
        if not ok:
            return False
        # 和 listen_once 串行化设备操作；锁被别的线程占着时最多等这么久，
        # 拿不到就当本轮没有打断（用户还能再开口），不能把朗读线程卡死。
        if not _mic_lock.acquire(timeout=2.0):
            return False
        try:
            try:
                _mic.open()
            except Exception:
                return False
            self._recording = True
            hist: list[int] = []          # 最近 window 帧是否过阈（1/0）
            try:
                # 丢掉开麦瞬态，否则每次开始监听都会被自己打断
                t_settle = time.time() + settle
                while time.time() < t_settle and not stop_event.is_set():
                    if _mic.read(FRAME, timeout=0.2) is None:
                        time.sleep(0.02)

                while not stop_event.is_set():
                    frame = _mic.read(FRAME, timeout=0.2)
                    if frame is None:
                        continue
                    try:
                        rms = float((frame ** 2).mean()) ** 0.5
                    except Exception:
                        continue
                    hist.append(1 if rms > threshold else 0)
                    if len(hist) > window:
                        hist.pop(0)
                    # 窗口填满后才判定，避免刚开麦就误报
                    if len(hist) >= window and sum(hist) / len(hist) >= ratio:
                        return True
            finally:
                self._recording = False
                try:
                    _mic.close()
                except Exception:
                    pass
        finally:
            _mic_lock.release()
        return False

    # ── 朗读 ────────────────────────────────────────────────────
    def speak(self, text: str) -> float:
        """合成并播放。返回耗时秒数。阻塞。"""
        global _speaker, _engine
        ok, _ = ensure_ready()
        if not ok or not text or not text.strip():
            return 0.0
        text = clean_for_tts(text)
        if not text:
            return 0.0
        self._speaking = True
        try:
            pcm, rate, t_syn = _synthesize(text)
            if pcm is None or len(pcm) == 0:
                return 0.0
            t_play = _play_pcm(pcm, rate)
            return float(t_syn + t_play)
        except Exception as exc:
            print("[voice] 朗读失败: %s" % exc, file=sys.stderr)
            return 0.0
        finally:
            self._speaking = False


class StreamSpeaker:
    """边生成边朗读：句子一凑齐就合成播放，不等整段答案。

    为什么要流水线（实测数据，RTF≈0.42）：
        单句合成 0.84s，音频 2.00s。串行「合成->播放」每句要等 0.84s，
        长回答会明显落后于文本。所以用两级流水：
        合成线程 往队列塞 (pcm, rate)，播放线程 从队列取来播，
        队列容量 2 —— 合成始终比播放快一步，播放永不空等。

    打断：stop() 置位后，合成线程不再产出，播放线程用 pkill 掐掉 aplay，
    延迟约等于当前这一句剩余播放时间。
    """

    def __init__(self, on_state=None):
        # Keep text cheap and lossless; answer length is bounded by the service.
        # Only the much larger PCM queue applies backpressure, in a worker.
        self._tq: queue.Queue = queue.Queue()
        self._aq: queue.Queue = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._synthesis_done = threading.Event()
        self._input_lock = threading.Lock()
        self._input_closed = False
        self._reported_error = False
        self._th_syn: threading.Thread | None = None
        self._th_play: threading.Thread | None = None
        self._on_state = on_state
        self._spoken_any = False
        self._fed_any = False

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self):
        if self._th_syn is not None:
            raise RuntimeError("每轮朗读必须使用新的 StreamSpeaker")
        self._th_syn = threading.Thread(target=self._syn_loop, daemon=True)
        self._th_play = threading.Thread(target=self._play_loop, daemon=True)
        self._th_syn.start()
        self._th_play.start()

    def feed(self, text: str):
        """喂一段可朗读的文字。必须是完整句子（调用方负责切句）。"""
        if self._stop.is_set() or not text:
            return
        text = clean_for_tts(text)
        if not text:
            return
        with self._input_lock:
            if self._input_closed or self._stop.is_set():
                return
            self._fed_any = True
            self._tq.put_nowait(text)

    def stop(self):
        """立即停止：不再合成，掐断当前播放。"""
        self._stop.set()
        with self._input_lock:
            self._input_closed = True
        # 清空两个队列，避免停止后还继续播旧内容
        for q in (self._tq, self._aq):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        try:
            subprocess.run(["pkill", "-x", "aplay"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass

    def finish(self):
        """文本已全部喂完：等队列里的句子合成并播完。"""
        # Called from GTK: never wait for audio/synthesis here.
        with self._input_lock:
            if self._input_closed:
                return
            self._input_closed = True
            self._tq.put_nowait(None)

    def join(self, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        for th in (self._th_syn, self._th_play):
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def has_data(self) -> bool:
        """是否已经喂过至少一段（用来判断"首句还没出去"）。"""
        return self._spoken_any or self._fed_any

    @property
    def busy(self) -> bool:
        """还在合成或播放。"""
        if self._th_play is not None and self._th_play.is_alive():
            return not self._stop.is_set()
        return False

    # ── 内部线程 ────────────────────────────────────────────────
    def _report_error(self, message: str):
        with self._input_lock:
            if self._reported_error:
                return
            self._reported_error = True
        print("[stream] %s" % message, file=sys.stderr)
        if self._on_state is not None:
            try:
                self._on_state(message)
            except Exception as exc:
                print("[stream] 状态回调失败: %s" % exc, file=sys.stderr)

    def _syn_loop(self):
        """Wait for the shared engine off the GTK thread, then drain all text."""
        try:
            if self._stop.is_set():
                return
            ok, message = ensure_ready()
            if not ok:
                self._report_error("语音初始化失败: %s" % message)
                return
            while not self._stop.is_set():
                try:
                    text = self._tq.get(timeout=0.2)
                except queue.Empty:
                    continue
                if text is None:
                    return
                try:
                    pcm, rate, _cost = _synthesize(text, self._stop)
                except Exception as exc:
                    self._report_error("语音合成失败: %s" % exc)
                    continue
                if self._stop.is_set() or pcm is None or len(pcm) == 0:
                    continue
                while not self._stop.is_set():
                    try:
                        self._aq.put((pcm, rate), timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:
            self._report_error("语音工作线程失败: %s" % exc)
        finally:
            # Completion is not a lossy sentinel in the bounded audio queue.
            self._synthesis_done.set()

    def _play_loop(self):
        """播放线程：按顺序播放合成的句子。"""
        global _speaker
        while True:
            try:
                item = self._aq.get(timeout=0.3)
            except queue.Empty:
                if self._stop.is_set():
                    return
                if self._synthesis_done.is_set():
                    return
                continue
            if item is None:
                return
            if self._stop.is_set():
                return
            pcm, rate = item
            try:
                # 每帧都重新取 _speaker：ensure_ready() 可能在播放线程
                # 启动之后才把 _speaker 建出来，早先捕获的全局量会是 None。
                if _speaker is None:
                    ensure_ready()
                if _speaker is None:
                    continue
                cost = _play_pcm(pcm, rate, self._stop)
                if self._stop.is_set():
                    return
                if cost is not None and cost <= 0:
                    raise RuntimeError("音频设备没有完成播放")
                self._spoken_any = True
            except Exception as exc:
                if self._stop.is_set():
                    return
                self._report_error("播放失败: %s" % exc)
                continue
            if self._stop.is_set():
                return


if __name__ == "__main__":
    print("加载中…")
    t0 = time.time()
    ok, msg = ensure_ready()
    print("ensure_ready:", ok, msg, "%.1fs" % (time.time() - t0))
    if not ok:
        raise SystemExit(1)
    vio = VoiceIO(max_seconds=8.0)
    print("请说话（最多 8 秒）…")
    txt = vio.listen_once(on_state=lambda s: print("  [%s]" % s))
    print("识别结果:", repr(txt))
    if txt:
        print("朗读中… %.2fs" % vio.speak(txt))
