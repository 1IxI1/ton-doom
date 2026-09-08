#!/usr/bin/env python3
"""Weapon sprite (pistol) for the 1-bit renderer: extract from the WAD, downscale, dither, encode.

Doom draws the pistol (PISGA0, 57x62) at screen x 127..183, y 138..199 of 320x200 and the muzzle flash
(PISFA0, 41x38) at x 141..181, y 98..135. We map 320x200 -> W x H (2:1 pixel aspect for H=120):
column = x * W / 320, row = y * H / 200. Each of our pixels covers a box of Doom pixels; it is opaque
if more than half of the box is sprite, and lit if the mean luminance beats an ordered-dither threshold.

Encoding (level root ref3), see contracts/Doom.tolk `overlayGun`:
  sprite cell: bits x0:uint8 w:uint8, ref0 -> idle columns, ref1 -> flash columns (flash = idle + muzzle)
  columns chain: per column pix:H mask:H (2H bits), 4 columns per cell, ref0 = next cell
Usage: python3 tools/sprites.py assets/doom1.wad --W 80 --H 120 --png gun.png
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boc import Cell, begin_cell  # noqa: E402
from wad import WAD  # noqa: E402

BAYER2 = [[0, 2], [3, 1]]   # thresholds /4


def read_picture(w: WAD, name: str):
    d = w.lump_data(w.find(name))
    width, height, left, top = struct.unpack("<hhhh", d[:8])
    cols = struct.unpack("<%dI" % width, d[8 : 8 + 4 * width])
    img = [[None] * width for _ in range(height)]
    for x, off in enumerate(cols):
        p = off
        while d[p] != 255:
            topdelta, length = d[p], d[p + 1]
            p += 3
            for i in range(length):
                y = topdelta + i
                if 0 <= y < height:
                    img[y][x] = d[p + i]
            p += length + 1
    return width, height, left, top, img


def luminance(pal: bytes, c: int) -> int:
    r, g, b = pal[3 * c : 3 * c + 3]
    return (r * 299 + g * 587 + b * 114) // 1000


SCALE = 1.4        # draw the weapon a bit larger than Doom does (it is tiny at 80 columns)
ANCHOR_X = 155     # pistol centre in 320x200 coords; the sprite is scaled about (ANCHOR_X, 200)


def place(pictures, W: int, H: int, threshold: int = 60):
    """pictures: list of (img, pal, sx, sy) in 320x200 screen coords. Returns dict col -> (pix, mask) ints,
    bit (H-1-row) = row."""
    screen = {}  # (x, y) -> luminance, in scaled 320x200 coords
    for width, height, img, pal, sx, sy in pictures:
        for y in range(height):
            for x in range(width):
                c = img[y][x]
                if c is None:
                    continue
                # scale about the anchor, filling the box the source pixel maps to
                X0 = int(ANCHOR_X + (sx + x - ANCHOR_X) * SCALE); X1 = int(ANCHOR_X + (sx + x + 1 - ANCHOR_X) * SCALE)
                Y0 = int(200 + (sy + y - 200) * SCALE); Y1 = int(200 + (sy + y + 1 - 200) * SCALE)
                for Y in range(Y0, max(Y1, Y0 + 1)):
                    for X in range(X0, max(X1, X0 + 1)):
                        screen[(X, Y)] = luminance(pal, c)
    cols = {}
    for cx in range(W):
        pix = 0
        mask = 0
        for ry in range(H):
            x0, x1 = cx * 320 // W, (cx + 1) * 320 // W
            y0, y1 = ry * 200 // H, (ry + 1) * 200 // H
            if y1 == y0:
                y1 = y0 + 1
            n = 0
            hit = 0
            lum = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    n += 1
                    v = screen.get((x, y))
                    if v is not None:
                        hit += 1
                        lum += v
            if hit * 2 > n:
                mean = lum // hit
                thr = threshold + (BAYER2[ry & 1][cx & 1] - 1) * 24
                bit = 1 if mean > thr else 0
                mask |= 1 << (H - 1 - ry)
                pix |= bit << (H - 1 - ry)
        if mask:
            cols[cx] = (pix, mask)
    # 1-pixel white outline around the silhouette (reads well over the dithered background)
    out = {}
    for cx in range(W):
        m = cols.get(cx, (0, 0))[1]
        ring = (m << 1) | (m >> 1) | cols.get(cx - 1, (0, 0))[1] | cols.get(cx + 1, (0, 0))[1]
        ring &= (1 << H) - 1
        ring &= ~m
        if m or ring:
            pix = cols.get(cx, (0, 0))[0]
            out[cx] = (pix | ring, m | ring)
    return out


def gun_sprites(wad_path: str, W: int, H: int):
    w = WAD.open(wad_path)
    pal = w.lump_data(w.find("PLAYPAL"))[:768]
    gw, gh, gl, gt, gimg = read_picture(w, "PISGA0")
    fw, fh, fl, ft, fimg = read_picture(w, "PISFA0")
    gun_sx, gun_sy = 1 - gl, 32 - gt          # Doom psprite placement (psp->sx = 1, sy = WEAPONTOP 32)
    fl_sx, fl_sy = 1 - fl, 32 - ft
    idle = place([(gw, gh, gimg, pal, gun_sx, gun_sy)], W, H)
    flash = place([(gw, gh, gimg, pal, gun_sx, gun_sy), (fw, fh, fimg, pal, fl_sx, fl_sy)], W, H)
    x0 = min(min(idle), min(flash))
    x1 = max(max(idle), max(flash))
    return x0, x1 - x0 + 1, idle, flash


def encode_columns(cols: dict, x0: int, w: int, H: int) -> Cell:
    nxt = None
    per_cell = 1023 // (2 * H)
    entries = [cols.get(x0 + i, (0, 0)) for i in range(w)]
    chunks = [entries[i : i + per_cell] for i in range(0, len(entries), per_cell)]
    for chunk in reversed(chunks):
        b = begin_cell()
        for pix, mask in chunk:
            b.store_uint(pix, H).store_uint(mask, H)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
    return nxt


def encode_gun(wad_path: str, W: int, H: int) -> Cell:
    x0, w, idle, flash = gun_sprites(wad_path, W, H)
    return (begin_cell().store_uint(x0, 8).store_uint(w, 8)
            .store_ref(encode_columns(idle, x0, w, H)).store_ref(encode_columns(flash, x0, w, H)).end_cell())


def overlay(columns, sprite_cols: dict, H: int, bob: int = 0):
    """Apply the sprite to a frame's column ints (mutates). bob: rows to push the sprite down."""
    FULL = (1 << H) - 1
    for x, (pix, mask) in sprite_cols.items():
        p, m = (pix >> bob) & FULL, (mask >> bob) & FULL
        columns[x] = (columns[x] & ~m) | p
    return columns


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("wad")
    ap.add_argument("--W", type=int, default=80)
    ap.add_argument("--H", type=int, default=120)
    ap.add_argument("--png")
    args = ap.parse_args(argv)
    x0, w, idle, flash = gun_sprites(args.wad, args.W, args.H)
    print(f"gun columns {x0}..{x0 + w - 1} ({w} wide); idle cols {len(idle)}, flash cols {len(flash)}")
    for name, cols in (("idle", idle), ("flash", flash)):
        rows_on = [ry for ry in range(args.H) if any((m >> (args.H - 1 - ry)) & 1 for _, m in cols.values())]
        print(f"  {name}: rows {min(rows_on)}..{max(rows_on)}")
    if args.png:
        from png import write_png
        H, W = args.H, args.W
        tiles = []
        for cols in (idle, flash):
            frame = [(0x55 * ((1 << H) - 1) // 0xFF) if (x & 1) == 0 else 0 for x in range(W)]  # test pattern
            overlay(frame, cols, H)
            tiles.append(frame)
        S = 8
        img_w = 2 * (W * S + 4)
        img_h = H * (S // 2)
        rows = [bytearray([128]) * img_w for _ in range(img_h)]
        for t, frame in enumerate(tiles):
            ox = t * (W * S + 4)
            for y in range(H):
                for x in range(W):
                    v = 235 if (frame[x] >> (H - 1 - y)) & 1 else 0
                    for dy in range(S // 2):
                        rows[y * (S // 2) + dy][ox + x * S : ox + x * S + S] = bytes([v]) * S
        write_png(args.png, img_w, img_h, rows)
        print("wrote", args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
