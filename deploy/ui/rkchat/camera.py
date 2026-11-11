#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""摄像头预览与抓帧。

设备：USB 摄像头 /dev/video73，只支持 YUYV 640x480@30fps（无 MJPEG、无更大分辨率）。

预览走 ffmpeg 输出 rawvideo rgb24：
  - 本机实测 640x480 rgb24 约 19.7 fps（sensor 上限），足够做预览
  - 直接用 numpy 从缓冲区 reshape 成 (H,W,3)，喂给 GdkPixbuf，不做 JPEG 解码
  - 注意：绝对不能传 bufsize=0，那会让每次 read 都退化成小 syscall，
    实测从 19.7 fps 掉到 0 fps（踩过这个坑）

抓帧另起一次 ffmpeg 存单张 JPEG，保证拿到的是干净的单帧。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

import numpy as np

DEFAULT_DEV = "/dev/video73"
PREVIEW_W, PREVIEW_H = 640, 480
PREVIEW_FPS = 30          # 让驱动给满帧，UI 侧自己限速
CAPTURE_SIZE = "640x480"
FFMPEG = "ffmpeg"

FRAME_BYTES = PREVIEW_W * PREVIEW_H * 3


def list_camera_devices() -> list[str]:
    out = []
    try:
        for name in os.listdir("/dev"):
            if name.startswith("video") and name[5:].isdigit():
                out.append(os.path.join("/dev", name))
    except OSError:
        pass
    return sorted(out, key=lambda p: int(p[5:]))


