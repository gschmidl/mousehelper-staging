"""Show the help window and a toast; check that the foreground window (and
so the keyboard focus) does not change.  Windows only."""
import ctypes
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import mousehelper as mh  # noqa: E402

user32 = ctypes.windll.user32


def main():
    splash = mh.TkSplash()
    before = user32.GetForegroundWindow()
    changes = []

    def work():
        splash.show(mh.WarCraft.about, "Warcraft Mouse Helper v1.2", width=720)
        for _ in range(30):
            time.sleep(0.1)
            if user32.GetForegroundWindow() != before:
                changes.append(user32.GetForegroundWindow())
        splash.hide()
        time.sleep(0.3)
        splash.show("WASD Scrolling Off", width=200, timeout=2)
        for _ in range(25):
            time.sleep(0.1)
            if user32.GetForegroundWindow() != before:
                changes.append(user32.GetForegroundWindow())

    t = threading.Thread(target=work)
    t.start()
    splash.mainloop(t)
    print("foreground unchanged" if not changes else f"FOCUS CHANGED: {changes[:3]}")


if __name__ == "__main__":
    main()
