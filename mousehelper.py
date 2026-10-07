#!/usr/bin/env python3
"""Mouse helpers for WarCraft: Orcs & Humans, Dune II and SkyNET / The
Terminator: Future Shock running in DOSBox Staging 0.83 or newer.

A platform-independent port of Osprey's AutoHotkey "mouse helpers" (2018).
The originals hook the Windows keyboard and mouse and sample screen pixels.
This port works inside the emulated PC instead: through DOSBox Staging's HTTP
API it plants a small resident program (guest/mh.asm) between the game and
the mouse driver / keyboard controller, and runs the helper logic here.

Usage (wrapper, like the original helpers):
    mousehelper.py --game warcraft -- <dosbox> [dosbox arguments...]
The helper starts DOSBox with the API enabled on a free port and exits when
DOSBox exits.  --attach PORT connects to a DOSBox that is already running
with the API enabled instead.
"""

import argparse
import base64
import http.client
import json
import os
import pathlib
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

import guestcode

VERSION = "0.4"
SYM = guestcode.SYMBOLS
HERE = pathlib.Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "mousehelper.json"

EV_MOUSE, EV_KEY, EV_INFO = 1, 2, 3
KF_DROPPED, KF_INJECTED, KF_E1PART, KF_EXTENDED = 1, 2, 4, 8
INFO_SETCB, INFO_RESET, INFO_INT9_ON, INFO_INT9_OFF = 1, 2, 3, 4
CQ_KEY, CQ_BTN_DOWN, CQ_BTN_UP, CQ_WAIT_POLL = 1, 2, 3, 4
EV_COUNT = 64
CQ_COUNT = 64
VRAM_RUNS, VRAM_BUF = 16, 128   # as in mh.asm
BTN_L, BTN_R, BTN_M = 1, 2, 4

