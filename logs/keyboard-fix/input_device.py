#!/usr/bin/env python3
"""Temporary kernel input device for end-to-end desktop input tests.
Creates a virtual touchscreen/keyboard, performs requested events, then removes it.
"""
import array, fcntl, os, struct, sys, time
fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
mode = sys.argv[1]
EV_KEY, EV_ABS, EV_SYN = 1, 3, 0
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
try:
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    mins, maxs = [0] * 64, [0] * 64
    if mode == 'tap':
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(fd, UI_SET_KEYBIT, 330)
        for code, maxvalue in [(0, 1279), (1, 799), (47, 9), (53, 1279), (54, 799), (57, 65535)]:
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            maxs[code] = maxvalue
    else:
        for code in range(1, 256):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
    name = b'RKChat temporary verification ' + mode.encode()
    data = struct.pack('80sHHHHI', name, 3, 0x1209, 0x0001, 1, 0)
    data += array.array('i', maxs + mins + [0] * 128).tobytes()
    os.write(fd, data)
    fcntl.ioctl(fd, 0x5501)
    time.sleep(1.2)
    def event(typ, code, value):
        t = time.time()
        os.write(fd, struct.pack('llHHi', int(t), int(t % 1 * 1000000), typ, code, value))
    if mode == 'tap':
        x, y = map(int, sys.argv[2:4])
        for c, v in [(47, 0), (57, 1), (0, x), (1, y), (53, x), (54, y)]:
            event(EV_ABS, c, v)
        event(EV_KEY, 330, 1); event(EV_SYN, 0, 0)
        time.sleep(0.15)
        event(EV_ABS, 57, -1); event(EV_KEY, 330, 0); event(EV_SYN, 0, 0)
    else:
        codes = [int(x) for x in sys.argv[2].split(',')]
        if mode == 'chord':
            for c in codes: event(EV_KEY, c, 1); event(EV_SYN, 0, 0)
            for c in reversed(codes): event(EV_KEY, c, 0); event(EV_SYN, 0, 0)
        else:
            for c in codes:
                event(EV_KEY, c, 1); event(EV_SYN, 0, 0)
                event(EV_KEY, c, 0); event(EV_SYN, 0, 0)
                time.sleep(0.08)
    time.sleep(1.0)
finally:
    try: fcntl.ioctl(fd, 0x5502)
    finally: os.close(fd)
