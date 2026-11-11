#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk.py —— RK3576 本地「语音对话 + 摄像头识物」全链路

链路：
    麦克风 → Silero VAD 切句 → SenseVoice ASR → 文本
                                                  ↓
                         （含识物指令时先抓一帧）→ Qwen3.5-0.8B 多模态
                                                  ↓
                                  Matcha TTS → ALSA 播放 → 喇叭

全部在本机完成，不联网。模型常驻，只加载一次。

用法：
    ./talk.py                          # 麦克风对话（自动探测摄像头）
    ./talk.py --image photo.jpg        # 用固定图片代替摄像头
    ./talk.py --once "图里有什么？"     # 文字问一次，走 LLM+TTS
    ./talk.py --wav speech.wav         # 用 wav 文件代替麦克风（测链路）
    ./talk.py --probe                  # 只做设备自检
"""

from __future__ import annotations

import argparse
import os
import re
import select
import subprocess
import sys
import time
import unicodedata
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE_ROOT = os.environ.get("VOICE_ROOT", "/data/voice-vision")
PY_DIR = os.path.join(VOICE_ROOT, "py")
LLM_DIR = os.environ.get("LLM_DIR", "/data/rkllm_qwen35")

sys.path.insert(0, PY_DIR)
sys.path.insert(0, LLM_DIR)      # 复用 chat.py 的 Engine（它用 LLM_DIR 定位模型）
os.environ.setdefault("LLM_DIR", LLM_DIR)

import numpy as np  # noqa: E402

# ── 设备常量 ────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
AREc = "arecord"
APlay = "aplay"
MIC_DEVICE = os.environ.get("MIC_DEVICE", "hw:0,0")
SPK_DEVICE = os.environ.get("SPK_DEVICE", "hw:0,0")

ASR_DIR = os.path.join(VOICE_ROOT, "models/asr",
                       "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
VAD_MODEL = os.path.join(VOICE_ROOT, "models/vad/silero_vad.onnx")
TTS_DIR = os.path.join(VOICE_ROOT, "models/tts/matcha-icefall-zh-baker")
TTS_VOCODER = os.path.join(VOICE_ROOT, "models/tts/matcha/hifigan_v2.onnx")

# 识物类指令关键词 —— 命中就抓一帧图再问
VISION_HINTS = ("看", "这是", "什么东", "什么", "前面", "镜头", "拍",
                "识", "图中", "画面", "手里", "桌上", "这个", "那个")
EXIT_HINTS = ("退出", "结束", "再见", "不聊了", "关闭")

# 让模型给出适合朗读的短回答
BRIEF_HINT = "\n（请用简短口语回答，不超过三句话，不要用 markdown 或表情符号。）"

SPEAKER_K_CACHE = "/tmp/.talk_speaker_k"
DEFAULT_SPEAKER_K = 1.897      # ES8389 实际播放速率 / 名义速率（实测）


def clean_for_speech(text: str) -> str:
    """把 markdown / emoji / 括号注释清掉，只留下能念的文字。

    不清的话 TTS 会把 '**' '✅' 之类的符号当作 OOV 丢掉，
    同时把 40 多字的内容合成出 40 秒音频。
    """
    s = text
    s = re.sub(r"```.*?```", " ", s, flags=re.S)          # 代码块
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # markdown 链接保留文字
    s = re.sub(r"\*\*|__|[*_`~]", "", s)                  # 强调/代码标记
    s = re.sub(r"^\s*#+\s*", "", s, flags=re.M)            # 标题
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)         # 列表符号
    s = re.sub(r"\|.*?\|", " ", s)                        # 表格行
    s = re.sub(r"https?://\S+", " ", s)                   # 链接
    s = re.sub(r"（[^）]*）|\([^)]*\)", "", s)              # 括号注释
    # 去掉 emoji / 杂符号
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    s = "".join(out)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "。", s)
    s = s.replace("\n", "。")
    s = re.sub(r"。{2,}", "。", s)
    return s.strip(" 。\t")


def truncate_speech(text: str, limit: int) -> str:
    """长回答只念开头几句，避免播报几十秒。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("，"))
    return head[:cut + 1] if cut > limit // 3 else head

C = {"reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
     "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
     "red": "\033[31m", "magenta": "\033[35m"}


