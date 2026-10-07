"""Test session helpers: start the helper + DOSBox, find the guest block,
fake mouse events through the guest's test override (a posted mouse move
triggers the driver callback, which then reports the faked event)."""
import os
import pathlib
import re
import struct
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import mousehelper as mh  # noqa: E402
import winpost  # noqa: E402

SYM = mh.SYM
STAGING = os.environ.get("MH_STAGING", "dosbox")


class Session:
    def __init__(self, log_path, port=None, pid=None):
        self.log_path = pathlib.Path(log_path)
        self.port = port
        self.pid = pid
        self.api = mh.Api(port) if port else None
        self.win = None

    @classmethod
    def start(cls, game, conf, log_path, extra=(), go_key="y"):
        log_path = pathlib.Path(log_path)
        f = open(log_path, "w")
        cmd = [sys.executable, "-u", str(HERE.parent / "mousehelper.py"),
               "--game", game, "--no-splash", "-v",
               "--settings", str(log_path.with_suffix(".settings.json")),
               *extra, "--",
               STAGING, "--noprimaryconf", "-conf", str(conf)]
        helper = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                                  cwd=HERE.parent)
        s = cls(log_path)
        s.helper = helper
        end = time.monotonic() + 30
        while time.monotonic() < end:
            m = re.search(r"DOSBox pid (\d+), API port (\d+)", s.text())
            if m and re.search(r"guest installed|reusing the helper block",
                               s.text()):
                s.pid, s.port = int(m.group(1)), int(m.group(2))
                s.api = mh.Api(s.port)
                if go_key:                  # the conf waits at a CHOICE prompt
                    time.sleep(0.5)
                    s.window().tap(go_key)
                return s
            time.sleep(0.1)
        raise RuntimeError("helper did not start DOSBox")

    def text(self):
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def helper_lines(self):
        return [ln for ln in self.text().splitlines()
                if not re.match(r"\d{4}-\d\d-\d\d ", ln)]

    def wait_log(self, pattern, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if re.search(pattern, self.text()):
                return True
            time.sleep(0.1)
        return False

    def window(self, x=-2300, y=120):
        if self.win is None:
            hwnd = winpost.find_window(self.pid)
            winpost.move_window(hwnd, x, y)
            time.sleep(0.2)
            self.win = winpost.Win(hwnd)
        return self.win

    @property
    def base(self):
        off, seg = struct.unpack("<HH", self.api.read(0x33 * 4, 4))
        return seg * 16

    def read(self, sym, n):
        return self.api.read(self.base + SYM[sym], n)

    def counter(self, sym):
        return struct.unpack("<H", self.read(sym, 2))[0]

    def fake_mouse(self, mask, buttons=0, wheel=0, x=None, y=None,
                   sticky=True):
        """Make the next driver event report this instead; with sticky,
        later events keep reporting this position and these buttons."""
        if x is None or y is None:
            x, y = struct.unpack("<hh", self.read("test_ev_x", 4))
        flag = 1 | (2 if sticky else 0)
        data = struct.pack("<BBBBhh", flag, mask, buttons, wheel & 0xFF, x, y)
        self.api.write(self.base + SYM["test_ev_flag"], data)
        cb = self.counter("cnt_mouse_cb")
        w = self.window()
        for i in range(20):                 # a real (posted) move triggers it
            w.move(10 + (i & 1), 10)
            time.sleep(0.03)
            if not self.read("test_ev_flag", 1)[0] & 1:
                return True
        raise RuntimeError(f"no driver event (callbacks {cb})")

    def move_to(self, x, y, buttons=0):
        """Sticky test position in driver coordinates (one motion event)."""
        return self.fake_mouse(0x01, buttons, 0, x, y)

    def mickeys(self, dx, dy):
        """Relative motion for games that read INT 33h fn 0Bh (sticky test
        mode must be on)."""
        cur = struct.unpack("<hh", self.read("test_mickey_x", 4))
        self.api.write(self.base + SYM["test_mickey_x"],
                       struct.pack("<hh", cur[0] + dx, cur[1] + dy))

    def fn_counts(self):
        raw = self.read("fn_count", 64)
        return {f: n for f, n in enumerate(struct.unpack("<32H", raw)) if n}

    def unstick(self):
        self.api.write(self.base + SYM["test_ev_flag"], b"\0")

    def stop(self):
        if self.pid:
            subprocess.run(["taskkill", "/PID", str(self.pid)],
                           capture_output=True)
        if getattr(self, "helper", None):
            try:
                self.helper.wait(10)
            except subprocess.TimeoutExpired:
                self.helper.kill()
