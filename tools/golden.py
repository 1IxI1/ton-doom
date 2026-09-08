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
from render import ANGLES, FRAC, VIEWHEIGHT, Level, Player, Renderer, cos_a, frame_cell_chain, load_level, sin_a  # noqa: E402

PATHS = {
    # (turn, forward, strafe) per frame
    "walk": [(0, 1, 0)] * 6 + [(-4, 1, 0)] * 8 + [(0, 1, 0)] * 6 + [(6, 0, 0)] * 10 + [(0, 1, 1)] * 6 + [(-3, 1, 0)] * 4,
    "spin": [(8, 0, 0)] * 16,
    "still": [(0, 0, 0)] * 3,
}


def gen(level: Level, inputs, W: int, H: int, png_dir=None, aspect_y: int = 1, ai_frames: int = 0, wad=None):
    """inputs: list of (turn, fwd, side); ai_frames > 0: instead drive the on-chain wanderer AI (tools/ai.py).
    Frames get the weapon overlay (bob while walking, muzzle flash on random shots), like the contract."""
    from ai import AiState, ai_step, frame_begin, gun_bob
    from sprites import gun_sprites, overlay
    sx, sy, sa = level.player_start
    st = AiState(sx << FRAC, sy << FRAC, sa * ANGLES // 360)
    r = Renderer(level, W, H, aspect_y)
    x0, w, idle, flash = gun_sprites(wad or os.path.join(os.path.dirname(__file__), "..", "assets", "doom1.wad"), W, H)
    frames = []
    n = ai_frames or len(inputs)
    if ai_frames:
        inputs = []
    for i in range(n):
        frame_begin(st)
        if ai_frames:
            turn, fwd = ai_step(level, st)
            inputs.append((turn, fwd, 0))
        else:
            turn, fwd, side = inputs[i]
            st.angle = (st.angle + turn) % ANGLES
            c, s_ = cos_a(st.angle), sin_a(st.angle)
            st.x += fwd * 8 * c + side * 6 * s_
            st.y += fwd * 8 * s_ - side * 6 * c
            st.moving = fwd != 0
        viewz = (level.point_in_subsector(st.x, st.y).floor + VIEWHEIGHT) << FRAC
        cols = r.render(st.x, st.y, st.angle, viewz, stats=True)
        overlay(cols, flash if st.gun > 0 else idle, H, gun_bob(st))
        chain = frame_cell_chain(cols, W, H)
        frames.append((i + 1, st.x, st.y, st.angle, viewz >> FRAC, chain.hash(), r.stats, cols))
        if png_dir:
            from png import write_bitmap_png
            os.makedirs(png_dir, exist_ok=True)
            write_bitmap_png(os.path.join(png_dir, f"frame{i+1:03d}.png"), cols, W, H, 3)
    return frames


def build_boc(inputs, frames, W, H):
    nxt = None
    chunks = [inputs[i : i + 40] for i in range(0, len(inputs), 40)] or [[]]
    for chunk in reversed(chunks):
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
    count = len(inputs) if inputs else len(frames)   # AI goldens carry no inputs: count = frames
    return begin_cell().store_uint(W, 16).store_uint(H, 16).store_uint(count, 8).store_ref(inputs_cell).store_ref(nxt).end_cell()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("out")
    ap.add_argument("--W", type=int, default=128)
    ap.add_argument("--H", type=int, default=96)
    ap.add_argument("--path", default="walk")
    ap.add_argument("--png-dir")
    ap.add_argument("--aspect", type=int, default=1, help="rows per column unit (2 for 80x120 shown as 4:3)")
    ap.add_argument("--ai", type=int, default=0, help="drive the wanderer AI for this many frames instead of a path")
    args = ap.parse_args(argv)
    level = load_level(args.level)
    if args.ai:
        import json as _json
        from blockmap import Blockmap
        with open(args.level) as f:
            level.blockmap = Blockmap(_json.load(f)) if False else None
        # the blockmap must see the opened doors: rebuild from the same (mutated) data the level was built from
        from render import open_doors
        data = _json.load(open(args.level)); open_doors(data); level.blockmap = Blockmap(data)
        frames = gen(level, [], args.W, args.H, args.png_dir, args.aspect, ai_frames=args.ai)
        inputs = [(0, 0, 0)] * 0
    else:
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