# Scancode set 1 (make codes).  Arrow keys are the keypad codes without the
# E0 prefix: one-byte injections cannot be split by a real keystroke.
SCAN = {
    "esc": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B, "-": 0x0C,
    "=": 0x0D, "backspace": 0x0E, "tab": 0x0F, "q": 0x10, "w": 0x11,
    "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16, "i": 0x17,
    "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B, "enter": 0x1C, "ctrl": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28, "`": 0x29,
    "shift": 0x2A, "\\": 0x2B, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F,
    "b": 0x30, "n": 0x31, "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35,
    "rshift": 0x36, "alt": 0x38, "space": 0x39, "capslock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57,
    "f12": 0x58, "home": 0x47, "up": 0x48, "pgup": 0x49, "left": 0x4B,
    "right": 0x4D, "end": 0x4F, "down": 0x50, "pgdn": 0x51,
}
NAME = {v: k for k, v in SCAN.items()}


def log(*args):
    now = time.time()
    print(time.strftime("%H:%M:%S", time.localtime(now))
          + f".{int(now * 1000) % 1000:03d}", *args, flush=True)


# ---------------------------------------------------------------------------
# DOSBox Staging API
# ---------------------------------------------------------------------------
class ApiError(Exception):
    pass


class ApiBusy(ApiError):
    """DOSBox did not run the command in time (e.g. window being dragged)."""


class GuestLost(Exception):
    """The helper's block no longer holds the helper (the game wrote over
    it): nothing read from it may drive a loop or an address any more."""


class Api:
    def __init__(self, port, timeout=2.0):
        self.host = "127.0.0.1"   # not "localhost": ::1 first costs ~2 s/request
        self.port = port
        self.timeout = timeout
        self.lock = threading.Lock()
        self.conn = None

    def _request(self, method, path, body=None, ctype=None, extra=None):
        headers = {"Host": f"{self.host}:{self.port}"}
        if ctype:
            headers["Content-Type"] = ctype
        if extra:
            headers.update(extra)
        with self.lock:
            for attempt in (0, 1):
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(
                        self.host, self.port, timeout=self.timeout)
                try:
                    self.conn.request(method, path, body=body, headers=headers)
                    resp = self.conn.getresponse()
                    data = resp.read()
                except (OSError, http.client.HTTPException):
                    self.conn.close()
                    self.conn = None
                    if attempt:
                        raise
                    continue
                if resp.status == 200:
                    return data
                text = data.decode("utf-8", "replace")
                if resp.status == 500 and "timeout" in text:
                    raise ApiBusy(text)
                raise ApiError(f"{method} {path}: {resp.status} {text}")

    def read(self, addr, length):
        return self._request("GET", f"/api/v1/memory/{addr}/{length}")

    def read_seg(self, seg, off, length):
        return self._request("GET", f"/api/v1/memory/{seg}/{off}/{length}")

    def write(self, addr, data, expect=None):
        """Write; with `expect`, only if memory still holds those bytes
        (atomic in the emulation).  Returns False if it did not."""
        headers = {}
        if expect is not None:
            headers["If-Match"] = base64.b64encode(bytes(expect)).decode()
        try:
            self._request("PUT", f"/api/v1/memory/{addr}", bytes(data),
                          "application/octet-stream", headers)
        except ApiError as e:
            if " 412 " in str(e):
                return False
            raise
        return True

    def alloc(self, size, area="conv", strategy="last_fit"):
        body = json.dumps({"size": size, "area": area, "strategy": strategy})
        return json.loads(self._request("POST", "/api/v1/memory/allocate",
                                        body, "application/json"))["addr"]

    def cpu(self):
        return json.loads(self._request("GET", "/api/v1/cpu/state"))["registers"]

    def info(self):
        return json.loads(self._request("GET", "/api/v1/dosbox/info"))

    def info_dos(self):
        return json.loads(self._request("GET", "/api/v1/dos/internals"))


# ---------------------------------------------------------------------------
# The resident program in the guest
# ---------------------------------------------------------------------------
class Guest:
    POLL_LEN = SYM["cq_buf"] + CQ_COUNT * 4     # the data; code follows
    HEADER = bytes(guestcode.BLOB[:8])
    CODE = bytes(guestcode.BLOB[POLL_LEN:])

    def __init__(self, api):
        self.api = api
        self.base = None          # linear address of the block
        self.seg = None
        self.lost = None          # why the block is no longer ours
        self.vesa_why = ""        # why vesa_read() gave None
        self.qlock = threading.Lock()
        self.cq_head = 0
        self.cq_tail = 0
        self.ev_tail = 0
        self.counters = {}

    # -- installation -----------------------------------------------------
    def find_existing(self):
        """Segment of a block a previous helper run left in this DOSBox
        (conventional memory or the upper memory chain)."""
        lol = self.api.info_dos()["listOfLists"]
        starts = [struct.unpack("<H", self.api.read(lol - 2, 2))[0],
                  struct.unpack("<H", self.api.read(lol + 0x66, 2))[0]]
        for seg in starts:
            if seg in (0, 0xFFFF):
                continue
            for _ in range(4096):
                mcb = self.api.read(seg * 16, 16)
                if mcb[0] not in (0x4D, 0x5A):
                    break
                size = struct.unpack_from("<H", mcb, 3)[0]
                if mcb[8:16] == b"MOUSEHLP":
                    hdr = self.api.read((seg + 1) * 16, 8)
                    if hdr == bytes(guestcode.BLOB[:8]):
                        return seg + 1
                    log("an incompatible helper block is installed; "
                        "not reusing it")
                    return None
                if mcb[0] == 0x5A:
                    break
                seg += size + 1
        return None

    def install(self, areas=("uma", "conv")):
        """Reuse the block or allocate one in the first of `areas` ("uma",
        "conv") that has room."""
        seg = self.find_existing()
        if seg is not None:
            self.base, self.seg = seg * 16, seg
            raw = self.api.read(self.base, self.POLL_LEN)
            self.check(raw)
            self.ev_tail = struct.unpack_from("<H", raw, SYM["ev_head"])[0]
            self.put("ev_tail", struct.pack("<H", self.ev_tail))
            self.cq_head = struct.unpack_from("<H", raw, SYM["cq_tail"])[0]
            self.put("cq_head", struct.pack("<H", self.cq_head))
            for name in ("cfg_enabled", "kbd_active", "busy", "inj_inflight"):
                self.put8(name, 0)
            log(f"reusing the helper block at {seg:04X}:0000")
        else:
            blob = bytearray(guestcode.BLOB)
            for n, area in enumerate(areas):
                try:
                    addr = self.api.alloc(len(blob), area, "last_fit")
                    break
                except ApiBusy:
                    raise
                except ApiError:
                    if n == len(areas) - 1:
                        raise
            if addr & 15:
                raise ApiError(f"allocation not paragraph aligned: {addr:#x}")
            seg = addr >> 4
            ivt = self.api.read(0, 0x400)
            struct.pack_into("<I", blob, SYM["old_int33"],
                             struct.unpack_from("<I", ivt, 0x33 * 4)[0])
            struct.pack_into("<I", blob, SYM["old_int8"],
                             struct.unpack_from("<I", ivt, 0x08 * 4)[0])
            struct.pack_into("<I", blob, SYM["bios_int9"],
                             struct.unpack_from("<I", ivt, 0x09 * 4)[0])
            struct.pack_into("<I", blob, SYM["old_int70"],
                             struct.unpack_from("<I", ivt, 0x70 * 4)[0])
            blob[SYM["ms_need_cb"]] = 1
            blob[SYM["rtc_wanted"]] = 1
            self.api.write(addr, blob)
            # owner 0008h ("DOS"): stays when the program that was current
            # exits; the name marks a complete block for find_existing()
            mcb = (seg - 1) * 16
            self.api.write(mcb + 1, struct.pack("<H", 8))
            self.api.write(mcb + 8, b"MOUSEHLP")
            self.base, self.seg = addr, seg
            log(f"guest installed at {seg:04X}:0000 ({len(blob)} bytes)")
        self.hook_vectors()

    def hook_vectors(self):
        """Point INT 33h and INT 8 at the block unless they already are (or
        something else has hooked them since: then leave them alone)."""
        ivt = self.api.read(0, 0x400)
        raw = self.api.read(self.base, SYM["cq_buf"])
        for vec, hook, old in ((0x33, "int33_hook", "old_int33"),
                               (0x08, "int8_hook", "old_int8"),
                               (0x70, "int70_hook", "old_int70")):
            ours = struct.pack("<HH", SYM[hook], self.seg)
            cur = ivt[vec * 4:vec * 4 + 4]
            if cur == ours:
                continue
            if cur == raw[SYM[old]:SYM[old] + 4]:
                self.api.write(vec * 4, ours)
            else:
                log(f"INT {vec:02X}h was hooked by something else; not hooking")

    def linear(self, sym):
        return self.base + SYM[sym]

    # -- shared data --------------------------------------------------------
    def put(self, sym, data):
        self.put_raw(SYM[sym], data)

    def put8(self, sym, value):
        self.put(sym, bytes([value & 0xFF]))

    def lose(self):
        self.lost = (f"the game overwrote the helper's memory at "
                     f"{self.seg:04X}:0000")
        raise GuestLost(self.lost)

    def check(self, raw):
        """The data area must still be the helper's: its ring indices drive
        loops and addresses."""
        if self.lost:
            raise GuestLost(self.lost)
        if (raw[:8] != self.HEADER
                or struct.unpack_from("<H", raw, SYM["ev_head"])[0] >= EV_COUNT
                or struct.unpack_from("<H", raw, SYM["cq_tail"])[0] >= CQ_COUNT):
            self.lose()

    def check_code(self):
        if self.api.read(self.base + self.POLL_LEN, len(self.CODE)) != self.CODE:
            self.lose()

    def poll(self):
        """Read the data area; returns the list of new events."""
        raw = self.api.read(self.base, self.POLL_LEN)
        self.check(raw)
        self.raw = raw
        head = struct.unpack_from("<H", raw, SYM["ev_head"])[0]
        self.cq_tail = struct.unpack_from("<H", raw, SYM["cq_tail"])[0]
        events = []
        tail = self.ev_tail
        while tail != head:
            off = SYM["ev_buf"] + tail * 8
            typ, a, b, c, x, y = struct.unpack_from("<BBBBhh", raw, off)
            events.append((typ, a, b, c, x, y))
            tail = (tail + 1) % EV_COUNT
        if events:
            self.ev_tail = tail
            self.put("ev_tail", struct.pack("<H", tail))
        return events

    def counter(self, name):
        return struct.unpack_from("<H", self.raw, SYM[name])[0]

    def program(self):
        """Name of the running DOS program (from its memory block), upper
        case; DOS/4GW games show their own name, the extender loads them
        as an overlay of the same process."""
        if not hasattr(self, "sda"):
            self.sda = self.api.info_dos()["dosSwappableArea"]
        psp = struct.unpack("<H", self.api.read(self.sda + 0x10, 2))[0]
        if psp < 0x40:
            return ""
        name = self.api.read((psp - 1) * 16 + 8, 8)
        name = name.split(b"\0")[0].decode("ascii", "replace").upper()
        if name.startswith("DOS4G"):        # the extender run by the game's stub
            parent = struct.unpack("<H", self.api.read(psp * 16 + 0x16, 2))[0]
            if 0x40 <= parent != psp:
                name = self.api.read((parent - 1) * 16 + 8, 8)
                name = name.split(b"\0")[0].decode("ascii", "replace").upper()
        return name

    def vram(self):
        """Mode 13h screen (320x200 palette indices)."""
        return self.api.read(0xA0000, 64000)

    def dac(self, indices, timeout=0.5):
        """VGA palette entries (8-bit RGB as DOSBox shows them) of up to 16
        indices, read by the guest on its next tick."""
        indices = list(indices)[:16]
        seq = self.api.read(self.linear("dac_seq"), 1)[0]
        self.put("dac_idx", bytes(indices))
        self.put8("dac_req", len(indices))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.api.read(self.linear("dac_seq"), 1)[0] != seq:
                raw = self.api.read(self.linear("dac_rgb"), 3 * len(indices))
                return {i: tuple((v & 63) * 255 // 63 for v in raw[n * 3:n * 3 + 3])
                        for n, i in enumerate(indices)}
            time.sleep(0.005)
        self.put8("dac_req", 0)
        return None

    def vesa_read(self, runs, timeout=0.25):
        """Video memory the 64 KB window does not show (VESA modes), copied
        by the guest on its next tick.  runs: (offset, length) pairs.
        Returns (bytes, scan line length, offset of the display start), or
        None."""
        entries = bytearray()
        total = 0
        for off, n in runs:
            while n > 0:                    # one 64 KB bank per entry
                part = min(n, 0x10000 - (off & 0xFFFF))
                entries += struct.pack("<BBH", off >> 16, part, off & 0xFFFF)
                total += part
                off += part
                n -= part
        if len(entries) > VRAM_RUNS * 4 or total > VRAM_BUF:
            raise ValueError("too much video memory for one request")
        seq = self.api.read(self.linear("vram_seq"), 1)[0]
        self.put("vram_run", bytes(entries))
        self.put8("vram_req", len(entries) // 4)
        end = time.monotonic() + timeout
        base = SYM["vram_seq"]
        while time.monotonic() < end:
            raw = self.api.read(self.linear("vram_seq"), SYM["vram_run"] - base)
            if raw[0] != seq:
                if not raw[SYM["vram_ok"] - base]:
                    self.vesa_why = "refused by the VESA BIOS"
                    return None
                pitch, x, y = struct.unpack_from("<HHH", raw,
                                                 SYM["vram_pitch"] - base)
                data = self.api.read(self.linear("vram_buf"), total)
                return data, pitch, y * pitch + x
            time.sleep(0.005)
        self.put8("vram_req", 0)
        self.vesa_why = "no answer from the guest"
        return None

    def owner(self, seg, mem=None):
        """Name of the program whose memory block holds segment `seg`."""
        if mem is None:
            mem = self.api.read(0, 0xA0000)
        lol = self.api.info_dos()["listOfLists"]
        m = struct.unpack("<H", self.api.read(lol - 2, 2))[0]
        for _ in range(4096):
            if (m + 1) * 16 + 16 > len(mem):
                return ""
            sig = mem[m * 16]
            if sig not in (0x4D, 0x5A):
                return ""
            owner, size = struct.unpack_from("<HH", mem, m * 16 + 1)
            if m < seg <= m + size:
                if owner < 0x40:
                    return "DOS"
                name = mem[(owner - 1) * 16 + 8:(owner - 1) * 16 + 16]
                return name.split(b"\0")[0].decode("ascii", "replace").upper()
            if sig == 0x5A:
                return ""
            m += size + 1
        return ""

    def maintain_int9(self, programs):
        """Real-mode games: ask the guest to slip its keyboard filter in
        front of the game's own INT 9 handler."""
        vec = self.api.read(9 * 4, 4)
        off, seg = struct.unpack("<HH", vec)
        if (off, seg) == (SYM["int9_hook"], self.seg) or seg >= 0xF000:
            return
        if self.owner(seg) in programs:
            self.put("int9_req", vec)

    def byte(self, name):
        return self.raw[SYM[name]]

    # -- commands -------------------------------------------------------------
    def send(self, ops):
        """Queue (op, arg) pairs; returns the queue index after the last one."""
        with self.qlock:
            if not ops:
                return self.cq_head
            entries = bytearray()
            head = self.cq_head
            for op, arg in ops:
                entries += bytes([op, arg & 0xFF, 0, 0])
            # contiguous writes, split at the ring's end
            first = min(len(ops), CQ_COUNT - head)
            self.put_raw(SYM["cq_buf"] + head * 4, entries[:first * 4])
            if first < len(ops):
                self.put_raw(SYM["cq_buf"], entries[first * 4:])
            self.cq_head = (head + len(ops)) % CQ_COUNT
            self.put("cq_head", struct.pack("<H", self.cq_head))
            return self.cq_head

    def put_raw(self, off, data):
        if self.lost:
            raise GuestLost(self.lost)
        self.api.write(self.base + off, data)

    def read_cq_tail(self):
        if self.lost:
            raise GuestLost(self.lost)
        tail = struct.unpack("<H", self.api.read(self.linear("cq_tail"), 2))[0]
        if tail >= CQ_COUNT:
            self.lose()
        return tail

    def drained(self, ticket):
        tail = self.read_cq_tail()
        self.cq_tail = tail
        return tail == ticket

    def wait(self, ticket, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.drained(ticket):
                return True
            time.sleep(0.002)
        return False

    def flush(self):
        """Drop whatever is still queued."""
        with self.qlock:
            tail = self.read_cq_tail()
            self.cq_head = tail
            self.put("cq_head", struct.pack("<H", tail))


# ---------------------------------------------------------------------------
# Helper logic framework (AutoHotkey-like)
# ---------------------------------------------------------------------------
def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(all_settings):
    try:
        SETTINGS_FILE.write_text(json.dumps(all_settings, indent=2) + "\n")
    except OSError as e:
        log(f"cannot save settings: {e}")


def use_settings_file(path):
    global SETTINGS_FILE
    SETTINGS_FILE = pathlib.Path(path)


class Splash:
    """Console stand-in for AutoHotkey's SplashImage (no tkinter)."""

    def show(self, text, title=None, width=None, timeout=None):
        log(("[" + title + "] ") if title else "", text.replace("\n", " | "))

    def hide(self):
        pass

    def mainloop(self, worker):
        worker.join()


class TkSplash(Splash):
    """SplashImage stand-in: a borderless always-on-top window centred on the
    primary screen.  It is never withdrawn (showing a withdrawn window can
    take the focus from DOSBox); when hidden it is transparent and parked
    off-screen.  On Windows it also gets WS_EX_NOACTIVATE.  Tk runs in the
    main thread; show/hide may be called from any thread."""

    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont
        self.q = queue.Queue()
        prev = None
        if sys.platform == "win32":
            import ctypes
            prev = ctypes.windll.user32.GetForegroundWindow()
        self.root = root = tk.Tk()
        root.title("Mouse helper")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        root.geometry("1x1+-10000+-10000")
        frame = tk.Frame(root, bd=1, relief="solid", padx=6, pady=4)
        frame.pack(fill="both", expand=True)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.title_label = tk.Label(frame, font=bold)
        self.text_label = tk.Label(frame, justify="left", anchor="w")
        self.title_label.pack(fill="x")
        self.text_label.pack(fill="both", expand=True)
        root.update()
        if sys.platform == "win32":
            self._no_activate()
            # creating the window took the foreground: give it back
            import ctypes
            user32 = ctypes.windll.user32
            if prev and user32.GetForegroundWindow() != prev:
                user32.SetForegroundWindow(prev)
        self.hide_job = None

    def _no_activate(self):
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(self.root.wm_frame(), 16)
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= 0x08000000 | 0x00000080 | 0x00000008  # NOACTIVATE TOOLWINDOW TOPMOST
        style &= ~0x00040000                            # APPWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

    # thread-safe API
    def show(self, text, title=None, width=None, timeout=None):
        self.q.put(("show", text, title, width, timeout))

    def hide(self):
        self.q.put(("hide",))

    # Tk side
    def _show(self, text, title, width, timeout):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        if title:
            self.title_label.configure(text=title)
            self.title_label.pack(fill="x", before=self.text_label)
            self.text_label.configure(justify="left", anchor="w")
        else:
            self.title_label.pack_forget()
            self.text_label.configure(justify="center", anchor="center")
        self.text_label.configure(text=text,
                                  wraplength=(width - 16) if width else 0)
        self.root.update_idletasks()
        w = max(width or 0, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.attributes("-alpha", 1.0)
        if timeout:
            self.hide_job = self.root.after(int(timeout * 1000), self._hide)

    def _hide(self):
        self.hide_job = None
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("1x1+-10000+-10000")

    def mainloop(self, worker):
        def pump():
            try:
                while True:
                    cmd = self.q.get_nowait()
                    if cmd[0] == "show":
                        self._show(*cmd[1:])
                    else:
                        self._hide()
            except queue.Empty:
                pass
            if worker.is_alive():
                self.root.after(30, pump)
            else:
                self.root.destroy()
        self.root.after(30, pump)
        self.root.mainloop()


def make_splash(args):
    if args.no_splash:
        return Splash()
    try:
        return TkSplash()
    except Exception as e:      # no tkinter / no display
        log(f"on-screen messages unavailable ({e}); using the console")
        return Splash()


class Brain:
    """Common machinery: physical input state from the guest's events, hotkey
    threads, sending keys and clicks."""

    key = None                  # settings key / --game name
    title = None
    defaults = {}
    programs = ()               # DOS program names the helper acts in
    # where the guest block goes: upper memory first leaves the game all of
    # conventional memory
    memory = ("uma", "conv")

    def __init__(self, guest, splash, settings, args):
        self.guest = guest
        self.api = guest.api
        self.splash = splash
        self.all_settings = settings
        self.s = dict(self.defaults)
        self.s.update(settings.get(self.key, {}))
        self.args = args
        self.cond = threading.Condition()
        self.buttons = 0          # physical mouse buttons
        self.keys = set()         # physical keys down (make codes)
        self.shift = False
        self.shift_bits = 0
        self.suspended = False
        self.attached = False
        self.in_game = False
        self.next_program_check = 0
        self.stop = threading.Event()

    # -- settings -----------------------------------------------------------
    def save(self, name, value):
        self.s[name] = value
        self.all_settings.setdefault(self.key, {})[name] = value
        save_settings(self.all_settings)

    # -- output ------------------------------------------------------------
    def keys_ops(self, *names, down=True, up=True):
        ops = []
        for n in names:
            code = SCAN[n]
            if down:
                ops.append((CQ_KEY, code))
            if up:
                ops.append((CQ_KEY, code | 0x80))
        return ops

    def send(self, ops, wait=True):
        ticket = self.guest.send(ops)
        if wait and not self.guest.wait(ticket):
            log("guest did not take the queued input in time")
            self.guest.flush()

    def send_keys(self, *names):
        self.send(self.keys_ops(*names))

    def click(self, button=BTN_L):
        ops = [(CQ_BTN_DOWN, button), (CQ_WAIT_POLL, 0), (CQ_BTN_UP, button)]
        self.send(ops)

    # -- physical state ---------------------------------------------------
    def button_down(self, b):
        with self.cond:
            return bool(self.buttons & b)

    def key_down(self, code):
        with self.cond:
            return code in self.keys

    def ctrl_alt(self):
        """Ctrl or Alt held: AutoHotkey hotkeys without '*' do not fire."""
        with self.cond:
            return SCAN["ctrl"] in self.keys or SCAN["alt"] in self.keys

    def wait_button_up(self, b, timeout=None):
        """KeyWait for a mouse button; False on timeout."""
        end = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while self.buttons & b:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.cond.wait(left if left is not None else 0.5)
                if self.stop.is_set():
                    return True
            return True

    def wait_key_up(self, code, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while code in self.keys:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.cond.wait(left if left is not None else 0.5)
                if self.stop.is_set():
                    return True
            return True

    def spawn(self, fn, *a):
        threading.Thread(target=self._run_hotkey, args=(fn,) + a,
                         daemon=True).start()

    def _run_hotkey(self, fn, *a):
        try:
            fn(*a)
        except GuestLost:
            pass                        # serve() reports it
        except ApiError as e:
            log(f"{fn.__name__}: {e}")

    # -- event dispatch -------------------------------------------------------
    def handle(self, ev):
        typ, a, b, c, x, y = ev
        if typ == EV_MOUSE:
            mask, buttons, wheel = a, b & 7, c
            with self.cond:
                self.buttons = buttons
                self.cond.notify_all()
            if self.args.verbose:
                log(f"mouse mask={mask:02X} buttons={buttons} wheel={wheel} "
                    f"x={x} y={y}")
            for bit, press, release in ((BTN_L, 0x02, 0x04),
                                        (BTN_R, 0x08, 0x10),
                                        (BTN_M, 0x20, 0x40)):
                if mask & press:
                    self.on_button(bit, True)
                if mask & release:
                    self.on_button(bit, False)
            if mask & 0x80 and wheel:
                delta = wheel - 256 if wheel > 127 else wheel
                self.on_wheel(delta)
        elif typ == EV_KEY:
            byte, flags, shift = a, b, c
            self.shift = bool(shift)
            self.shift_bits = shift
            if flags & KF_E1PART:
                return                  # rest of a Pause frame
            if flags & KF_EXTENDED or byte in (0xE0, 0xE1):
                if self.args.verbose:
                    log(f"key {byte:02X} flags={flags}")
                if not flags & KF_INJECTED:
                    self.on_extended(byte, flags)
                return
            code, release = byte & 0x7F, bool(byte & 0x80)
            if self.args.verbose:
                log(f"key {NAME.get(code, hex(code))}{' up' if release else ''}"
                    f" flags={flags} shift={shift}")
            if flags & KF_INJECTED:
                return
            with self.cond:
                repeat = code in self.keys and not release
                if release:
                    self.keys.discard(code)
                else:
                    self.keys.add(code)
                self.cond.notify_all()
            self.on_key(code, release, repeat, bool(flags & KF_DROPPED))
        elif typ == EV_INFO:
            if self.args.verbose:
                log(f"info {a} {b} {c} {x:04X} {y:04X}")
            self.on_info(a, b, c, x, y)

    # -- to override --------------------------------------------------------
    def on_button(self, bit, down):
        pass

    def on_wheel(self, delta):
        pass

    def on_key(self, code, release, repeat, dropped):
        pass

    def on_extended(self, byte, flags):
        pass

    def on_info(self, code, b, c, x, y):
        pass

    def tick(self):
        """Called every poll; attach/detach and state upkeep."""

    def check_program(self):
        """Is one of our programs running?  Applies the configuration when
        that changes (the original helpers only acted while the DOSBox
        window title named the game)."""
        now = time.monotonic()
        if now < self.next_program_check:
            return self.in_game
        self.next_program_check = now + 0.5
        name = self.guest.program()
        running = not self.programs or name in self.programs
        if running != self.in_game:
            self.in_game = running
            with self.cond:             # we may have missed releases
                self.keys.clear()
                self.buttons = 0
                self.cond.notify_all()
            log(f"program {name or '?'}: helper {'on' if running else 'idle'}")
            self.apply_config()
        return running

    def active(self):
        return self.in_game and not self.suspended

    def apply_config(self):
        """Write capture/drop settings for the current state."""

    # -- helpers for the configuration ------------------------------------
    def set_drop(self, plain=(), shifted=()):
        tp = bytearray(16)
        ts = bytearray(16)
        for code in plain:
            tp[code >> 3] |= 1 << (code & 7)
        for code in shifted:
            ts[code >> 3] |= 1 << (code & 7)
        self.guest.put("kbd_drop_plain", tp)
        self.guest.put("kbd_drop_shift", ts)

    def set_follow(self, pairs=()):
        """Guest-side follow-ups: tap key B right after the game got make A
        (for AutoHotkey's "~A::Send {B}", whose B lands right behind A)."""
        table = bytearray(16)
        for i, (a, b) in enumerate(pairs[:8]):
            table[i * 2] = SCAN[a]
            table[i * 2 + 1] = SCAN[b]
        self.guest.put("kbd_follow", table)

    def toggle_suspend(self):
        self.suspended = not self.suspended
        self.splash.hide()
        log("helper " + ("paused" if self.suspended else "resumed"))
        self.apply_config()


# ---------------------------------------------------------------------------
# DOS/4GW games: find the code and patch the keyboard handler
# ---------------------------------------------------------------------------
class IDTHook:
    """DOS/4GW games: point the extender's protected-mode IDT entry for
    IRQ 1 at pm_kbd_entry, which filters the key and then calls the original
    gate as an interrupt.  Needs no knowledge of the game's code.

    The real-mode RTC tick records SGDT (under DOS/4GW in DOSBox the CPU
    switches to real mode for reflected interrupts and GDTR keeps the
    extender's GDT); the IDT is the 256-gate table one of its data
    descriptors covers."""

    VECTOR = 9

    def __init__(self, guest):
        self.guest = guest
        self.entry = None           # linear address of the IDT entry
        self.ours = None            # gate bytes we wrote

    @staticmethod
    def descriptors(gdt):
        for i in range(len(gdt) // 8):
            e = gdt[i * 8:i * 8 + 8]
            limit = e[0] | e[1] << 8 | (e[6] & 15) << 16
            base = e[2] | e[3] << 8 | e[4] << 16 | e[7] << 24
            if e[6] & 0x80:
                limit = (limit << 12) | 0xFFF
            yield i * 8, base, limit, e[5]

    def install(self):
        g = self.guest
        gl, gb = struct.unpack("<HI", g.api.read(g.linear("sys_gdtr"), 6))
        if not gb or gl < 0x17:
            return False
        try:
            gdt = g.api.read(gb, gl + 1)
        except ApiError:
            return False
        descs = list(self.descriptors(gdt))
        code = [sel for sel, base, limit, acc in descs
                if acc & 0x98 == 0x98 and acc & 0x60 == 0 and base == 0
                and limit == 0xFFFFFFFF]
        data = [sel for sel, base, limit, acc in descs
                if acc & 0x9A == 0x92 and acc & 0x60 == 0 and base == 0
                and limit == 0xFFFFFFFF]
        codes = {sel for sel, base, limit, acc in descs if acc & 0x98 == 0x98}
        if not code or not data:
            return False
        idt = None
        for sel, base, limit, acc in descs:
            if acc & 0x9A != 0x92 or limit != 0x7FF:
                continue
            try:
                table = g.api.read(base, 0x800)
            except ApiError:
                continue
            gates = sum(1 for v in range(0x30)
                        if table[v * 8 + 5] in (0x8E, 0x8F, 0xEE, 0xEF)
                        and (table[v * 8 + 2] | table[v * 8 + 3] << 8) in codes)
            if gates >= 0x28:
                idt = base
                break
        if idt is None:
            return False
        entry = idt + self.VECTOR * 8
        gate = g.api.read(entry, 8)
        off = gate[0] | gate[1] << 8 | gate[6] << 16 | gate[7] << 24
        sel = gate[2] | gate[3] << 8
        handler = g.linear("pm_kbd_entry")
        ours = struct.pack("<HHBBH", handler & 0xFFFF, code[0], 0, gate[5],
                           handler >> 16)
        if gate == ours:
            self.entry, self.ours = entry, ours
            return True
        g.put("pm_ds_value", struct.pack("<H", data[0]))
        g.put("pm_ds_store", struct.pack("<I", g.linear("pm_ds_value")))
        g.put("pm_chain", struct.pack("<IH", off, sel))
        if not g.api.write(entry, ours, expect=gate):
            return False
        self.entry, self.ours = entry, ours
        g.put8("kbd_active", 1)
        log(f"IRQ 1 gate at {entry:#x} -> helper (was {sel:04X}:{off:08X}), "
            f"flat selectors {code[0]:04X}/{data[0]:04X}")
        return True

    def present(self):
        try:
            return self.guest.api.read(self.entry, 8) == self.ours
        except ApiError:
            return False


class DOS4GWBrain(Brain):
    """Games under DOS/4GW: the keyboard filter goes into the extender's IDT
    once the game runs, and is gone when it exits."""

    def __init__(self, *a):
        super().__init__(*a)
        self.hook = None
        self.next_try = 0
        self.next_check = 0

    def tick(self):
        if not self.check_program():
            if self.hook is not None:
                self.detach()
            return
        now = time.monotonic()
        if self.hook is None:
            if now >= self.next_try:
                self.next_try = now + 0.5
                hook = IDTHook(self.guest)
                if hook.install():
                    self.hook = hook
                    self.attached = True
                    self.apply_config()
        elif now >= self.next_check:
            self.next_check = now + 1.0
            if not self.hook.present():
                self.detach()

    def detach(self):
        log("keyboard hook gone")
        self.hook = None
        self.attached = False
        self.guest.put8("kbd_active", 0)
        self.apply_config()


# ---------------------------------------------------------------------------
# WarCraft: Orcs & Humans
# ---------------------------------------------------------------------------
class WarCraft(DOS4GWBrain):
    key = "warcraft"
    title = "Warcraft Mouse Helper"
    version = "1.2"
    programs = ("WAR", "WAR_HACK")
    defaults = {"ScrollWASD": 1, "AutoSelect": 1}
    about = (
        "* Left click and drag to draw selection boxes around multiple units (without holding CTRL).\n"
        "* Right click to order units to attack or harvest/transport goods (or move, in most cases).\n"
        "* Middle click to order units to repair or explicitly move (without attacking or harvesting).\n"
        "* Middle click and hold to auto-select units and structures as you mouse over them.\n"
        "* Move the mouse wheel up/down/left/right to scroll around the map.\n"
        "* Hold Shift while scrolling down/up or use the 4th/5th mouse buttons to scroll left/right.\n"
        "* Press WASD to scroll around the map.\n"
        "* Press Esc to open and close the Options menu.\n"
        "* Press F6-F8 and F10-F12 a second time to toggle their functions off.\n"
        "* Press Shift + F10 to show and hide this list.\n"
        "* Press Shift + F11 to toggle scrolling with WASD.\n"
        "* Press Shift + F12 to toggle auto-selecting units and structures while holding middle click.\n"
        "* Press and hold Backspace to delete characters until released.\n"
        "* Press Pause/Break to pause and unpause the helper.")

    WASD = {SCAN["w"]: "up", SCAN["a"]: "left", SCAN["s"]: "down", SCAN["d"]: "right"}

    def __init__(self, *a):
        super().__init__(*a)
        self.showing_help = False
        self.stats_off = None
        self.typing_message = False
        self.wheel_lock = threading.Lock()
        self.menu_open = False      # ScrollWASD(): dark red at the bottom
        self.bottom = None          # indices last seen there
        self.next_scan = 0

    # The original searched the bottom centre of the window (49.5-50.5% of the
    # width, 98-99.3% of the height) for 0x550000 +-16: WarCraft dims the
    # screen to red while one of its menus (save game names...) is open.
    BOTTOM = [(x, y) for y in range(194, 200) for x in range(156, 164)]

    def tick(self):
        super().tick()
        if not self.in_game:
            return
        now = time.monotonic()
        if now < self.next_scan:
            return
        self.next_scan = now + 0.15
        v = self.guest.vram()
        indices = {v[y * 320 + x] for x, y in self.BOTTOM}
        if indices == self.bottom:
            return
        colours = self.guest.dac(sorted(indices))
        if colours is None:
            return
        self.bottom = indices
        red = any(abs(r - 0x55) <= 16 and g <= 16 and b <= 16
                  for r, g, b in colours.values())
        if red != self.menu_open:
            self.menu_open = red
            self.apply_config()

    # -- configuration ------------------------------------------------------
    def wasd_active(self, skip=False):
        """ScrollWASD() of the original."""
        if not skip and not self.s["ScrollWASD"]:
            return False
        return not self.menu_open

    def apply_config(self):
        on = self.active()
        self.guest.put8("cfg_capture", BTN_L | BTN_R | BTN_M)
        self.guest.put8("cfg_drop_pause", 1 if self.in_game else 0)
        plain = list(self.WASD) if self.wasd_active() else []
        shifted = plain + [SCAN["f10"], SCAN["f11"], SCAN["f12"]]
        self.set_drop(plain, shifted)
        # ~F10/~F11/~F12::Send {esc} and ~Esc::...Send {f10} (not while the
        # help list is up: then Esc only hides it)
        follow = [("f10", "esc"), ("f11", "esc"), ("f12", "esc")]
        if not self.showing_help:
            follow.append(("esc", "f10"))
        if self.stats_off == 0:             # this press of F6-F8 sends F5
            follow += [("f6", "f5"), ("f7", "f5"), ("f8", "f5")]
        self.set_follow(follow)
        self.guest.put8("cfg_enabled", 1 if on else 0)

    # -- hotkeys -----------------------------------------------------------
    def on_button(self, bit, down):
        if not self.active() or not down:
            return
        if bit == BTN_L:
            self.spawn(self.lbutton)
        elif bit == BTN_M:
            self.spawn(self.mbutton)
        elif bit == BTN_R:
            self.spawn(self.rbutton)

    def lbutton(self):
        self.send(self.keys_ops("ctrl", up=False) + [(CQ_BTN_DOWN, BTN_L)])
        self.wait_button_up(BTN_L)
        self.send([(CQ_BTN_UP, BTN_L)] + self.keys_ops("ctrl", down=False))

    def mbutton(self):
        held = not self.wait_button_up(BTN_M, 0.5)
        if held and self.s["AutoSelect"]:
            while True:
                self.click()
                time.sleep(0.3)
                if not self.button_down(BTN_M) or self.stop.is_set():
                    break
        else:
            self.send_keys("esc", "r", "m")
            time.sleep(0.09)
            self.click()

    def rbutton(self):
        self.send_keys("esc", "h", "a", "w")
        time.sleep(0.09)
        self.click()
        time.sleep(0.09)
        self.send_keys("t")

    def on_wheel(self, delta):
        if not self.active() or self.ctrl_alt():
            return
        if self.shift:
            direction = "right" if delta < 0 else "left"
        else:
            direction = "up" if delta < 0 else "down"
        for _ in range(abs(delta)):
            self.spawn(self.mouse_key, direction)

    def mouse_key(self, name):
        with self.wheel_lock:
            self.send(self.keys_ops(name, up=False))
            time.sleep(0.025)
            self.send(self.keys_ops(name, down=False))
            time.sleep(0.025)

    def on_key(self, code, release, repeat, dropped):
        if not self.active():
            if dropped:                     # swallowed before we went idle
                self.send(self.keys_ops(NAME[code], down=not release,
                                        up=release), wait=False)
            return
        if code in self.WASD and dropped:
            name = self.WASD[code] if self.wasd_active() else NAME[code]
            self.send(self.keys_ops(name, down=not release, up=release),
                      wait=False)
            return
        if release:
            return
        shift = self.shift
        if dropped and shift and code in (SCAN["f10"], SCAN["f11"], SCAN["f12"]):
            {SCAN["f10"]: self.show_hide_help,
             SCAN["f11"]: lambda: self.toggle_scroll_wasd("Shift+F11"),
             SCAN["f12"]: self.toggle_auto_select}[code]()
            return
        if shift or self.ctrl_alt():
            return
        if code in (SCAN["f6"], SCAN["f7"], SCAN["f8"]):
            # If % StatsOff := StatsOff != 0 ? 0 : 1 -> F5 on every second
            # press (the guest sent it; arm the next one)
            self.stats_off = 0 if self.stats_off != 0 else 1
            self.apply_config()
        elif code == SCAN["esc"]:
            # the guest already sent F10 after it, unless the help was up
            self.splash.hide()
            self.show_hide_help(from_esc=True)
        elif code == SCAN["enter"]:
            self.toggle_scroll_wasd("Enter")
        elif code == SCAN["backspace"] and not repeat:
            self.spawn(self.backspace)      # one repeat loop, not one per repeat

    def on_extended(self, byte, flags):
        if byte == 0xE1 and flags & KF_DROPPED:
            self.toggle_suspend()

    def backspace(self):
        code = SCAN["backspace"]
        if self.wait_key_up(code, 0.4):
            return
        while self.key_down(code) and not self.stop.is_set():
            self.send_keys("backspace")
            time.sleep(0.03)

    def show_hide_help(self, from_esc=False):
        if self.showing_help:
            self.splash.hide()
            self.showing_help = False
            self.apply_config()
            return True
        if not from_esc:
            self.splash.show(self.about, f"{self.title} v{self.version}",
                             width=720)
            self.showing_help = True
            self.apply_config()
        return False

    def toggle_scroll_wasd(self, hotkey):
        self.splash.hide()
        if hotkey == "Enter":
            if self.wasd_active(skip=True):
                if not self.s["ScrollWASD"] and not self.typing_message:
                    return
                self.typing_message = not self.typing_message
            else:
                return
        self.save("ScrollWASD", 0 if self.s["ScrollWASD"] else 1)
        self.apply_config()
        if hotkey == "Enter":
            return
        state = "On" if self.s["ScrollWASD"] else "Off"
        self.splash.show(f"WASD Scrolling {state}", width=200, timeout=2)

    def toggle_auto_select(self):
        self.splash.hide()
        self.save("AutoSelect", 0 if self.s["AutoSelect"] else 1)
        state = "On" if self.s["AutoSelect"] else "Off"
        self.splash.show(f"Auto Select {state}", width=200, timeout=2)


# ---------------------------------------------------------------------------
# Dune II: The Building of a Dynasty (and Super Dune II Classic)
# ---------------------------------------------------------------------------
class Dune2(Brain):
    key = "dune2"
    title = "Dune II Mouse Helper"
    version = "1.2"
    programs = ("DUNE2", "SUPER")
    defaults = {"ScrollWASD": 1, "AutoSelect": 1}
    about = (
        "* Right click to order units to move, attack or harvest.\n"
        "* Right click to build or place structures, as well as pause or resume construction.\n"
        "* Right click and hold to repair or upgrade structures.\n"
        "* Middle click to explicitly order units to attack (for times when right click isn't ideal).\n"
        "* Middle click to open or close a structure's production screen.\n"
        "* Left click and hold to auto-select units and structures as you mouse over them.\n"
        "* Use the mouse wheel to scroll the map and production screens.\n"
        "* Hold Shift while scrolling down/up or click the 4th/5th mouse buttons to scroll left/right.\n"
        "* Press WASD to scroll around the map.\n"
        "* Press the Esc key to open and close the Options menu.\n"
        "* Press F1-F3 a second time to toggle their screens off.\n"
        "* Press Shift + F10 to show and hide this list.\n"
        "* Press Shift + F11 to toggle scrolling with WASD.\n"
        "* Press Shift + F12 to toggle auto-selecting units and structures while holding left click.\n"
        "* Press Pause/Break to pause and unpause the helper.")
    WASD = {SCAN["w"]: "up", SCAN["a"]: "left", SCAN["s"]: "down", SCAN["d"]: "right"}
    # The original sampled screen pixels.  Here: palette indices of IBM.PAL
    # within its tolerance (16 per channel) of the colours it looked for.
    DIALOG = {128, 233}         # 0xBCBDD3 / 0xB2B2C7 at (W/4, H/2): a text dialog
    LIST = {115, 224}           # 0xD89103 at 10-20% of W, H/2: production list

    def __init__(self, *a):
        super().__init__(*a)
        self.screen_off = None
        self.showing_help = False
        self.dialog = False
        self.list_open = False
        self.next_scan = 0
        self.extra_capture = 0
        self.game_ver = None
        self.variant = None
        self.wheel_lock = threading.Lock()

    # -- state ---------------------------------------------------------------
    def tick(self):
        if not self.check_program():
            return
        if self.game_ver is None:
            self.detect_version()
        now = time.monotonic()
        if now >= self.next_scan:
            self.next_scan = now + 0.1
            self.scans = getattr(self, "scans", 0) + 1
            if self.scans % 3 == 0:
                self.guest.maintain_int9(self.programs)
            v = self.guest.vram()
            dialog = any(v[y * 320 + x] in self.DIALOG
                         for y in range(97, 107) for x in range(78, 83))
            self.list_open = any(v[y * 320 + x] in self.LIST
                                 for y in range(99, 106) for x in range(32, 65))
            if dialog != self.dialog:
                self.dialog = dialog
                self.apply_config()

    def detect_version(self):
        """GetGameVersion(): v1.07 sends 'h' (Harvest) on right click, except
        in Super Dune II Classic.  The original read the exe named in its
        settings, which eXo never set, so there it always acted as v1.00."""
        program = self.guest.program()
        self.variant = "Super Dune II Classic" if program == "SUPER" else "Dune II"
        mem = self.api.read(0, 0xA0000)
        self.game_ver = "1.07" if b"V1.07" in mem else "1.00"
        log(f"{self.variant} {self.game_ver}")

    def wasd_active(self, skip=False):
        if not skip and not self.s["ScrollWASD"]:
            return False
        return not self.dialog

    def apply_config(self):
        on = self.active()
        self.guest.put8("cfg_rmkbd", 1)     # real-mode game: INT 9 filter
        self.guest.put8("cfg_capture", BTN_R | BTN_M | self.extra_capture)
        self.guest.put8("cfg_drop_pause", 1 if self.in_game else 0)
        plain = list(self.WASD) if self.wasd_active() else []
        shifted = plain + [SCAN["f10"], SCAN["f11"], SCAN["f12"]]
        self.set_drop(plain, shifted)
        follow = []
        if not self.showing_help:           # ~Esc:: ... Send {f2}
            follow.append(("esc", "f2"))
        if self.screen_off == 0:            # this press of F1-F3 sends Esc
            follow += [("f1", "esc"), ("f2", "esc"), ("f3", "esc")]
        self.set_follow(follow)
        self.guest.put8("cfg_enabled", 1 if on else 0)

    # -- hotkeys -----------------------------------------------------------
    def on_button(self, bit, down):
        if not self.active() or not down:
            return
        if bit == BTN_L:
            self.spawn(self.lbutton)
        elif bit == BTN_M:
            self.spawn(self.mbutton)
        elif bit == BTN_R:
            self.spawn(self.rbutton)

    def lbutton(self):
        # $~*LButton: the press reaches the game; held for 0.5 s -> clicks
        if self.wait_button_up(BTN_L, 0.5) or not self.s["AutoSelect"]:
            return
        self.extra_capture = BTN_L      # clicks replace the held button
        self.apply_config()
        try:
            self.send([(CQ_BTN_UP, BTN_L)])
            while True:
                self.click()
                time.sleep(0.09)
                if not self.button_down(BTN_L) or self.stop.is_set():
                    break
        finally:
            self.extra_capture = 0
            self.apply_config()

    def mbutton(self):
        self.send_keys("c", "a", "h", "r", "f3")
        time.sleep(0.2)
        self.click()

    def rbutton(self):
        keys = ["c"]
        if self.game_ver == "1.07" and self.variant != "Super Dune II Classic":
            keys.append("h")
        keys.append("m")
        self.send_keys(*keys)
        time.sleep(0.2)
        self.click()
        time.sleep(0.03)
        if not self.wait_button_up(BTN_R, 0.5):
            self.send_keys("u", "r")
        else:
            self.send_keys("o", "b", "p")

    def on_wheel(self, delta):
        if not self.active() or self.ctrl_alt():
            return
        if self.shift:
            direction = "right" if delta < 0 else "left"
        else:
            direction = "up" if delta < 0 else "down"
        for _ in range(abs(delta)):
            self.spawn(self.mouse_key, direction)

    def mouse_key(self, name):
        with self.wheel_lock:
            self.send_keys(name)
            if not self.list_open:          # map: two steps per notch
                time.sleep(0.06)
                self.send_keys(name)

    def on_key(self, code, release, repeat, dropped):
        if not self.active():
            if dropped:                     # swallowed before we went idle
                self.send(self.keys_ops(NAME[code], down=not release,
                                        up=release), wait=False)
            return
        if code in self.WASD and dropped:
            name = self.WASD[code] if self.wasd_active() else NAME[code]
            self.send(self.keys_ops(name, down=not release, up=release),
                      wait=False)
            return
        if release:
            return
        if dropped and self.shift and code in (SCAN["f10"], SCAN["f11"], SCAN["f12"]):
            {SCAN["f10"]: self.show_hide_help,
             SCAN["f11"]: lambda: self.toggle_scroll_wasd("Shift+F11"),
             SCAN["f12"]: self.toggle_auto_select}[code]()
            return
        if self.shift or self.ctrl_alt():
            return
        if code in (SCAN["f1"], SCAN["f2"], SCAN["f3"]):
            self.screen_off = 0 if self.screen_off != 0 else 1
            self.apply_config()             # the guest sent Esc if armed
        elif code == SCAN["esc"]:
            self.screen_off = 1
            self.splash.hide()
            self.show_hide_help(from_esc=True)
            self.apply_config()

    def on_extended(self, byte, flags):
        if byte == 0xE1 and flags & KF_DROPPED:
            self.toggle_suspend()

    def show_hide_help(self, from_esc=False):
        if self.showing_help:
            self.splash.hide()
            self.showing_help = False
            self.apply_config()
            return True
        if not from_esc:
            self.splash.show(self.about, f"{self.title} v{self.version}",
                             width=720)
            self.showing_help = True
            self.apply_config()
        return False

    def toggle_scroll_wasd(self, hotkey):
        self.splash.hide()
        self.save("ScrollWASD", 0 if self.s["ScrollWASD"] else 1)
        self.apply_config()
        state = "On" if self.s["ScrollWASD"] else "Off"
        self.splash.show(f"WASD Scrolling {state}", width=200, timeout=2)

    def toggle_auto_select(self):
        self.splash.hide()
        self.save("AutoSelect", 0 if self.s["AutoSelect"] else 1)
        state = "On" if self.s["AutoSelect"] else "Off"
        self.splash.show(f"Auto Select {state}", width=200, timeout=2)


# ---------------------------------------------------------------------------
# SkyNET and The Terminator: Future Shock
# ---------------------------------------------------------------------------
class SkyNET(DOS4GWBrain):
    key = "skynet"
    title = "SkyNET Mouse Helper"
    version = "1.2"
    programs = ("GAME", "SKYNET", "SHOCK")
    defaults = {"InvertCycle": 0, "WeaponMode": 1, "WeaponAutoSkip": 1,
                "CurrentWeapon": 1}
    about = (
        "* Scroll the mouse wheel up/down to cycle weapon types or all 13 available weapons.\n"
        "* Press the middle mouse button or scroll left/right to cycle weapons of the same type.\n"
        "* Press ~ for next weapon and Shift + ~ for previous weapon (for mice without wheels).\n"
        "* Press Shift + F10 to show and hide this list.\n"
        "* Press Shift + F11 to switch between weapon cycle modes (weapon types or all weapons).\n"
        "* Press Shift + F12 to toggle automatic weapon skipping.\n"
        "* Press Pause/Break to pause and unpause the helper.")
    DIGITS = {SCAN[str(n)]: n for n in range(1, 8)}
    # all-weapons mode: weapon index (1-13) -> digit key that cycles to it
    SAME = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 3, 7: 4, 8: 5, 9: 6, 10: 6,
            11: 7, 12: 7, 13: 7}
    FIRST = {1: 1, 2: 3, 3: 6, 4: 7, 5: 8, 6: 9, 7: 11}
    # weapon display: the original sampled three pixels at 17.28%, 27.64% and
    # 39.14% of the picture's width, 93.81% of the window's height
    HUD = [(x, y) for cx in (55, 88, 125) for x in (cx - 1, cx, cx + 1)
           for y in range(186, 191)]
    # the same at 640x480: runs of 5 pixels from x = cx - 2, line y
    HUD640 = [(cx, y) for cx in (111, 177, 251) for y in range(448, 453)]
    # at 640x480 the game also draws its status bar (the bottom 96 lines) as
    # if the screen were linear from A0000h, past the 64 KB window into
    # DC000h-EAFFFh, and a guest block in upper memory with it
    memory = ("conv",)

    def __init__(self, *a):
        super().__init__(*a)
        self.showing_help = False
        self.cycle_lock = threading.Lock()
        self.current = int(self.s["CurrentWeapon"])
        self.was_in_game = False
        self.vesa_geometry = (640, 0)       # scan line length, display start
        self.hud_why = ""                   # why hud() gave None
        # wheel/~ steps (+ up, - down) and middle clicks still to do: one
        # worker does them in turn (the original's hotkey threads ran
        # through each other on CurrentWeapon)
        self.queue_cond = threading.Condition()
        self.queued_steps = 0
        self.queued_same = 0
        threading.Thread(target=self.weapon_worker, daemon=True).start()

    def shock(self):
        return self.guest.program() == "SHOCK"

    def apply_config(self):
        on = self.active()
        self.guest.put8("cfg_polling", 1)       # the game polls INT 33h
        self.guest.put8("cfg_capture", BTN_M)
        self.guest.put8("cfg_drop_pause", 1 if self.in_game else 0)
        tilde = SCAN["`"]
        self.set_drop([tilde], [tilde, SCAN["f9"], SCAN["f10"], SCAN["f11"],
                                SCAN["f12"]])
        self.guest.put8("cfg_enabled", 1 if on else 0)
        if self.in_game != self.was_in_game:
            self.was_in_game = self.in_game
            if not self.in_game:            # SetCurrentWeapon() on exit
                self.save("CurrentWeapon", self.current)

    # -- sending ----------------------------------------------------------------
    def press(self, *names):
        """Send with SetKeyDelay 0, 40: each key held for 40 ms."""
        for n in names:
            self.send(self.keys_ops(n, up=False))
            time.sleep(0.04)
            self.send(self.keys_ops(n, down=False))

    def hud(self):
        """The weapon display's pixels; None (reason in hud_why) when they
        cannot be read."""
        mode = self.api.read(0x449, 1)[0]
        if mode == 0x13:
            rows = self.api.read(0xA0000 + 186 * 320, 5 * 320)
            pixels = bytes(rows[(y - 186) * 320 + x] for x, y in self.HUD)
        elif mode == 0x69:                  # S3's number for VESA mode 101h
            pixels = self.hud640()
        else:
            self.hud_why = f"video mode {mode:02X}h"
            return None
        if pixels is not None and not any(pixels):
            self.hud_why = "blank"
            return None
        return pixels

    def hud_read(self, tries=5):
        """hud(), read again when it fails: the original's PixelGetColor
        always gave a colour."""
        for _ in range(tries):
            pixels = self.hud()
            if pixels is not None:
                return pixels
            time.sleep(0.02)
        return None

    def hud_change(self, old, timeout):
        """The weapon display once it differs from `old`; None when it
        stays the same for `timeout`.  Reads that fail are skipped (the
        game may be redrawing it)."""
        end = time.monotonic() + timeout
        while True:
            time.sleep(0.02)
            new = self.hud()
            if new is not None and new != old:
                return new
            if time.monotonic() >= end:
                return None

    def hud640(self):
        """640x480: the status bar lies beyond the 64 KB window the API
        sees (the game's stray copy of it does not follow the weapon), so
        the guest copies the spots through the VESA BIOS.  The offsets
        follow the game's scan line length and display start."""
        for _ in range(2):
            pitch, start = self.vesa_geometry
            try:
                got = self.guest.vesa_read(
                    [(start + y * pitch + cx - 2, 5) for cx, y in self.HUD640])
            except ValueError as e:
                self.hud_why = str(e)
                return None
            if got is None:
                self.hud_why = self.guest.vesa_why
                return None
            data, pitch_now, start_now = got
            if (pitch_now, start_now) == (pitch, start):
                return data
            self.vesa_geometry = (pitch_now, start_now)
        self.hud_why = "display start moving"
        return None

    # -- weapons ----------------------------------------------------------------
    def cycle_weapon(self, direction):
        mode = self.s["WeaponMode"]
        top = 7 if mode == 1 else 13
        if direction == "down":
            self.current -= 1
            if self.current < 1:
                self.current = top
            if self.shock() and mode == 2 and self.current == 2:
                self.current = 1
        else:
            self.current += 1
            if self.current > top:
                self.current = 1
            if self.shock() and mode == 2 and self.current == 2:
                self.current = 3
        self.select_weapon("cycle", direction)

    def key_for_current(self):
        if self.s["WeaponMode"] == 1:
            return str(self.current)
        return str(self.SAME.get(self.current, 1))

    def select_weapon(self, target, direction=None):
        with self.cycle_lock:
            if target != "cycle":
                return
            if not self.s["WeaponAutoSkip"]:
                self.press(self.key_for_current())
                return
            top = 7 if self.s["WeaponMode"] == 1 else 13
            start = pressed = None
            for _ in range(top):            # the original could loop forever
                if self.stop.is_set() or not self.active():
                    return
                old = self.hud_read()
                key = self.key_for_current()
                if old is None:             # no weapon display to go by
                    self.press(key)
                    if self.args.verbose:
                        log(f"weapon key {key}: no display ({self.hud_why})")
                    return
                if start is None:
                    start = old
                elif old != start:          # the last key switched, late
                    self.current = pressed
                    if self.args.verbose:
                        log(f"weapon key {key}: not pressed, the last key "
                            f"switched late")
                    return
                t = time.monotonic()
                new = self.hold(key, old)
                if self.args.verbose:
                    log(f"weapon key {key}: display {old.hex()} -> "
                        f"{new.hex() if new else 'unchanged'} after "
                        f"{time.monotonic() - t:.2f} s")
                if new is not None:
                    return
                pressed = self.current
                self.current += -1 if direction == "down" else 1
                if self.current > top:
                    self.current = 1
                elif self.current < 1:      # the original did not wrap here
                    self.current = top

    def hold(self, key, old):
        """Press a weapon key and keep it down until the weapon display
        differs from `old`, 0.45 s at most (the original tapped it for 40 ms
        and looked once, 50 ms later: after turning or shooting the game
        often missed that)."""
        self.send(self.keys_ops(key, up=False))
        try:
            return self.hud_change(old, 0.45)
        finally:
            self.send(self.keys_ops(key, down=False))

    def queue_weapons(self, steps=0, same=0):
        """Queue `steps` CycleWeapon() steps (+ up, - down; opposite ones
        cancel out) and `same` SelectWeapon("cycle") presses, up to one
        round of weapons each."""
        top = 7 if self.s["WeaponMode"] == 1 else 13
        with self.queue_cond:
            self.queued_steps = max(-top, min(top, self.queued_steps + steps))
            self.queued_same = min(top, self.queued_same + same)
            self.queue_cond.notify()

    def weapon_worker(self):
        while not self.stop.is_set():
            with self.queue_cond:
                if not (self.queued_steps or self.queued_same):
                    self.queue_cond.wait(0.5)
                    continue
                if self.queued_steps:
                    direction = "up" if self.queued_steps > 0 else "down"
                    self.queued_steps += -1 if direction == "up" else 1
                else:
                    direction = None
                    self.queued_same -= 1
            if not self.active():
                continue
            try:
                if direction:
                    self.cycle_weapon(direction)
                else:
                    self.select_weapon("cycle")
            except GuestLost:
                return
            except Exception as e:          # keep the wheel working
                log(f"weapon cycling: {type(e).__name__}: {e}")

    def digit(self, n):
        """*1..*7: remember which weapon the player picked (the key itself
        reaches the game)."""
        if self.s["WeaponMode"] == 1:
            self.current = n
        else:
            self.current = self.FIRST[n]

    # -- hotkeys -----------------------------------------------------------
    def on_button(self, bit, down):
        if self.active() and down and bit == BTN_M:
            self.queue_weapons(same=1)

    def on_wheel(self, delta):
        if not self.active():
            return
        up = delta < 0
        if self.s["InvertCycle"]:
            up = not up
        n = abs(delta)                      # *WheelUp -> CycleWeapon("down")
        self.queue_weapons(steps=-n if up else n)

    def on_key(self, code, release, repeat, dropped):
        if not self.active():
            if dropped:
                self.send(self.keys_ops(NAME[code], down=not release,
                                        up=release), wait=False)
            return
        if release:
            if code == SCAN["`"] and getattr(self, "held_tilde", False):
                self.held_tilde = False
                self.send(self.keys_ops("`", down=False), wait=False)
            return
        if code in self.DIGITS:
            self.digit(self.DIGITS[code])
            return
        if code == SCAN["`"] and dropped:
            if self.ctrl_alt():             # not a hotkey with Ctrl/Alt
                self.send(self.keys_ops("`", up=False), wait=False)
                self.held_tilde = True
            else:
                self.queue_weapons(steps=-1 if self.shift else 1)
            return
        if dropped and self.shift:
            if code == SCAN["f9"]:
                self.spawn(self.shift_f9)
            elif code == SCAN["f10"]:
                self.show_hide_help()
            elif code == SCAN["f11"]:
                self.toggle_weapon_mode()
            elif code == SCAN["f12"]:
                self.toggle_auto_skip()
            return
        if code == SCAN["esc"] and not self.shift and not self.ctrl_alt():
            self.splash.hide()
            self.show_hide_help(from_esc=True)

    def on_extended(self, byte, flags):
        if byte == 0xE1 and flags & KF_DROPPED:
            self.toggle_suspend()

    def shift_f9(self):
        """+F9: Sleep 90, Send {F9} - AutoHotkey lifts the held Shift around
        an unshifted key."""
        time.sleep(0.09)
        shifts = [n for n, bit in (("shift", 1), ("rshift", 2))
                  if self.shift_bits & bit]
        ops = [(CQ_KEY, SCAN[n] | 0x80) for n in shifts]
        self.send(ops)
        self.press("f9")
        self.send([(CQ_KEY, SCAN[n]) for n in shifts if self.key_down(SCAN[n])])

    def show_hide_help(self, from_esc=False):
        if self.showing_help:
            self.splash.hide()
            self.showing_help = False
            return True
        if not from_esc:
            self.splash.show(self.about, f"{self.title} v{self.version}",
                             width=720)
            self.showing_help = True
        return False

    def toggle_weapon_mode(self):
        self.splash.hide()
        self.save("WeaponMode", 1 if self.s["WeaponMode"] == 2 else 2)
        text = ("1\n(cycle weapon types)" if self.s["WeaponMode"] == 1
                else "2\n(cycle all weapons)")
        self.splash.show(f"Weapon Mode {text}", width=170, timeout=2)

    def toggle_auto_skip(self):
        self.splash.hide()
        self.save("WeaponAutoSkip", 0 if self.s["WeaponAutoSkip"] else 1)
        state = "On" if self.s["WeaponAutoSkip"] else "Off"
        self.splash.show(f"Weapon Skip {state}", width=200, timeout=2)


class Probe(Brain):
    """Diagnostics: hooks everything, changes nothing, logs the input."""
    key = "probe"
    title = "Probe"

    def tick(self):
        self.check_program()

    def apply_config(self):
        self.guest.put8("cfg_capture", 0)
        self.guest.put8("cfg_drop_pause", 0)
        self.guest.put8("cfg_rmkbd", 1 if self.args.rmkbd else 0)
        self.set_drop()
        self.guest.put8("cfg_enabled", 1)


GAMES = {cls.key: cls for cls in (WarCraft, Dune2, SkyNET, Probe)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def has_api(exe):
    """Is `exe` DOSBox Staging 0.83 or newer (its webserver settings are in
    the program)?  Remembered per file size and date."""
    try:
        st = os.stat(exe)
    except OSError:
        return False
    key = os.path.normcase(os.path.abspath(exe))
    stamp = [st.st_size, st.st_mtime_ns]
    settings = load_settings()
    known = settings.get("_api", {}).get(key)
    if known and known[:2] == stamp:
        return known[2]
    found, tail = False, b""
    try:
        with open(exe, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                if b"webserver_enabled" in tail + chunk:
                    found = True
                    break
                tail = chunk[-32:]
    except OSError:
        return False
    settings.setdefault("_api", {})[key] = stamp + [found]
    save_settings(settings)
    return found


def staging_args(args):
    """DOSBox ECE arguments Staging does not take: -nomenu, and a -conf
    without a file (eXo's launchers pass "-conf %conf%" with %conf% unset)."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a.lower() in ("-nomenu", "--nomenu"):
            i += 1
            continue
        if a.lower() in ("-conf", "--conf") and (
                i + 1 >= len(args) or args[i + 1].startswith("-")):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def choose_dosbox(args):
    """The command line to run and whether it is a DOSBox the helper can
    work with: the given DOSBox if it is Staging 0.83+, else --staging, else
    the --fallback program (the original AutoHotkey helper, Windows) or the
    given DOSBox on its own."""
    given, rest = args.dosbox[0], list(args.dosbox[1:])
    for exe in (given, args.staging):
        if exe and has_api(exe):
            return [exe] + staging_args(rest), True
    if (args.fallback and sys.platform == "win32"
            and os.path.isfile(args.fallback)):
        return [args.fallback, given] + rest, False
    return [given] + rest, False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_api(api, proc, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            api.info()
            return True
        except (ApiError, OSError):
            time.sleep(0.1)
    return False


def run(args):
    splash = make_splash(args)      # before DOSBox: creating it may take focus
    result = []
    worker = threading.Thread(target=lambda: result.append(helper(args, splash)))
    worker.start()
    try:
        splash.mainloop(worker)
    except KeyboardInterrupt:
        pass
    worker.join()
    return result[0] if result else 1


def helper(args, splash):
    if args.attach:
        port, proc = args.attach, None
    else:
        if not args.dosbox:
            log("no DOSBox command line given")
            return 2
        cmd, api = choose_dosbox(args)
        if api:
            port = free_port()
            cmd += [
                "--set", "webserver_enabled=true",
                "--set", f"webserver_port={port}",
                "--set", "builtin_dos_mouse_driver_model=wheel",
                # in a window Staging otherwise takes the middle button for
                # capturing and releasing the mouse; the games use it
                "--set", "mouse_middle_release=false",
            ]
        log("starting " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd)
        except OSError as e:
            log(f"cannot start DOSBox: {e}")
            return 2
        if not api:
            log("no DOSBox Staging 0.83 or newer: the game runs without "
                "this helper")
            proc.wait()
            return 0
        log(f"DOSBox pid {proc.pid}, API port {port}")
    brain = None
    try:
        brain = serve(args, port, proc, splash)
    except Exception as e:                  # never take the game down with us
        log(f"helper stopped: {type(e).__name__}: {e}")
    finally:
        if brain is not None:
            brain.stop.set()
    if proc is not None:
        proc.wait()                         # the launcher waits for us
    return 0


def serve(args, port, proc, splash):
    """Install the guest part and run the game's helper logic until DOSBox
    exits.  Returns the brain."""
    api = Api(port)
    if not wait_for_api(api, proc):
        log("DOSBox API not reachable")
        return None
    guest = Guest(api)
    # /dosbox/info answers before the emulation runs; memory commands go
    # through the emulation loop and time out until it does
    end = time.monotonic() + 30
    while True:
        try:
            guest.install(GAMES[args.game].memory)
            break
        except ApiBusy:
            if time.monotonic() > end or (proc is not None and
                                          proc.poll() is not None):
                log("DOSBox does not run API commands")
                return None
            time.sleep(0.1)
    settings = load_settings()
    brain = GAMES[args.game](guest, splash, settings, args)
    while True:
        try:
            brain.apply_config()
            break
        except ApiBusy:
            time.sleep(0.05)
    last_report = 0
    next_beat = 0
    while proc is None or proc.poll() is None:
        try:
            for ev in guest.poll():
                brain.handle(ev)
            brain.tick()
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + 0.5
                guest.check_code()
                guest.put8("host_alive", 255)
                if brain.active() and not guest.byte("cfg_enabled"):
                    brain.apply_config()    # the guest had given up on us
        except GuestLost as e:
            log(f"{e}; the helper has stopped")
            splash.show(f"Mouse helper stopped:\n{e}.", width=420,
                        timeout=8)
            break
        except ApiBusy:
            time.sleep(0.05)
            continue
        except (ApiError, OSError) as e:
            if proc is not None and proc.poll() is not None:
                break
            log(f"API: {e}")
            time.sleep(0.2)
            continue
        if args.verbose and time.monotonic() - last_report > 5:
            last_report = time.monotonic()
            log("counters " + " ".join(
                f"{n[4:]}={guest.counter(n)}" for n in
                ("cnt_int8", "cnt_int70", "cnt_int33", "cnt_mouse_cb",
                 "cnt_kbd", "cnt_pm_kbd", "cnt_inject", "cnt_ev_lost")))
        time.sleep(0.004)
    return brain


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Mouse helpers for WarCraft, Dune II and SkyNET in "
                    "DOSBox Staging (port of Osprey's AutoHotkey helpers).")
    p.add_argument("--game", required=True, choices=sorted(GAMES))
    p.add_argument("--attach", type=int, metavar="PORT",
                   help="use a running DOSBox's API on this port")
    p.add_argument("--rmkbd", action="store_true",
                   help="probe: keep a real-mode INT 9 hook on top of the game's")
    p.add_argument("--staging", metavar="DOSBOX",
                   help="DOSBox Staging 0.83+ to use when the given DOSBox "
                        "is not one")
    p.add_argument("--fallback", metavar="PROGRAM",
                   help="without a DOSBox Staging 0.83+, run this with the "
                        "DOSBox command line instead (the original "
                        "AutoHotkey helper; Windows)")
    p.add_argument("--settings", metavar="FILE",
                   help=f"settings file (default: {SETTINGS_FILE.name} next "
                        "to this script)")
    p.add_argument("--no-splash", action="store_true",
                   help="print messages instead of showing them on screen")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("dosbox", nargs=argparse.REMAINDER,
                   help="DOSBox command line (after --)")
    args = p.parse_args(argv)
    if args.dosbox and args.dosbox[0] == "--":
        args.dosbox = args.dosbox[1:]
    if args.settings:
        use_settings_file(args.settings)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
