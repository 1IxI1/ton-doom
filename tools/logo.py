#!/usr/bin/env python3
"""The DOOM logo (M_DOOM from the WAD) recolored in TON blue, as an RGBA PNG for the viewer header.

Usage: python3 tools/logo.py assets/doom1.wad viewer/logo.png [--scale 2]
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sprites import luminance, read_picture  # noqa: E402
from wad import WAD  # noqa: E402

TON_BLUE = (0, 152, 234)
DARK = (2, 34, 74)
LIGHT = (196, 230, 255)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def tint(lum: int):
    """Luminance 0..255 of the red logo -> a blue ramp: dark blue, TON blue at the mid tones, light at highlights."""
    t = lum / 255.0
    if t < 0.55:
        return lerp(DARK, TON_BLUE, t / 0.55)
    return lerp(TON_BLUE, LIGHT, (t - 0.55) / 0.45)


def write_rgba_png(path, width, height, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("wad")
    ap.add_argument("out")
    ap.add_argument("--scale", type=int, default=2, help="integer upscale (nearest) for crisp downsampling in the browser")
    args = ap.parse_args(argv)
    w = WAD.open(args.wad)
    pal = w.lump_data(w.find("PLAYPAL"))[:768]
    width, height, _, _, img = read_picture(w, "M_DOOM")
    lums = [[luminance(pal, c) if c is not None else None for c in row] for row in img]
    lo = min(v for row in lums for v in row if v is not None)
    hi = max(v for row in lums for v in row if v is not None)
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            v = lums[y][x]
            if v is None:
                px = (0, 0, 0, 0)
            else:
                r, g, b = tint((v - lo) * 255 // max(1, hi - lo))
                px = (r, g, b, 255)
            for _ in range(args.scale):
                row += bytes(px)
        for _ in range(args.scale):
            rows.append(bytes(row))
    write_rgba_png(args.out, width * args.scale, height * args.scale, rows)
    print(f"wrote {args.out}: {width * args.scale}x{height * args.scale}, luminance range {lo}..{hi}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
