#!/usr/bin/env python3
"""Generate the source app icon (1024x1024 PNG) for the Evolve Pods shell.

Stdlib-only (struct + zlib) so it runs anywhere with no pip installs. The
output `source-icon.png` is fed to `cargo tauri icon source-icon.png`, which
derives every platform size (.icns/.ico/PNGs) under this directory.

Motif: a dark ops background with a 2x2 grid of rounded "pod" tiles — one
tile lit (the active pod), echoing the tab-switch UX. No text, no real names.
"""
from __future__ import annotations

import struct
import zlib

N = 1024
BG = (13, 17, 23)        # near-black ops background
TILE = (33, 41, 54)      # inactive pod tile
TILE_ON = (45, 212, 191) # active pod tile (teal accent)
GAP = (13, 17, 23)


def _rounded(px: int, py: int, x0: int, y0: int, size: int, radius: int) -> bool:
    """True if (px,py) is inside a rounded square at (x0,y0) of side `size`."""
    if not (x0 <= px < x0 + size and y0 <= py < y0 + size):
        return False
    # distance into each nearest corner
    dx = min(px - x0, x0 + size - 1 - px)
    dy = min(py - y0, y0 + size - 1 - py)
    if dx >= radius or dy >= radius:
        return True
    cx, cy = radius - dx, radius - dy
    return cx * cx + cy * cy <= radius * radius


def build() -> bytes:
    margin = 150
    grid = N - 2 * margin
    gap = 56
    tile = (grid - gap) // 2
    radius = tile // 4
    # (col, row) of the lit tile
    lit = (1, 0)
    rows = []
    for y in range(N):
        row = bytearray()
        row.append(0)  # PNG filter type 0 for this scanline
        for x in range(N):
            color = BG
            for cy in range(2):
                for cx in range(2):
                    x0 = margin + cx * (tile + gap)
                    y0 = margin + cy * (tile + gap)
                    if _rounded(x, y, x0, y0, tile, radius):
                        color = TILE_ON if (cx, cy) == lit else TILE
            row += bytes((color[0], color[1], color[2], 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


if __name__ == "__main__":
    import pathlib

    out = pathlib.Path(__file__).with_name("source-icon.png")
    out.write_bytes(build())
    print(f"wrote {out} ({out.stat().st_size} bytes)")
