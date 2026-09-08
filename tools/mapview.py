#!/usr/bin/env python3
"""
mapview.py - render a Doom map's linedefs top-down to a PNG (stdlib only).

  python3 tools/mapview.py assets/doom1.wad E1M1 assets/e1m1-map.png [--width 1200] [--segs]

One-sided linedefs are drawn dark, two-sided linedefs light grey, the player 1
start is a red disc with a heading tick, other things are small dots.
With --segs the BSP segs are drawn instead of linedefs (coloured per subsector).
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wad import WAD  # noqa: E402


# ---------------------------------------------------------------------------
# minimal RGB PNG writer


class Canvas:
    def __init__(self, width: int, height: int, bg=(255, 255, 255)):
        self.w, self.h = width, height
        self.px = bytearray(bg * (width * height))

    def set(self, x: int, y: int, rgb) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = 3 * (y * self.w + x)
            self.px[i:i + 3] = bytes(rgb)

    def line(self, x0: int, y0: int, x1: int, y1: int, rgb) -> None:
        """Bresenham."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set(x0, y0, rgb)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def disc(self, cx: int, cy: int, r: int, rgb) -> None:
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self.set(x, y, rgb)

    def png(self) -> bytes:
        raw = bytearray()
        stride = 3 * self.w
        for y in range(self.h):
            raw.append(0)  # filter type 0 (None)
            raw += self.px[y * stride:(y + 1) * stride]

        def chunk(tag: bytes, body: bytes) -> bytes:
            return (struct.pack(">I", len(body)) + tag + body
                    + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)  # 8-bit RGB
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------


def render(m: dict, width: int = 1200, margin: int = 24, draw_segs: bool = False) -> Canvas:
    V = m["vertexes"]
    xs = [v["x"] for v in V]
    ys = [v["y"] for v in V]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    scale = (width - 2 * margin) / max(1, maxx - minx)
    height = int((maxy - miny) * scale) + 2 * margin

    def tx(x: float) -> int:
        return int(round(margin + (x - minx) * scale))

    def ty(y: float) -> int:  # Doom y axis points up; image y points down
        return int(round(margin + (maxy - y) * scale))

    c = Canvas(width, height)

    if draw_segs:
        palette = [(200, 40, 40), (40, 140, 40), (40, 60, 220), (200, 140, 0),
                   (140, 0, 180), (0, 150, 160), (120, 80, 0), (0, 0, 0)]
        for si, ss in enumerate(m["ssectors"]):
            rgb = palette[si % len(palette)]
            for seg in m["segs"][ss["firstseg"]:ss["firstseg"] + ss["numsegs"]]:
                a, b = V[seg["v1"]], V[seg["v2"]]
                c.line(tx(a["x"]), ty(a["y"]), tx(b["x"]), ty(b["y"]), rgb)
    else:
        # two-sided first (light) so one-sided (dark) draws on top where they overlap
        for pass_two_sided, rgb in ((True, (170, 170, 170)), (False, (20, 20, 20))):
            for ld in m["linedefs"]:
                if ld["two_sided"] != pass_two_sided:
                    continue
                a, b = V[ld["v1"]], V[ld["v2"]]
                c.line(tx(a["x"]), ty(a["y"]), tx(b["x"]), ty(b["y"]), rgb)

    for t in m["things"]:
        if t["type"] == 1:
            continue
        c.disc(tx(t["x"]), ty(t["y"]), 1, (90, 140, 220))

    for t in m["things"]:
        if t["type"] == 1:
            px, py = tx(t["x"]), ty(t["y"])
            c.disc(px, py, 5, (220, 30, 30))
            ang = math.radians(t["angle"])
            c.line(px, py, int(round(px + 14 * math.cos(ang))), int(round(py - 14 * math.sin(ang))),
                   (220, 30, 30))
    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render Doom map linedefs to PNG.")
    ap.add_argument("wad")
    ap.add_argument("map")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--segs", action="store_true", help="draw BSP segs instead of linedefs")
    args = ap.parse_args(argv)

    wad = WAD.open(args.wad)
    m = wad.read_map(args.map)
    c = render(m, args.width, draw_segs=args.segs)
    data = c.png()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"wrote {args.out}: {c.w}x{c.h} RGB, {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
