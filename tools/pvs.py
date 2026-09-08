#!/usr/bin/env python3
"""Sampled potentially-visible-set (PVS) per subsector, and a simulation of BSP pruning with it.

For every subsector we render from a few sample points inside it in 8 directions (with a wide FOV
renderer pass) and record which subsectors contributed drawn segs. Conservative dilation: the PVS
of a subsector also includes the PVS of its BSP siblings (cheap safety margin).

Usage: python3 tools/pvs.py assets/e1m1.json [--samples 3] [--out assets/e1m1-pvs.json]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402
from render import ANGLES, FRAC, Level, Renderer, VIEWHEIGHT  # noqa: E402


def subsector_points(level: Level, ss, rnd, n):
    """Sample points inside the subsector: centroid of its seg vertices, pulled toward each vertex."""
    verts = []
    for seg in ss.segs:
        verts.append((seg.x1, seg.y1))
        verts.append((seg.x2, seg.y2))
    if not verts:
        return []
    cx = sum(v[0] for v in verts) / len(verts)
    cy = sum(v[1] for v in verts) / len(verts)
    pts = [(cx, cy)]
    for _ in range(n - 1):
        vx, vy = rnd.choice(verts)
        t = rnd.uniform(0.2, 0.8)
        pts.append((cx + (vx - cx) * t, cy + (vy - cy) * t))
    return pts


class Recorder(Renderer):
    def __init__(self, level, W, H):
        super().__init__(level, W, H)
        self.seen = set()

    def render_subsector(self, ss):
        self.cur_ss = ss.idx
        super().render_subsector(ss)

    def add_seg(self, seg):
        before = self.stats["segs_drawn"] if self.stats else 0
        super().add_seg(seg)
        if self.stats and self.stats["segs_drawn"] > before:
            self.seen.add(self.cur_ss)


def compute_pvs(level: Level, samples=3, seed=1):
    rnd = random.Random(seed)
    r = Recorder(level, 64, 48)
    pvs = {}
    for ss in level.subsectors:
        seen = set()
        pts = subsector_points(level, ss, rnd, samples)
        # all subsector segs' own subsector is trivially visible
        seen.add(ss.idx)
        for (x, y) in pts:
            px, py = int(x * 65536), int(y * 65536)
            floor = ss.floor
            for z in (floor + VIEWHEIGHT, floor + 8, floor + 120):
                for a in range(0, ANGLES, ANGLES // 8):
                    r.seen = set()
                    r.render(px, py, a, z << FRAC, stats=True)
                    seen |= r.seen
        pvs[ss.idx] = seen
    return pvs


def dilate(level: Level, pvs):
    """Union PVS over BSP sibling subsectors (parent node's leaves) as a safety margin."""
    parent_leaves = {}
    for node in level.nodes:
        leaves = []
        for is_ss, idx in node.child:
            if is_ss:
                leaves.append(idx)
        for l in leaves:
            parent_leaves[l] = leaves
    out = {}
    for k, v in pvs.items():
        u = set(v)
        for sib in parent_leaves.get(k, []):
            u |= pvs[sib]
        out[k] = u
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    level = Level(json.load(open(args.level)))
    pvs = dilate(level, compute_pvs(level, args.samples))
    sizes = sorted(len(v) for v in pvs.values())
    print(f"subsectors {len(pvs)}, pvs size min {sizes[0]} median {sizes[len(sizes)//2]} max {sizes[-1]}")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({str(k): sorted(v) for k, v in pvs.items()}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
