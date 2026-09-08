#!/usr/bin/env python3
"""Generate golden frames for the contract tests.

Runs the reference renderer along an input path (same movement model as the contract) and writes
a BOC:  root bits: w:16 h:16 count:8   ref0: inputs cell chain (count x (turn:int8 fwd:int8 side:int8), 40 per cell,
                                        next in ref0)
        ref1: frames chain: per frame cell: frameNo:32 px:int32 py:int32 angle:16 viewz:int16 hash:256, next in ref0

Usage: python3 tools/golden.py assets/e1m1.json assets/golden/walk.boc --W 128 --H 96 --path walk [--png-dir dir]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boc import begin_cell  # noqa: E402
from render import ANGLES, FRAC, Level, Player, Renderer, frame_cell_chain, load_level  # noqa: E402

PATHS = {
    # (turn, forward, strafe) per frame
    "walk": [(0, 1, 0)] * 6 + [(-4, 1, 0)] * 8 + [(0, 1, 0)] * 6 + [(6, 0, 0)] * 10 + [(0, 1, 1)] * 6 + [(-3, 1, 0)] * 4,
    "spin": [(8, 0, 0)] * 16,
    "still": [(0, 0, 0)] * 3,
}


def gen(level: Level, inputs, W: int, H: int, png_dir=None, aspect_y: int = 1):
    sx, sy, sa = level.player_start
    player = Player(level, sx, sy, sa)
    r = Renderer(level, W, H, aspect_y)
    frames = []
    for i, (turn, fwd, side) in enumerate(inputs):
        player.move(turn, fwd, side)
        viewz = player.viewz()
        cols = r.render(player.x, player.y, player.angle, viewz, stats=True)
        chain = frame_cell_chain(cols, W, H)
        frames.append((i + 1, player.x, player.y, player.angle, viewz >> FRAC, chain.hash(), r.stats, cols))
        if png_dir:
            from png import write_bitmap_png
            os.makedirs(png_dir, exist_ok=True)
            write_bitmap_png(os.path.join(png_dir, f"frame{i+1:03d}.png"), cols, W, H, 3)
    return frames


def build_boc(inputs, frames, W, H):
    nxt = None
    for chunk in reversed([inputs[i : i + 40] for i in range(0, len(inputs), 40)]):
        b = begin_cell()
        for turn, fwd, side in chunk:
            b.store_int(turn, 8).store_int(fwd, 8).store_int(side, 8)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
    inputs_cell = nxt
    nxt = None
    for frame_no, px, py, angle, viewz, h, _, _ in reversed(frames):
        b = begin_cell().store_uint(frame_no, 32).store_int(px, 32).store_int(py, 32).store_uint(angle, 16).store_int(viewz, 16)
        b.store_uint(int.from_bytes(h, "big"), 256)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
    return begin_cell().store_uint(W, 16).store_uint(H, 16).store_uint(len(inputs), 8).store_ref(inputs_cell).store_ref(nxt).end_cell()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("out")
    ap.add_argument("--W", type=int, default=128)
    ap.add_argument("--H", type=int, default=96)
    ap.add_argument("--path", default="walk")
    ap.add_argument("--png-dir")
    ap.add_argument("--aspect", type=int, default=1, help="rows per column unit (2 for 80x120 shown as 4:3)")
    args = ap.parse_args(argv)
    level = load_level(args.level)
    inputs = PATHS[args.path]
    frames = gen(level, inputs, args.W, args.H, args.png_dir, args.aspect)
    root = build_boc(inputs, frames, args.W, args.H)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(root.to_boc())
    for frame_no, px, py, angle, viewz, h, st, _ in frames:
        print(f"frame {frame_no:3d} pos=({px/65536:.1f},{py/65536:.1f}) a={angle} z={viewz} hash={h.hex()[:16]} "
              f"segs_drawn={st['segs_drawn']} col_events={st['col_events']} nodes={st['nodes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
