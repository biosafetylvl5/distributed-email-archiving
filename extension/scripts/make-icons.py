"""Draw the extension's icons (icons/<size>.png): a white envelope on a purple
rounded square. Pure Python, so it needs nothing installed:
    python3 scripts/make-icons.py
"""

import struct
import zlib
from pathlib import Path

BG = (0x5B, 0x3F, 0xA0)
FG = (0xFF, 0xFF, 0xFF)
SS = 4  # supersampling per axis


def coverage(x: float, y: float) -> tuple[float, float]:
    """(background, envelope) coverage at a point of the unit square."""
    r = 0.2  # corner radius
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    inside = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    if not inside:
        return 0.0, 0.0
    # envelope body: 0.2..0.8 x 0.3..0.72, outline width w
    left, right, top, bottom, w = 0.2, 0.8, 0.3, 0.72, 0.065
    in_box = left <= x <= right and top <= y <= bottom
    border = in_box and (x < left + w or x > right - w or y < top + w or y > bottom - w)
    # flap: lines from the top corners to the middle
    mid_x, mid_y = 0.5, 0.54
    flap = False
    if in_box and y <= mid_y + w:
        for (x0, y0) in ((left, top), (right, top)):
            dx, dy = mid_x - x0, mid_y - y0
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
            px, py = x0 + t * dx, y0 + t * dy
            if (x - px) ** 2 + (y - py) ** 2 <= (w / 1.6) ** 2:
                flap = True
    return 1.0, 1.0 if (border or flap) else 0.0


def png(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            a = e = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    ca, ce = coverage((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    a += ca
                    e += ce
            a /= SS * SS
            e /= SS * SS
            mix = e / a if a else 0
            rgb = [round(b * (1 - mix) + f * mix) for b, f in zip(BG, FG)]
            row += bytes(rgb + [round(a * 255)])
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "icons"
    out.mkdir(exist_ok=True)
    for size in (16, 32, 48, 128):
        (out / f"{size}.png").write_bytes(png(size))
