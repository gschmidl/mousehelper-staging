"""Runs the guest's vram_service in Unicorn (16-bit real mode) against a fake
VESA BIOS that, like Staging's, switches banks through CRTC register 6Ah;
then drives the host's Guest.vesa_read / SkyNET.hud() through it."""
import pathlib
import struct
import sys

from unicorn import (UC_ARCH_X86, UC_HOOK_INSN, UC_HOOK_INTR, UC_MODE_16, Uc)
from unicorn.x86_const import (UC_X86_INS_IN, UC_X86_INS_OUT, UC_X86_REG_AX,
                               UC_X86_REG_BP, UC_X86_REG_BX, UC_X86_REG_CS,
                               UC_X86_REG_CX, UC_X86_REG_DI, UC_X86_REG_DS,
                               UC_X86_REG_DX, UC_X86_REG_EFLAGS, UC_X86_REG_ES,
                               UC_X86_REG_SI, UC_X86_REG_SP, UC_X86_REG_SS)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import guestcode  # noqa: E402
import mousehelper as mh  # noqa: E402

SYM = guestcode.SYMBOLS
SEG = 0x9EED
BASE = SEG * 16
HLT = 0x2000                            # offset of a HLT behind the block
VRAM = bytes((i * 7 + (i >> 16) * 31 + (i >> 9)) & 0xFF for i in range(1 << 20))


class Machine:
    def __init__(self, pitch=640, start=(0, 0), fail=None):
        self.uc = Uc(UC_ARCH_X86, UC_MODE_16)
        self.uc.mem_map(0, 0x110000)
        self.uc.mem_write(BASE, bytes(guestcode.BLOB))
        self.uc.mem_write(BASE + HLT, b"\xF4")
        self.pitch, self.start, self.fail = pitch, start, fail
        self.crtc_index = 0x11
        self.bank = 3
        self.map_bank()
        self.int10 = []
        self.uc.hook_add(UC_HOOK_INTR, self.on_int)
        self.uc.hook_add(UC_HOOK_INSN, self.on_in, None, 1, 0, UC_X86_INS_IN)
        self.uc.hook_add(UC_HOOK_INSN, self.on_out, None, 1, 0, UC_X86_INS_OUT)

    def map_bank(self):
        self.uc.mem_write(0xA0000, VRAM[self.bank << 16:(self.bank + 1) << 16])

    def on_in(self, uc, port, size, _):
        return self.crtc_index if port == 0x3D4 else 0xFF

    def on_out(self, uc, port, size, value, _):
        if port == 0x3D4:
            self.crtc_index = value & 0xFF

    def on_int(self, uc, intno, _):
        assert intno == 0x10, intno
        ax, bx = uc.reg_read(UC_X86_REG_AX), uc.reg_read(UC_X86_REG_BX)
        self.int10.append((ax, bx))
        if self.fail == ax:
            uc.reg_write(UC_X86_REG_AX, 0x014F)
            return
        if ax == 0x4F06 and bx & 0xFF == 1:
            uc.reg_write(UC_X86_REG_BX, self.pitch)
            uc.reg_write(UC_X86_REG_CX, self.pitch)
            uc.reg_write(UC_X86_REG_DX, 1600)
        elif ax == 0x4F07 and bx == 1:
            uc.reg_write(UC_X86_REG_CX, self.start[0])
            uc.reg_write(UC_X86_REG_DX, self.start[1])
        elif ax == 0x4F05 and bx == 0x0100:
            self.crtc_index = 0x6A
            uc.reg_write(UC_X86_REG_DX, self.bank)
        elif ax == 0x4F05 and bx == 0:
            self.crtc_index = 0x6A
            self.bank = uc.reg_read(UC_X86_REG_DX) & 0xFF
            self.map_bank()
        else:
            raise AssertionError(f"unexpected INT 10h AX={ax:04X} BX={bx:04X}")
        uc.reg_write(UC_X86_REG_AX, 0x004F)

    def tick(self):
        """Call vram_service as int70_hook does (DS = CS), with DF set."""
        uc = self.uc
        regs = {UC_X86_REG_AX: 0x0099, UC_X86_REG_BX: 0x1111,
                UC_X86_REG_CX: 0x2222, UC_X86_REG_DX: 0x3333,
                UC_X86_REG_SI: 0x4444, UC_X86_REG_DI: 0x5555,
                UC_X86_REG_BP: 0x6666, UC_X86_REG_ES: 0x7777}
        for r, v in regs.items():
            uc.reg_write(r, v)
        uc.reg_write(UC_X86_REG_CS, SEG)
        uc.reg_write(UC_X86_REG_DS, SEG)
        uc.reg_write(UC_X86_REG_SS, 0x7000)
        uc.reg_write(UC_X86_REG_EFLAGS, 0x0402)           # DF=1, IF=0
        uc.reg_write(UC_X86_REG_SP, 0xFFFC)               # near return -> HLT
        uc.mem_write(0x7000 * 16 + 0xFFFC, struct.pack("<H", HLT))
        uc.emu_start(BASE + SYM["vram_service"], BASE + HLT, count=100000)
        assert uc.reg_read(UC_X86_REG_SP) == 0xFFFE, hex(uc.reg_read(UC_X86_REG_SP))
        for r, v in regs.items():
            assert uc.reg_read(r) == v, (r, hex(uc.reg_read(r)))

    def read(self, sym, n):
        return bytes(self.uc.mem_read(BASE + SYM[sym], n))

    def write(self, sym, data):
        self.uc.mem_write(BASE + SYM[sym], bytes(data))


