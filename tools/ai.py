#!/usr/bin/env python3
"""On-chain wanderer AI, reference implementation (must match contracts/Doom.tolk `aiInput`/`blocked`).

Each frame:
  rnd = LCG(rnd)
  if blocked(pos -> pos + PROBE ahead):        turning episode (no move)
  else: gentle random drift; newpos = pos + SPEED ahead(angle+turn); if blocked(pos -> newpos): turning episode
Collision: tools/blockmap.py (segment intersection against the blocking lines of the touched blocks).

Usage: python3 tools/ai.py assets/e1m1.json --frames 3000 --png route.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blockmap import Blockmap  # noqa: E402
from render import ANGLES, FRAC, Level, cos_a, sin_a  # noqa: E402

SPEED_FWD = 8
TURN_RATE = 6        # angle units per frame while turning on the spot
STEER_RATE = 3       # while walking and steering away from a wall
PROBE_NEAR = 24      # must be free to walk
PROBE_FAR = 72       # if blocked: steer
PROBE_SIDE = 48      # whiskers at +-45 degrees
SIDE_ANGLE = ANGLES // 8
STEP_MAX = 24
MIN_GAP = 56


def lcg(r: int) -> int:
    return (r * 1103515245 + 12345) & 0xFFFFFFFF


def blocked(level: Level, px: int, py: int, nx: int, ny: int) -> bool:
    """Does the move (16.16 coordinates) cross a blocking line? Uses the level's blockmap."""
    return level.blockmap.blocked(px, py, nx, ny)


class AiState:
    def __init__(self, x: int, y: int, angle: int, rnd: int = 1, turn_dir: int = 0):
        self.x, self.y, self.angle = x, y, angle
        self.rnd = rnd
        self.turn_dir = turn_dir


def ai_step(level: Level, st: AiState):
    """Advances the state by one frame; returns the (turn, fwd) it applied. Mirrors Doom.tolk aiStep."""
    st.rnd = lcg(st.rnd)
    x, y, a = st.x, st.y, st.angle

    def probe(dist, da):
        aa = (a + da) % ANGLES
        return blocked(level, x, y, x + dist * cos_a(aa), y + dist * sin_a(aa))

    near = probe(PROBE_NEAR, 0)
    left = probe(PROBE_SIDE, SIDE_ANGLE)
    right = probe(PROBE_SIDE, -SIDE_ANGLE)
    if near:
        # turn on the spot toward the free side; keep the direction for the whole episode
        if st.turn_dir == 0:
            if left != right:
                st.turn_dir = 1 if right else -1
            else:
                st.turn_dir = 1 if (st.rnd >> 16) & 1 else -1
        turn, fwd = st.turn_dir * TURN_RATE, 0
    else:
        st.turn_dir = 0
        far = probe(PROBE_FAR, 0)
        if left != right:
            turn = STEER_RATE if right else -STEER_RATE      # steer away from the blocked side
        elif far:
            turn = STEER_RATE if (st.rnd >> 16) & 1 else -STEER_RATE
        else:
            turn = (((st.rnd >> 8) % 3) - 1) if ((st.rnd >> 12) & 3) == 0 else 0   # rare gentle drift
        fwd = 1
        a2 = (a + turn) % ANGLES
        if blocked(level, x, y, x + SPEED_FWD * cos_a(a2), y + SPEED_FWD * sin_a(a2)):
            st.turn_dir = 1 if (st.rnd >> 16) & 1 else -1
            turn, fwd = st.turn_dir * TURN_RATE, 0
    st.angle = (st.angle + turn) % ANGLES
    if fwd:
        st.x += SPEED_FWD * cos_a(st.angle)
        st.y += SPEED_FWD * sin_a(st.angle)
    return turn, fwd


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("--frames", type=int, default=2000)
    ap.add_argument("--png")
    args = ap.parse_args(argv)
    data = json.load(open(args.level))
    level = Level(data)
    level.blockmap = Blockmap(data)
    sx, sy, sa = level.player_start
    st = AiState(sx << FRAC, sy << FRAC, sa * ANGLES // 360)
    path = []
    moves = 0
    for _ in range(args.frames):
        turn, fwd = ai_step(level, st)
        moves += fwd
        path.append((st.x / 65536, st.y / 65536))
    xs = [p[0] for p in path]; ys = [p[1] for p in path]
    print(f"{args.frames} frames, moved on {moves}, bbox x [{min(xs):.0f},{max(xs):.0f}] y [{min(ys):.0f},{max(ys):.0f}], final ({xs[-1]:.0f},{ys[-1]:.0f})")
    if args.png:
        from mapview import Canvas  # noqa: F401
        from png import write_png
        verts = data["vertexes"]
        minx = min(v["x"] for v in verts); maxx = max(v["x"] for v in verts)
        miny = min(v["y"] for v in verts); maxy = max(v["y"] for v in verts)
        W = 1200; scale = W / (maxx - minx); H = int((maxy - miny) * scale) + 1
        rows = [bytearray([255]) * W for _ in range(H)]
        def put(x, y, v):
            px = int((x - minx) * scale); py = int((maxy - y) * scale)
            if 0 <= px < W and 0 <= py < H:
                rows[py][px] = v
        def line(x0, y0, x1, y1, v):
            n = int(max(abs(x1 - x0), abs(y1 - y0)) * scale) + 1
            for i in range(n + 1):
                t = i / n
                put(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, v)
        for ld in data["linedefs"]:
            a, b = verts[ld["v1"]], verts[ld["v2"]]
            line(a["x"], a["y"], b["x"], b["y"], 0 if ld["sector_left"] < 0 else 170)
        for i in range(1, len(path)):
            line(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1], 90)
        write_png(args.png, W, H, rows)
        print("wrote", args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