def paint(text: str, *styles: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(C[s] for s in styles) + text + C["reset"]


def log(tag: str, msg: str, color: str = "dim") -> None:
    print(f"{paint(tag.rjust(6), color)} {msg}", flush=True)


# ── 音频 I/O ────────────────────────────────────────────────────────
class Mic:
    """arecord 原始流常驻，按 512 样本一块地读给 VAD。"""

    def __init__(self, device: str = MIC_DEVICE, rate: int = SAMPLE_RATE) -> None:
        self.device = device
        self.rate = rate
        self.proc: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [AREc, "-D", self.device, "-c", "1", "-r", str(self.rate),
             "-f", "S16_LE", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def close(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
        self.proc = None

    def read(self, samples: int = 512, timeout: float = 0.3) -> np.ndarray | None:
        """读一块 float32 [-1,1]，超时返回 None。"""
        if not self.proc or not self.proc.stdout:
            return None
        fd = self.proc.stdout.fileno()
        need = samples * 2
        buf = b""
        deadline = time.monotonic() + timeout
        while len(buf) < need:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                return None
            chunk = os.read(fd, need - len(buf))
            if not chunk:
                return None
            buf += chunk
        return np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0


class Speaker:
    """把 float32 波形写给 aplay。

    ES8389 的时钟系数没锁住（dmesg: 'Clock coefficients do not match'），
    实际播放速率约为名义值的 k 倍，导致声音又快又尖。
    补偿办法：把样本数放大 k 倍再按原采样率送出去（等价于 sox speed 1/k），
    这样时长和音调都能回到正确值。
    """

    def __init__(self, device: str = SPK_DEVICE, ratio: float = 0.0) -> None:
        self.device = device
        self.ratio = ratio or self._load_ratio()

    @staticmethod
    def _load_ratio() -> float:
        env = os.environ.get("SPEAKER_RATIO")
        if env:
            try:
                return float(env)
            except ValueError:
                pass
        try:
            with open(SPEAKER_K_CACHE, encoding="ascii") as fh:
                v = float(fh.read().strip())
                if 1.0 <= v <= 4.0:
                    return v
        except (OSError, ValueError):
            pass
        return DEFAULT_SPEAKER_K

    def calibrate(self, save: bool = True) -> float:
        """播一段已知时长测试音，量出实际速率倍数。已校准过就直接用缓存。"""
        if os.path.exists(SPEAKER_K_CACHE) and not os.environ.get("SPEAKER_RECAL"):
            return self.ratio
        tone = "/tmp/.talk_cal.wav"
        subprocess.run(["sox", "-n", "-r", "22050", "-c", "1", tone,
                        "synth", "4", "sine", "440"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = time.monotonic()
        subprocess.run(["aplay", "-D", self.device, "-q", tone],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        dur = time.monotonic() - t
        if 0.3 < dur < 60:
            self.ratio = 4.0 / dur
            if save:
                try:
                    with open(SPEAKER_K_CACHE, "w", encoding="ascii") as fh:
                        fh.write(f"{self.ratio:.4f}")
                except OSError:
                    pass
        return self.ratio

    @staticmethod
    def _stretch(x: np.ndarray, k: float) -> np.ndarray:
        """按 k 倍拉长（样本数 ×k），线性插值即可。"""
        if k <= 1.001 or x.size == 0:
            return x
        n_new = int(x.size * k)
        idx = np.linspace(0, x.size - 1, n_new, dtype=np.float64)
        return np.interp(idx, np.arange(x.size), x).astype(np.float32)

    def play(self, samples: np.ndarray, rate: int) -> float:
        if samples.size == 0:
            return 0.0
        samples = self._stretch(samples, self.ratio)
        peak = float(np.abs(samples).max())
        if peak > 1e-6:                      # 归一化到 -1.4 dBFS
            samples = samples * (0.85 / peak)
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16).tobytes()
        t0 = time.monotonic()
        try:
            subprocess.run(
                [APlay, "-D", self.device, "-f", "S16_LE", "-r", str(rate),
                 "-c", "1", "-q", "-"],
                input=pcm, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            log("ERR", f"播放失败: {exc}", "red")
        return time.monotonic() - t0


# ── 摄像头 ──────────────────────────────────────────────────────────
def _video_devices() -> list[str]:
    try:
        names = sorted(os.listdir("/sys/class/video4linux"))
    except OSError:
        return []
    return ["/dev/" + n for n in names if n.startswith("video")]


def find_camera() -> tuple[str | None, str]:
    """挑一个能出图的 V4L2 节点。返回 (设备, 说明)。"""
    candidates: list[tuple[str, str, str]] = []
    for dev in _video_devices():
        try:
            with open(f"/sys/class/video4linux/{os.path.basename(dev)}/name",
                      encoding="utf-8", errors="replace") as fh:
                card = fh.read().strip()
        except OSError:
            continue
        info = ""
        try:
            info = subprocess.run(["v4l2-ctl", "-d", dev, "--info"],
                                  capture_output=True, text=True,
                                  timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        if "Video Capture" not in info:
            continue
        drv = ""
        for line in info.splitlines():
            if "Driver name" in line:
                drv = line.split(":", 1)[1].strip()
        candidates.append((dev, card, drv))

    # 优先 UVC（USB 摄像头，ffmpeg 直接可用），其次 ISP 主通路
    for dev, card, drv in candidates:
        if "uvc" in drv.lower():
            return dev, f"USB 摄像头 {dev} ({card})"
    for dev, card, drv in candidates:
        if "mainpath" in card or "isp" in drv.lower():
            return dev, f"MIPI/ISP 摄像头 {dev} ({card})"
    if candidates:
        return candidates[0][0], f"{candidates[0][0]} ({candidates[0][1]})"
    return None, "未找到可用摄像头（V4L2 无采集节点）"


def grab_frame(dev: str, out_path: str, size: str = "640x480") -> bool:
    """抓一帧存 JPEG。UVC 走 ffmpeg，ISP 多平面走 v4l2-ctl + 转码。"""
    if os.path.exists(out_path):
        os.unlink(out_path)

    # 路线 1：ffmpeg 直接读（UVC 单平面设备）
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
         "-video_size", size, "-i", dev, "-frames:v", "1", "-y", out_path],
        capture_output=True, text=True, timeout=25,
    )
    if r.returncode == 0 and os.path.exists(out_path):
        return True
    err1 = (r.stderr or "").strip().splitlines()[-1:] or [""]

    # 路线 2：v4l2-ctl 拿原始 NV12，再转 JPEG
    raw = "/tmp/.frame.raw"
    w, h = size.split("x")
    r2 = subprocess.run(
        ["v4l2-ctl", "-d", dev,
         f"--set-fmt-video=width={w},height={h},pixelformat=NV12",
         "--stream-mmap", "--stream-count=1", f"--stream-to={raw}"],
        capture_output=True, text=True, timeout=25,
    )
    if r2.returncode == 0 and os.path.exists(raw) and os.path.getsize(raw) > 0:
        r3 = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "nv12", "-s", size,
             "-i", raw, "-frames:v", "1", "-y", out_path],
            capture_output=True, text=True, timeout=25,
        )
        os.unlink(raw)
        if r3.returncode == 0 and os.path.exists(out_path):
            return True

    log("ERR", f"抓帧失败: ffmpeg={err1[0]!r} v4l2-ctl={r2.stderr.strip()[:80]!r}", "red")
    return False


# ── 语音引擎（ASR / TTS / VAD 常驻）─────────────────────────────────
class VoiceEngine:
    def __init__(self, sr_threads: int = 4, tts_threads: int = 4) -> None:
        self.sr_threads = sr_threads
        self.tts_threads = tts_threads
        self.rec = None
        self.tts = None
        self.vad = None
        self.load_stats: dict[str, float] = {}

    def load(self) -> None:
        import sherpa_onnx as S

        t = time.monotonic()
        self.rec = S.OfflineRecognizer.from_sense_voice(
            model=os.path.join(ASR_DIR, "model.int8.onnx"),
            tokens=os.path.join(ASR_DIR, "tokens.txt"),
            num_threads=self.sr_threads, use_itn=True, language="zh",
        )
        self.load_stats["asr"] = time.monotonic() - t

        t = time.monotonic()
        cfg = S.OfflineTtsConfig(
            model=S.OfflineTtsModelConfig(
                matcha=S.OfflineTtsMatchaModelConfig(
                    acoustic_model=os.path.join(TTS_DIR, "model-steps-3.onnx"),
                    vocoder=TTS_VOCODER,
                    tokens=os.path.join(TTS_DIR, "tokens.txt"),
                    lexicon=os.path.join(TTS_DIR, "lexicon.txt"),
                    dict_dir=os.path.join(TTS_DIR, "dict"),
                ),
                num_threads=self.tts_threads,
            ),
            rule_fsts=f"{TTS_DIR}/date.fst,{TTS_DIR}/number.fst",
            rule_fars=f"{TTS_DIR}/phone.fst",
            max_num_sentences=1,
        )
        self.tts = S.OfflineTts(cfg)
        self.load_stats["tts"] = time.monotonic() - t

        t = time.monotonic()
        vcfg = S.VadModelConfig()
        vcfg.silero_vad.model = VAD_MODEL
        vcfg.silero_vad.threshold = 0.5
        vcfg.silero_vad.min_silence_duration = 0.35
        vcfg.silero_vad.min_speech_duration = 0.25
        vcfg.sample_rate = SAMPLE_RATE
        self.vad = S.VoiceActivityDetector(vcfg, buffer_size_in_seconds=30)
        self.load_stats["vad"] = time.monotonic() - t

    # -- ASR -------------------------------------------------------
    def transcribe(self, samples: np.ndarray) -> tuple[str, float]:
        t = time.monotonic()
        st = self.rec.create_stream()
        st.accept_waveform(SAMPLE_RATE, samples.astype(np.float32))
        self.rec.decode_stream(st)
        return (st.result.text or "").strip(), time.monotonic() - t

    # -- TTS -------------------------------------------------------
    def synthesize(self, text: str) -> tuple[np.ndarray, int, float]:
        t = time.monotonic()
        audio = self.tts.generate(text, sid=0, speed=1.0)
        return (np.asarray(audio.samples, dtype=np.float32),
                audio.sample_rate, time.monotonic() - t)

    # -- VAD -------------------------------------------------------
    def reset_vad(self) -> None:
        while not self.vad.empty():
            self.vad.pop()


# ── 主程序 ──────────────────────────────────────────────────────────
class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.voice = VoiceEngine(args.asr_threads, args.tts_threads)
        self.mic = Mic()
        self.speaker = Speaker(ratio=args.speaker_ratio)
        self.engine = None            # chat.Engine（LLM 子进程）
        self.cam_dev: str | None = None
        self.cam_desc = ""
        self.static_image = os.path.abspath(args.image) if args.image else None
        self.shot_path = "/tmp/talk_shot.jpg"
        self.image_is_fresh = False

    # -- LLM -------------------------------------------------------
    def _llm(self):
        from chat import Engine
        if self.engine is None:
            img = self.static_image or self.shot_path
            if not os.path.exists(img):
                raise RuntimeError(f"LLM 需要一张图启动，但没有: {img}")
            self.engine = Engine(img, self.args.tokens, 4096)
            self.engine.start()
        return self.engine

    def _switch_image(self, path: str) -> None:
        from chat import Engine
        if self.engine is not None:
            self.engine.stop()
        self.engine = Engine(path, self.args.tokens, 4096)
        self.engine.start()

    def think(self, question: str, need_image: bool) -> tuple[str, dict]:
        """返回 (回答, 各阶段耗时)。"""
        timing: dict[str, float] = {}

        if need_image:
            if self.static_image:
                img, fresh = self.static_image, True
            elif self.cam_dev:
                t = time.monotonic()
                fresh = grab_frame(self.cam_dev, self.shot_path)
                timing["capture"] = time.monotonic() - t
                img = self.shot_path
            else:
                log("警告", "没有摄像头，这一问只能靠文字回答", "yellow")
                need_image, fresh, img = False, False, self.shot_path
            if fresh:
                t = time.monotonic()
                self._switch_image(img)     # demo 只能在启动时加载图片
                timing["llm_reload"] = time.monotonic() - t
                self.image_is_fresh = True

        t = time.monotonic()
        eng = self._llm()
        prompt = ("<image>" if need_image else "") + question
        if self.args.brief:
            prompt += BRIEF_HINT
        pieces: list[str] = []
        stats = eng.ask(prompt, on_token=lambda s: pieces.append(s))
        timing["llm"] = time.monotonic() - t
        timing["ttft"] = stats.get("ttft", 0.0)
        return (stats.get("text") or "".join(pieces)).strip(), timing

    # -- 说 ---------------------------------------------------------
    def speak(self, text: str) -> float:
        speech = truncate_speech(clean_for_speech(text), self.args.speak_max)
        if not speech:
            return 0.0
        self.mic.close()                 # 半双工，播放时先放开麦克风
        t = time.monotonic()
        samples, rate, synth = self.voice.synthesize(speech)
        play = self.speaker.play(samples, rate)
        log("TTS", f"念 {len(speech)} 字：{speech[:60]}", "dim")
        log("TTS", f"合成 {synth:.2f}s + 播放 {play:.2f}s", "dim")
        return time.monotonic() - t

    # -- 一轮 -------------------------------------------------------
    def handle(self, question: str) -> bool:
        """处理一个问题。返回 False 表示退出。"""
        if any(h in question for h in EXIT_HINTS) and len(question) <= 8:
            self.speak("好的，再见。")
            return False

        need_image = any(h in question for h in VISION_HINTS) if not self.args.no_vision else False
        tag = "识物" if need_image else "对话"
        log("问", f"[{tag}] {question}", "cyan")

        try:
            answer, timing = self.think(question, need_image)
        except Exception as exc:                      # noqa: BLE001
            log("ERR", f"LLM 失败: {exc}", "red")
            self.speak("抱歉，我这边出了点问题。")
            return True

        parts = " ".join(f"{k}={v:.2f}s" for k, v in timing.items())
        log("答", answer.replace("\n", " ")[:160], "green")
        log("耗时", parts, "dim")
        self.speak(answer)
        return True

    # -- 麦克风循环 --------------------------------------------------
    def loop_mic(self) -> None:
        self.mic.open()
        self.voice.reset_vad()
        log("就绪", "请说话（说「退出」结束）", "green")
        say_count = 0
        while True:
            chunk = self.mic.read(512)
            if chunk is None:
                continue
            self.voice.vad.accept_waveform(chunk)
            while not self.voice.vad.empty():
                seg = np.array(self.voice.vad.front.samples)
                self.voice.vad.pop()
                if len(seg) < SAMPLE_RATE * 0.3:      # 太短，忽略
                    continue
                say_count += 1
                t = time.monotonic()
                text, dt = self.voice.transcribe(seg)
                log("听", f"{text!r}  ({len(seg)/SAMPLE_RATE:.1f}s 音频, ASR {dt*1000:.0f}ms)", "yellow")
                if not text:
                    continue
                if not self.handle(text):
                    self.mic.close()
                    return
                self.voice.reset_vad()
                self.mic.open()

    # -- 自检 -------------------------------------------------------
    def probe(self) -> int:
        print(paint("=== 设备自检 ===", "bold"))
        ok = True

        dev, desc = find_camera()
        self.cam_dev, self.cam_desc = dev, desc
        print(f"  摄像头   : {desc}")
        if dev:
            good = grab_frame(dev, self.shot_path)
            print(f"             抓帧 → {'成功 ' + self.shot_path if good else '失败'}")
            ok = ok and good
        elif not self.static_image:
            print(paint("             没有摄像头，也没有 --image，"
                        "只能做纯文本对话", "yellow"))
            ok = False

        r = subprocess.run([AREc, "-D", MIC_DEVICE, "-c", "1", "-r", "16000",
                            "-f", "S16_LE", "-t", "raw", "-d", "1", "-q", "-"],
                           capture_output=True, timeout=10)
        if r.returncode == 0 and len(r.stdout) > 1000:
            x = np.frombuffer(r.stdout[:len(r.stdout)//2*2], dtype=np.int16)
            log_peak = float(np.abs(x).max()) / 32768
            print(f"  麦克风   : OK  峰值 {log_peak:.3f}"
                  f"{'  ⚠ 偏大，可能削顶' if log_peak > 0.95 else ''}")
        else:
            print(paint("  麦克风   : 失败", "red"))
            ok = False

        r = subprocess.run([APlay, "-D", SPK_DEVICE, "-f", "S16_LE", "-r", "22050",
                            "-c", "1", "-q", "-"], input=b"\0" * 4000,
                           capture_output=True, timeout=10)
        print(f"  喇叭     : {'OK' if r.returncode == 0 else '失败 ' + r.stderr.decode()[:60]}")
        k = self.speaker.calibrate()
        print(f"  喇叭速率 : 实测为名义值的 {k:.2f} 倍"
              f"{'（已补偿）' if k > 1.2 else ''}")

        print("  模型     :", end=" ")
        missing = [p for p in (ASR_DIR, VAD_MODEL, TTS_DIR, TTS_VOCODER)
                   if not os.path.exists(p)]
        print("齐全" if not missing else f"缺少 {missing}")
        if missing:
            ok = False
        print(f"  LLM 目录 : {LLM_DIR} "
              f"{'OK' if os.path.exists(os.path.join(LLM_DIR, 'demo')) else '缺少 demo'}")
        print(f"  可用内存 : {self._mem()} MB")
        return 0 if ok else 1

    @staticmethod
    def _mem() -> int:
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            pass
        return 0

    # -- 入口 -------------------------------------------------------
    def run(self) -> int:
        a = self.args
        if a.probe:
            return self.probe()

        if not a.image:
            self.cam_dev, self.cam_desc = find_camera()
            log("摄像头", self.cam_desc, "dim")
            if not self.cam_dev:
                log("警告", "没有摄像头。识物问题会被跳过；"
                    "可用 --image 指定固定图片", "yellow")

        t = time.monotonic()
        log("加载", "ASR / TTS / VAD …", "dim")
        self.voice.load()
        log("加载", "  ".join(f"{k} {v:.1f}s" for k, v in self.voice.load_stats.items()), "green")

        log("校准", "喇叭速率（ES8389 时钟补偿）…", "dim")
        k = self.speaker.calibrate()
        log("校准", f"实际播放速率 = 名义 × {k:.2f}" +
            ("（已缓存）" if os.path.exists(SPEAKER_K_CACHE) else ""), "green")

        log("加载", "LLM（Qwen3.5-0.8B）…", "dim")
        try:
            self._llm()
        except Exception as exc:                        # noqa: BLE001
            log("ERR", f"LLM 启动失败: {exc}", "red")
            return 1
        log("就绪", f"总加载 {time.monotonic()-t:.1f}s", "green")

        try:
            if a.once:
                self.handle(a.once)
            elif a.wav:
                self.run_wav(a.wav)
            else:
                self.loop_mic()
        finally:
            self.mic.close()
            if self.engine:
                self.engine.stop()
        return 0

    def run_wav(self, path: str) -> None:
        with wave.open(path) as f:
            n, rate, ch = f.getnframes(), f.getframerate(), f.getnchannels()
            raw = f.readframes(n)
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        if rate != SAMPLE_RATE:
            log("警告", f"wav 采样率 {rate} != {SAMPLE_RATE}，可能影响识别", "yellow")

        segs = []
        for i in range(0, len(x), 512):
            self.voice.vad.accept_waveform(x[i:i + 512])
            while not self.voice.vad.empty():
                segs.append(np.array(self.voice.vad.front.samples))
                self.voice.vad.pop()
        if not segs and len(x):
            segs = [x]                                  # VAD 没切出来就整段识别

        for seg in segs:
            text, dt = self.voice.transcribe(seg)
            log("听", f"{text!r}  ({len(seg)/SAMPLE_RATE:.1f}s, ASR {dt*1000:.0f}ms)", "yellow")
            if text and not self.handle(text):
                break


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="talk.py", description="RK3576 本地语音对话 + 摄像头识物",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  ./talk.py                    麦克风对话\n"
               "  ./talk.py --image a.jpg      固定图片\n"
               "  ./talk.py --wav zh.wav       用音频文件测链路\n"
               "  ./talk.py --once 图里有什么   问一次\n"
               "  ./talk.py --probe            设备自检\n")
    ap.add_argument("-i", "--image", help="固定图片（代替摄像头）")
    ap.add_argument("-t", "--tokens", type=int, default=192, help="LLM 生成长度上限")
    ap.add_argument("--once", help="问一次就退出")
    ap.add_argument("--wav", help="用 wav 文件代替麦克风")
    ap.add_argument("--probe", action="store_true", help="只做设备自检")
    ap.add_argument("--no-vision", action="store_true", help="禁用摄像头，纯文本对话")
    ap.add_argument("--brief", action="store_true", default=True,
                    help="要求模型用短口语回答（默认开）")
    ap.add_argument("--no-brief", dest="brief", action="store_false")
    ap.add_argument("--speak-max", type=int, default=160,
                    help="最多朗读多少字（默认 160，0=不限制）")
    ap.add_argument("--speaker-ratio", type=float, default=0.0,
                    help="喇叭速率补偿倍数（默认自动校准并缓存）")
    ap.add_argument("--asr-threads", type=int, default=4)
    ap.add_argument("--tts-threads", type=int, default=4)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return App(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