def request(m, runs):
    entries = b"".join(struct.pack("<BBH", off >> 16, n, off & 0xFFFF)
                       for off, n in runs)
    m.write("vram_run", entries)
    m.write("vram_req", [len(runs)])


# 1. copies across banks, restores bank and CRTC index, keeps registers
m = Machine(start=(0, 480))
runs = [(286720 + 109, 5), (0x4FFF0, 16), (0x0, 3), (0x7A123, 7)]
request(m, runs)
seq = m.read("vram_seq", 1)[0]
m.tick()
want = b"".join(VRAM[o:o + n] for o, n in runs)
got = m.read("vram_buf", len(want))
assert got == want, (got.hex(), want.hex())
assert m.read("vram_ok", 1) == b"\x01"
assert m.read("vram_req", 1) == b"\x00"
assert m.read("vram_seq", 1)[0] == (seq + 1) & 0xFF
assert m.bank == 3, m.bank
assert m.crtc_index == 0x11, hex(m.crtc_index)
pitch, sx, sy = struct.unpack("<HHH", m.read("vram_pitch", 6))
assert (pitch, sx, sy) == (640, 0, 480), (pitch, sx, sy)
print(f"ok   4 runs copied ({len(want)} bytes), bank 3 and CRTC index 11h "
      f"back, registers kept, DF set by the caller; geometry {pitch} {sx},{sy}")

# 2. nothing requested: no BIOS calls
m.int10.clear()
m.tick()
assert m.int10 == [] and m.read("vram_seq", 1)[0] == (seq + 1) & 0xFF
print("ok   no request: no INT 10h")

# 3. BIOS refuses: ok=0, answered, bank/index untouched
for fail in (0x4F06, 0x4F07, 0x4F05):
    m = Machine(fail=fail)
    request(m, [(1000, 4)])
    m.tick()
    assert m.read("vram_ok", 1) == b"\x00" and m.read("vram_req", 1) == b"\x00"
    assert m.read("vram_seq", 1) == b"\x01" and m.bank == 3
    assert m.crtc_index == 0x11
print("ok   a refusing VESA BIOS: vram_ok 0, answered, stack balanced")

# 4. more than the buffer: clamped, nothing written past vram_buf
m = Machine()
after = m.read("vram_buf", mh.VRAM_BUF + 16)[mh.VRAM_BUF:]
request(m, [(i * 300, 255) for i in range(16)])
m.uc.mem_write(BASE + SYM["vram_req"], bytes([20]))     # more runs than exist
m.tick()
assert m.read("vram_buf", mh.VRAM_BUF + 16)[mh.VRAM_BUF:] == after
assert m.read("vram_buf", mh.VRAM_BUF) == VRAM[0:mh.VRAM_BUF]
print("ok   oversized request clamped to the buffer")


# 5. the host side through the emulated guest
class EmuApi:
    """API stand-in: memory is the emulator's; a write to vram_req runs the
    guest's tick right away."""
    def __init__(self, m):
        self.m = m

    def read(self, addr, n):
        return bytes(self.m.uc.mem_read(addr, n))

    def write(self, addr, data, expect=None):
        self.m.uc.mem_write(addr, bytes(data))
        if addr == BASE + SYM["vram_req"] and data[0]:
            self.m.tick()
        return True


for start in ((0, 0), (0, 480)):
    m = Machine(start=start)
    m.uc.mem_write(0x449, b"\x69")
    g = mh.Guest(EmuApi(m))
    g.base, g.seg = BASE, SEG
    sk = mh.SkyNET.__new__(mh.SkyNET)
    sk.api, sk.guest, sk.vesa_geometry = g.api, g, (640, 0)
    px = sk.hud()
    off = start[1] * 640 + start[0]
    want = b"".join(VRAM[off + y * 640 + cx - 2:off + y * 640 + cx + 3]
                    for cx, y in mh.SkyNET.HUD640)
    assert px == want, (px.hex(), want.hex())
    assert sk.vesa_geometry == (640, off)
    print(f"ok   SkyNET.hud() at 640x480, display start line {start[1]}: "
          f"{len(px)} bytes, bank {m.bank} kept")
m.uc.mem_write(0x449, b"\x13")
for x, y in mh.SkyNET.HUD:
    m.uc.mem_write(0xA0000 + y * 320 + x, b"\x05")
assert sk.hud() == b"\x05" * len(mh.SkyNET.HUD)
print("ok   SkyNET.hud() at 320x200 reads A0000h directly")
