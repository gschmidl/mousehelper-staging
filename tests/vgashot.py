"""Render the guest's 320x200x256 (mode 13h) video memory to a PNG, read
through the DOSBox Staging API - emulator RAM only, nothing from the host
screen.  The DAC palette is not readable through the API: pass a 768-byte
palette (6-bit VGA values) found in the game's memory, or get a gray ramp."""
import struct
import sys
import zlib

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
import mousehelper  # noqa: E402


def png(path, width, height, rgb_rows):
    raw = b"".join(b"\0" + row for row in rgb_rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9))
    data += chunk(b"IEND", b"")
    open(path, "wb").write(data)


def shot(api, path, palette=None, scale=2):
    pix = api.read(0xA0000, 320 * 200)
    if palette is None:
        pal = [(i, i, i) for i in range(256)]
    else:
        pal = [tuple(min(255, v * 255 // 63) for v in palette[i * 3:i * 3 + 3])
               for i in range(256)]
    rows = []
    for y in range(200):
        row = bytearray()
        for x in range(320):
            row += bytes(pal[pix[y * 320 + x]]) * scale
        for _ in range(scale):
            rows.append(bytes(row))
    png(path, 320 * scale, 200 * scale, rows)
    return pix


if __name__ == "__main__":
    port, out = int(sys.argv[1]), sys.argv[2]
    pal = None
    if len(sys.argv) > 3:
        pal = mousehelper.Api(port).read(int(sys.argv[3], 0), 768)
    shot(mousehelper.Api(port), out, pal)
