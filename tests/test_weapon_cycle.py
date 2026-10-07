"""Simulated SkyNET weapon switching against the helper's queue / worker /
select logic.  The fake game runs frames: it looks at its keys once a frame
(a key-down edge switches), frames take 30-70 ms (80-200 ms when slow),
weapon keys can be ignored for a while after a switch, the display shows the
new weapon a frame later, and 10% of display reads fail.  Owned weapons:
1, 2, 3, 6 (WeaponMode 1)."""
import pathlib
import random
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import mousehelper as mh  # noqa: E402

OWNED = {1, 2, 3, 6}


class Game:
    def __init__(self, weapon, seed, slow=False, busy=0.0):
        self.rng = random.Random(seed)
        self.weapon = self.shown = weapon
        self.slow, self.busy = slow, busy
        self.busy_until = 0.0
        self.keys = set()
        self.lock = threading.Lock()
        self.done = False
        self.switches = []
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        prev, pending = set(), None
        while not self.done:
            time.sleep(self.rng.uniform(0.08, 0.2) if self.slow
                       else self.rng.uniform(0.03, 0.07))
            now = time.monotonic()
            with self.lock:
                if pending is not None:
                    self.shown, pending = pending, None
                down = set(self.keys)
            for k in down - prev:
                if k in OWNED and k != self.weapon and now >= self.busy_until:
                    self.weapon = pending = k
                    self.busy_until = now + self.busy
                    self.switches.append(k)
            prev = down

    def force(self, k):
        """The game switches by itself (out of ammo...)."""
        with self.lock:
            self.weapon = self.shown = k

    def send(self, ops, wait=True):
        for op, arg in ops:
            if op == mh.CQ_KEY and 0x02 <= arg & 0x7F <= 0x08:
                with self.lock:
                    (self.keys.discard if arg & 0x80 else self.keys.add)(
                        (arg & 0x7F) - 1)
        time.sleep(0.016)                     # the guest's next tick

    def display(self):
        if self.rng.random() < 0.10:
            return None
        with self.lock:
            return bytes([self.shown]) * 45


def make(game, current):
    guest = types.SimpleNamespace(api=None, program=lambda: "GAME")
    sk = mh.SkyNET(guest, mh.Splash(), {}, types.SimpleNamespace(verbose=False))
    sk.in_game = True
    sk.current = current
    sk.hud = game.display
    sk.send = game.send
    return sk


def drain(sk, limit=15):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        time.sleep(0.03)
        with sk.queue_cond:
            idle = not (sk.queued_steps or sk.queued_same)
        if idle and not sk.cycle_lock.locked():
            time.sleep(0.05)
            if not sk.cycle_lock.locked():
                return
    raise SystemExit("FAIL: queue did not drain")


def scenario(name, start, script, seeds=range(12), **game_args):
    """script: ("steps", n) | ("sleep", s) | ("force", k) |
    ("expect", weapon) - checked once the queue is idle."""
    times = []
    for seed in seeds:
        game = Game(start, seed, **game_args)
        sk = make(game, start)
        for item in script:
            kind, arg = item
            if kind == "steps":
                t = time.monotonic()
                sk.queue_weapons(steps=arg)
            elif kind == "sleep":
                time.sleep(arg)
            elif kind == "force":
                drain(sk)
                time.sleep(0.2)
                game.force(arg)
            elif kind == "mark":
                times.clear()
            elif kind == "expect":
                drain(sk)
                times.append(time.monotonic() - t)
                time.sleep(0.25)
                if game.weapon != arg:
                    game.done = True
                    sk.stop.set()
                    raise SystemExit(
                        f"FAIL {name} seed {seed}: weapon {game.weapon}, want "
                        f"{arg}; switches {game.switches}, helper's current {sk.current}")
        game.done = True
        sk.stop.set()
    print(f"ok   {name}: {len(seeds)} runs, slowest step "
          f"{max(times):.2f} s, mean {sum(times) / len(times):.2f} s")


def walk(start, direction, pause=0.4):
    order = [1, 2, 3, 6] if direction > 0 else [6, 3, 2, 1]
    i = order.index(start)
    script = []
    for _ in range(5):
        i = (i + 1) % 4
        script += [("steps", direction), ("expect", order[i]), ("sleep", pause)]
    return script


scenario("single steps up", 1, walk(1, +1))
scenario("single steps down", 1, walk(1, -1))
scenario("single steps up, slow frames", 1, walk(1, +1), slow=True)
scenario("single steps down, slow frames", 1, walk(1, -1), slow=True)
scenario("three quick steps up", 1, [("steps", 1), ("sleep", 0.05),
                                     ("steps", 1), ("sleep", 0.05),
                                     ("steps", 1), ("expect", 6)])
scenario("three quick steps up, slow frames", 1,
         [("steps", 1), ("sleep", 0.05), ("steps", 1), ("sleep", 0.05),
          ("steps", 1), ("expect", 6)], slow=True)
scenario("up 2 then down 1 quickly", 1, [("steps", 2), ("steps", -1),
                                         ("expect", 2)])
# the helper does not see the game switch by itself (nor did the original):
# from its own 6 the next step is 7 (missing), then 1
scenario("game switches by itself, then a step", 1,
         walk(1, +1)[:9] + [("force", 3), ("steps", 1), ("expect", 1)])
scenario("ten quick steps (one round: 2 3 6 1 2 3 6)", 1,
         [("steps", 10), ("expect", 6)], seeds=range(4))