def capture_frame(path: str, dev: str = DEFAULT_DEV,
                  size: str = CAPTURE_SIZE, timeout: float = 8.0) -> bool:
    """抓一帧存成 JPEG。成功返回 True。"""
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-f", "v4l2", "-video_size", size, "-i", dev,
           "-frames:v", "1", "-q:v", "3", path]
    try:
        r = subprocess.run(cmd, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class PreviewStream:
    """持续从摄像头取 RGB 帧，只保留最新一帧。

    用法：
        st = PreviewStream(); st.start()
        rgb, seq = st.latest()      # rgb 是 (H,W,3) uint8，可能为 None
        st.snapshot("/tmp/a.jpg")   # 把最新帧存成图片
        st.stop()
    """

    def __init__(self, dev: str = DEFAULT_DEV,
                 w: int = PREVIEW_W, h: int = PREVIEW_H,
                 fps: int = PREVIEW_FPS):
        self.dev = dev
        self.w = w
        self.h = h
        self.fps = fps
        self._nbytes = w * h * 3
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._frame: np.ndarray | None = None
        self._frame_time: float | None = None
        self._seq = 0
        self._epoch = 0
        self._running = False
        self.last_error: str | None = None

    def start(self) -> bool:
        if self.running:
            return True
        self.stop()
        cmd = [FFMPEG, "-loglevel", "error",
               "-f", "v4l2",
               "-video_size", "%dx%d" % (self.w, self.h),
               "-framerate", str(self.fps),
               "-i", self.dev,
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.last_error = str(exc)
            return False
        with self._frame_ready:
            self._epoch += 1
            epoch = self._epoch
            self._proc = proc
            self._running = True
            self.last_error = None
            self._frame = self._frame_time = None
            thread = threading.Thread(target=self._pump, args=(proc, epoch), daemon=True)
            self._thread = thread
        thread.start()
        return True

    def stop(self):
        with self._frame_ready:
            proc, thread = self._proc, self._thread
            self._epoch += 1
            self._running = False
            self._proc = self._thread = None
            self._frame = self._frame_time = None
            self._frame_ready.notify_all()
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=2)

    @property
    def running(self) -> bool:
        with self._lock:
            return (self._running and self._proc is not None and self._proc.poll() is None
                    and self._thread is not None and self._thread.is_alive())

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def frame_age(self) -> float | None:
        with self._lock:
            return None if self._frame_time is None else time.monotonic() - self._frame_time

    def latest(self) -> tuple[np.ndarray | None, int]:
        # 不 copy：采集线程每次都从新 buf 生成新数组，发布后不再改写，
        # 所以 UI 直接拿引用是安全的。copy 会持锁 600KB memcpy，拖慢采集。
        with self._lock:
            return self._frame, self._seq

    # ── 从流里取快照 ───────────────────────────────────────────
    def snapshot(self, path: str) -> bool:
        """Compatibility API: only write a recent frame from the active stream."""
        return self.fresh_snapshot(path, timeout=0) is not None

    def fresh_snapshot(self, path: str, min_seq: int = -1, epoch: int | None = None,
                       timeout: float = 1.5, max_age: float = 1.0) -> dict | None:
        """Wait off the GTK thread for a newer, non-stalled frame in this session."""
        deadline = time.monotonic() + timeout
        with self._frame_ready:
            wanted_epoch = self._epoch if epoch is None else epoch
            while True:
                if (wanted_epoch != self._epoch or not self._running or self._proc is None
                        or self._proc.poll() is not None):
                    return None
                now = time.monotonic()
                if (self._frame is not None and self._frame_time is not None
                        and self._seq > min_seq and now - self._frame_time <= max_age):
                    arr, seq, stamp = self._frame, self._seq, self._frame_time
                    break
                remaining = deadline - now
                if remaining <= 0:
                    return None
                self._frame_ready.wait(timeout=min(remaining, 0.2))
        if not write_bmp(path, arr):
            return None
        return {"seq": seq, "epoch": wanted_epoch, "frame_time": stamp,
                "age_ms": round((time.monotonic() - stamp) * 1000, 1)}

    def _pump(self, proc, epoch):
        if proc.stdout is None:
            return
        try:
            while True:
                with self._lock:
                    if not self._running or self._epoch != epoch:
                        break
                buf = proc.stdout.read(self._nbytes)
                if len(buf) < self._nbytes:
                    break
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(self.h, self.w, 3)
                with self._frame_ready:
                    if not self._running or self._epoch != epoch:
                        break
                    self._frame = arr
                    self._frame_time = time.monotonic()
                    self._seq += 1
                    self._frame_ready.notify_all()
        except (OSError, ValueError) as exc:
            with self._lock:
                if self._epoch == epoch:
                    self.last_error = str(exc)
        finally:
            with self._frame_ready:
                if self._epoch == epoch:
                    self._running = False
                    self._frame = self._frame_time = None
                    self.last_error = self.last_error or "摄像头停止输出新画面"
                    self._frame_ready.notify_all()
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()



# ── 无依赖的 BMP 写出（stb_image / OpenCV 都能读）─────────────────
def write_bmp(path: str, rgb: "np.ndarray") -> bool:
    """把 (h,w,3) uint8 写成 24bit BMP。失败返回 False。"""
    try:
        h, w = rgb.shape[0], rgb.shape[1]
        # BMP 行按 4 字节对齐，且像素存的是 BGR + 自下而上
        row_raw = w * 3
        pad = (-row_raw) % 4
        row = row_raw + pad
        size = 54 + row * h
        hdr = bytearray(54)
        hdr[0:2] = b"BM"
        hdr[2:6] = size.to_bytes(4, "little")
        hdr[10:14] = (54).to_bytes(4, "little")      # 像素数据偏移
        hdr[14:18] = (40).to_bytes(4, "little")      # DIB 头大小
        hdr[18:22] = w.to_bytes(4, "little", signed=True)
        hdr[22:26] = h.to_bytes(4, "little", signed=True)
        hdr[26:28] = (1).to_bytes(2, "little")       # planes
        hdr[28:30] = (24).to_bytes(2, "little")      # bpp
        hdr[34:38] = (row * h).to_bytes(4, "little")
        # BGR 并翻转行序
        bgr = rgb[:, :, ::-1]
        body = bgr.tobytes()
        with open(path, "wb") as f:
            f.write(bytes(hdr))
            if pad == 0:
                for y in range(h - 1, -1, -1):
                    f.write(body[y * row_raw:(y + 1) * row_raw])
            else:
                padb = b"\x00" * pad
                for y in range(h - 1, -1, -1):
                    f.write(body[y * row_raw:(y + 1) * row_raw])
                    f.write(padb)
        return os.path.getsize(path) > 54
    except Exception:
        return False


if __name__ == "__main__":
    st = PreviewStream()
    if not st.start():
        print("启动失败:", st.last_error)
        raise SystemExit(1)
    t0 = time.time()
    n = 0
    last = -1
    while time.time() - t0 < 3.0:
        _, seq = st.latest()
        if seq != last:
            n += 1
            last = seq
        time.sleep(0.01)
    st.stop()
    print("3 秒内收到 %d 帧，约 %.1f fps" % (n, n / 3.0))
    ok = capture_frame("/tmp/_cap_test.jpg")
    print("抓帧:", ok, os.path.getsize("/tmp/_cap_test.jpg") if ok else 0, "字节")
