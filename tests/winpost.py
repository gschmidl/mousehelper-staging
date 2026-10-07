"""Windows test driver: posts mouse and keyboard messages to a DOSBox Staging
window.  Nothing goes through the real input devices, so the user's cursor
and focus are left alone.  Needs Staging with mouse_capture = seamless and
mouse_raw_input = false for the mouse messages to reach the guest."""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32
try:    # physical pixels, like the DPI-aware DOSBox window
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass

WM_KEYDOWN, WM_KEYUP = 0x100, 0x101
WM_MOUSEMOVE = 0x200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x207, 0x208
WM_MOUSEWHEEL = 0x20A
MK_LBUTTON, MK_RBUTTON, MK_SHIFT, MK_CONTROL, MK_MBUTTON = 1, 2, 4, 8, 0x10

BUTTONS = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
           "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
           "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON)}

# virtual key, scancode
KEYS = {
    "esc": (0x1B, 0x01), "enter": (0x0D, 0x1C), "space": (0x20, 0x39),
    "backspace": (0x08, 0x0E), "shift": (0x10, 0x2A), "ctrl": (0x11, 0x1D),
    "pause": (0x13, 0x45),
    **{chr(c).lower(): (c, sc) for c, sc in zip(
        b"QWERTYUIOPASDFGHJKLZXCVBNM",
        (0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19,
         0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26,
         0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32))},
    **{str(n): (0x30 + n, 0x02 + (n - 1) % 10) for n in range(10)},
    **{f"f{n}": (0x6F + n, 0x3A + n) for n in range(1, 11)},
    "f11": (0x7A, 0x57), "f12": (0x7B, 0x58),
    "`": (0xC0, 0x29),
}


def find_window(pid, timeout=10):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found.clear()
        user32.EnumWindows(cb, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    raise RuntimeError(f"no window for pid {pid}")


def move_window(hwnd, x, y):
    user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)


class Win:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.held = 0
        self.x = self.y = 0
        r = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        self.width, self.height = r.right, r.bottom

    def _post(self, msg, wparam, lparam):
        user32.PostMessageW(self.hwnd, msg, wparam, lparam)

    def _xy(self):
        return (self.y << 16) | (self.x & 0xFFFF)

    def move(self, x, y):
        self.x, self.y = int(x), int(y)
        self._post(WM_MOUSEMOVE, self.held, self._xy())

    def game_xy(self, gx, gy, gw=320, gh=200):
        """Client position of a pixel of a gw x gh game screen."""
        return ((gx + 0.5) * self.width / gw, (gy + 0.5) * self.height / gh)

    def move_game(self, gx, gy, gw=320, gh=200):
        self.move(*self.game_xy(gx, gy, gw, gh))

    def down(self, button="left"):
        msg, _, mk = BUTTONS[button]
        self.held |= mk
        self._post(msg, self.held, self._xy())

    def up(self, button="left"):
        _, msg, mk = BUTTONS[button]
        self.held &= ~mk
        self._post(msg, self.held, self._xy())

    def click(self, button="left", hold=0.08):
        self.down(button)
        time.sleep(hold)
        self.up(button)

    def wheel(self, notches):
        """Positive = away from the user (wheel up)."""
        pt = wt.POINT(self.x, self.y)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        delta = (120 * notches) & 0xFFFF
        lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
        self._post(WM_MOUSEWHEEL, (delta << 16) | self.held, lparam)

    def key(self, name, up=False):
        vk, sc = KEYS[name]
        lparam = 1 | (sc << 16)
        if up:
            lparam |= (1 << 30) | (1 << 31)
        self._post(WM_KEYUP if up else WM_KEYDOWN, vk, lparam)

    def tap(self, name, hold=0.06):
        self.key(name)
        time.sleep(hold)
        self.key(name, up=True)
