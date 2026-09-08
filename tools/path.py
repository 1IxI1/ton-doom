#!/usr/bin/env python3
"""Autopilot for demo routes: turns toward waypoints and walks, with a wall-crossing validator.

Usage:
  python3 tools/path.py assets/e1m1.json                 # validate the default loop, print stats
  python3 tools/path.py assets/e1m1.json --dump route.txt
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render import ANGLES, FRAC, Level, Player, cos_a, sin_a  # noqa: E402

TURN_MAX = 8        # angle units per frame (5.6 deg)
STEP_MAX = 24       # max climbable step (Doom: 24)
MIN_GAP = 56        # min ceiling - floor to pass (Doom: 56)

# waypoints in map units: a loop through the E1M1 hangar, the nukage courtyard and back
DEFAULT_ROUTE = [
    (1056, -3616), (1056, -3420), (1056, -3230), (1200, -3000), (1344, -2820), (1344, -2600),
    (1344, -2820), (1200, -3000), (1000, -3150), (800, -3250), (600, -3232), (400, -3232),
    (280, -3232), (400, -3300), (600, -3232), (800, -3300), (880, -3500), (1056, -3560),
]


class Blocker:
    """Blocking linedefs: one-sided, or two-sided with an unclimbable step / low gap."""

    def __init__(self, data):
        self.lines = []
        verts, sectors = data["vertexes"], data["sectors"]
        for ld in data["linedefs"]:
            v1, v2 = verts[ld["v1"]], verts[ld["v2"]]
            block = ld["sector_left"] < 0
            if not block:
                a, b = sectors[ld["sector_right"]], sectors[ld["sector_left"]]
                lo = max(a["floor"], b["floor"])
                hi = min(a["ceiling"], b["ceiling"])
                if hi - lo < MIN_GAP or abs(a["floor"] - b["floor"]) > STEP_MAX:
                    block = True
            if block:
                self.lines.append((v1["x"], v1["y"], v2["x"], v2["y"]))
        # note: steps > STEP_MAX block in both directions here (conservative)

    def crosses(self, x0, y0, x1, y1):
        for (ax, ay, bx, by) in self.lines:
            if segments_intersect(x0, y0, x1, y1, ax, ay, bx, by):
                return (ax, ay, bx, by)
        return None


def _ccw(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(x0, y0, x1, y1, ax, ay, bx, by):
    d1 = _ccw(ax, ay, bx, by, x0, y0)
    d2 = _ccw(ax, ay, bx, by, x1, y1)
    d3 = _ccw(x0, y0, x1, y1, ax, ay)
    d4 = _ccw(x0, y0, x1, y1, bx, by)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def angle_to(px, py, tx, ty) -> int:
    a = math.atan2(ty - py, tx - px)
    return int(round(a / (2 * math.pi) * ANGLES)) % ANGLES


def autopilot(level: Level, route, max_frames=100000, loop=True):
    """Yields (turn, fwd, side) inputs following the waypoints; player state simulated exactly."""
    sx, sy, sa = level.player_start
    p = Player(level, sx, sy, sa)
    i = 0
    frames = 0
    while frames < max_frames:
        tx, ty = route[i % len(route)] if loop else route[min(i, len(route) - 1)]
        px, py = p.x / 65536, p.y / 65536
        if math.hypot(tx - px, ty - py) < 12:
            i += 1
            if not loop and i >= len(route):
                return
            continue
        want = angle_to(px, py, tx, ty)
        diff = (want - p.angle + ANGLES // 2) % ANGLES - ANGLES // 2
        turn = max(-TURN_MAX, min(TURN_MAX, diff))
        fwd = 1 if abs(diff) < ANGLES // 8 else 0
        p.move(turn, fwd, 0)
        frames += 1
        yield (turn, fwd, 0), p


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--dump")
    args = ap.parse_args(argv)
    with open(args.level) as f:
        data = json.load(f)
    level = Level(data)
    blocker = Blocker(data)
    inputs = []
    prev = None
    bad = 0
    for (inp, p) in autopilot(level, DEFAULT_ROUTE, args.frames):
        x, y = p.x / 65536, p.y / 65536
        if prev is not None:
            hit = blocker.crosses(prev[0], prev[1], x, y)
            if hit:
                bad += 1
                if bad <= 10:
                    print(f"frame {len(inputs)}: crossing wall {hit} at ({x:.0f},{y:.0f})")
        prev = (x, y)
        inputs.append(inp)
    print(f"{len(inputs)} frames, wall crossings: {bad}, final pos ({prev[0]:.0f},{prev[1]:.0f})")
    if args.dump:
        with open(args.dump, "w") as f:
            f.write("\n".join(f"{t},{fw},{s}" for t, fw, s in inputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
