"""DOSBox Staging raw screenshots (Ctrl+F5 with default_image_capture_formats
= raw) paired with the guest's video memory: gives the palette colour of
every index on screen."""
import os
import pathlib
import struct
import time
import zlib


def read_png_rgb(path):
    d = pathlib.Path(path).read_bytes()
    pos, idat, w = 8, b"", 0
    while pos < len(d):
        n, = struct.unpack(">I", d[pos:pos + 4])
        t = d[pos + 4:pos + 8]
        body = d[pos + 8:pos + 8 + n]
        if t == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", body[:10])
            assert bd == 8 and ct == 2, (bd, ct)
        elif t == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    while i < len(raw):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - 3] if x >= 3 else 0
            b = prev[x]
            c = prev[x - 3] if x >= 3 else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out += line
        prev = line
    return w, bytes(out)


def grab(session, capdir):
    """Screenshot + video memory; returns (rgb, vram) for 320x200."""
    capdir = pathlib.Path(capdir)
    before = set(os.listdir(capdir))
    vram = session.api.read(0xA0000, 64000)
    w = session.window()
    w.key("ctrl")
    w.key("f5")
    time.sleep(0.08)
    w.key("f5", up=True)
    w.key("ctrl", up=True)
    end = time.monotonic() + 5
    while time.monotonic() < end:
        new = set(os.listdir(capdir)) - before
        if new:
            time.sleep(0.3)
            width, rgb = read_png_rgb(capdir / sorted(new)[-1])
            return rgb, vram
        time.sleep(0.1)
    raise RuntimeError("no screenshot")


def palette(rgb, vram):
    pal = {}
    for i, idx in enumerate(vram):
        pal.setdefault(idx, tuple(rgb[i * 3:i * 3 + 3]))
    return pal
