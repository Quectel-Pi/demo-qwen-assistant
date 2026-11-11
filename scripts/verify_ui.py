#!/usr/bin/env python3
"""验证UI改动：截图 + 像素统计（设备端、无PIL版本，用 numpy 解码 xwd）。

用法（在设备上运行）:
    python3 verify_ui.py shot /tmp/shot.png
    python3 verify_ui.py stats /tmp/shot.png
    python3 verify_ui.py shot-stats /tmp/shot.png   # 一步到位
"""
import struct
import subprocess
import sys
import zlib

XWD_HDR = 100


def find_window():
    out = subprocess.run(["xwininfo", "-root", "-tree"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if '"rkchat": ("mutter-x11-frames"' in line:
            return line.split()[0]
    return None


def png_write(path, rgb, w, h):
    """最小 PNG 编码器（RGB8，无滤波），避免依赖 PIL。"""
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filter type 0
        raw += rgb[y * w * 3:(y + 1) * w * 3]

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def shot(path):
    wid = find_window()
    if not wid:
        print("ERROR: rkchat window not found", file=sys.stderr)
        return 1
    xwd = "/tmp/_shot.xwd"
    r = subprocess.run(["xwd", "-id", wid, "-display", ":0", "-out", xwd],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERROR xwd: {r.stderr}", file=sys.stderr)
        return 1
    d = open(xwd, "rb").read()
    if len(d) < XWD_HDR:
        print("ERROR: xwd too short", file=sys.stderr)
        return 1
    h = struct.unpack(">25I", d[:XWD_HDR])
    hdr_size, w, hh, bpl = h[0], h[4], h[5], h[12]
    pix = d[hdr_size:hdr_size + bpl * hh]
    # BGRX -> RGB，逐行去掉 padding
    rgb = bytearray(w * hh * 3)
    for y in range(hh):
        row = pix[y * bpl:y * bpl + w * 4]
        for x in range(w):
            o = (y * w + x) * 3
            s = x * 4
            rgb[o] = row[s + 2]
            rgb[o + 1] = row[s + 1]
            rgb[o + 2] = row[s]
    png_write(path, rgb, w, hh)
    print(f"OK {path} size={w}x{hh} wid={wid}")
    return 0


def stats(path):
    """不用 PIL：直接从生成的 PNG 里读回像素统计。"""
    import numpy as np
    d = open(path, "rb").read()
    # 解析我们自己的 PNG（单 IDAT、无滤波）
    pos = 8
    w = hh = 0
    idat = b""
    while pos < len(d):
        ln = struct.unpack(">I", d[pos:pos + 4])[0]
        tag = d[pos + 4:pos + 8]
        data = d[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, hh = struct.unpack(">II", data[:8])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    raw = zlib.decompress(idat)
    a = np.frombuffer(raw, dtype=np.uint8).reshape(hh, 1 + w * 3)[:, 1:]
    a = a.reshape(hh, w, 3).astype(np.int32)
    flat = a.reshape(-1, 3)
    uniq = len(np.unique(flat[:, 0] * 65536 + flat[:, 1] * 256 + flat[:, 2]))
    nonblack = (flat.sum(1) > 20).mean() * 100
    print(f"size={w}x{hh} unique_colors={uniq} nonblack={nonblack:.1f}% "
          f"mean={flat.mean():.1f}")
    ok = uniq > 1000 and nonblack > 90
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cmd, arg = sys.argv[1], sys.argv[2]
    if cmd == "shot":
        sys.exit(shot(arg))
    if cmd == "stats":
        sys.exit(stats(arg))
    if cmd == "shot-stats":
        rc = shot(arg)
        sys.exit(rc or stats(arg))
    print(__doc__)
    sys.exit(2)
