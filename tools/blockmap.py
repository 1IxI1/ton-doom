#!/usr/bin/env python3
"""Blocking-line blockmap for collisions (reference for contracts/Doom.tolk `blocked`).

Grid of BLOCK x BLOCK map units over the level's bounding box. Each block lists the linedefs that touch it
and can block movement: one-sided lines, and two-sided lines where crossing in some direction climbs more
than STEP_MAX or squeezes through a gap lower than MIN_GAP (doors are already opened in the level data).
Per line: x1 y1 x2 y2 (int16 each) and two flags: blockFront (crossing from the front/right side to the back
is blocked) and blockBack (the opposite crossing is blocked).

On-chain layout (level root ref2): a 4-ary tree of depth TREE_DEPTH keyed by block index (by * cols + bx,
10 bits, top bits first); a leaf cell holds count:uint4 then count x (x1:int16 y1:int16 x2:int16 y2:int16
blockFront:1 blockBack:1) = 66 bits, ref0 = continuation cell with more lines. Missing subtrees are shared
empty cells. The tree root also carries the grid: ox:int16 oy:int16 cols:uint8 rows:uint8.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boc import Cell, begin_cell  # noqa: E402

BLOCK_SHIFT = 7
BLOCK = 1 << BLOCK_SHIFT
STEP_MAX = 24
MIN_GAP = 56
LINES_PER_CELL = 15
TREE_DEPTH = 5           # 4^5 = 1024 >= cols * rows
FRAC = 16


class BlockLine:
    __slots__ = ("x1", "y1", "x2", "y2", "block_front", "block_back")

    def __init__(self, x1, y1, x2, y2, bf, bb):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.block_front, self.block_back = bf, bb


class Blockmap:
    def __init__(self, data: dict):
        verts, sectors = data["vertexes"], data["sectors"]
        xs = [v["x"] for v in verts]
        ys = [v["y"] for v in verts]
        self.ox = min(xs) - 8
        self.oy = min(ys) - 8
        self.cols = ((max(xs) - self.ox) >> BLOCK_SHIFT) + 1
        self.rows = ((max(ys) - self.oy) >> BLOCK_SHIFT) + 1
        assert self.cols * self.rows <= 4 ** TREE_DEPTH
        self.lines = []
        for ld in data["linedefs"]:
            v1, v2 = verts[ld["v1"]], verts[ld["v2"]]
            if ld["sector_left"] < 0:
                bf, bb = 1, 1
            else:
                a, b = sectors[ld["sector_right"]], sectors[ld["sector_left"]]   # front, back
                gap = min(a["ceiling"], b["ceiling"]) - max(a["floor"], b["floor"])
                bf = 1 if (b["floor"] - a["floor"] > STEP_MAX or gap < MIN_GAP) else 0
                bb = 1 if (a["floor"] - b["floor"] > STEP_MAX or gap < MIN_GAP) else 0
                if not bf and not bb:
                    continue
            self.lines.append(BlockLine(v1["x"], v1["y"], v2["x"], v2["y"], bf, bb))
        self.blocks = [[] for _ in range(self.cols * self.rows)]
        for li, ln in enumerate(self.lines):
            for b in self.line_blocks(ln):
                self.blocks[b].append(li)

    def block_of(self, x: int, y: int):
        return (x - self.ox) >> BLOCK_SHIFT, (y - self.oy) >> BLOCK_SHIFT

    def line_blocks(self, ln: BlockLine):
        """Blocks touched by a line: all blocks in its bbox whose square the segment intersects (conservative)."""
        bx1, by1 = self.block_of(min(ln.x1, ln.x2), min(ln.y1, ln.y2))
        bx2, by2 = self.block_of(max(ln.x1, ln.x2), max(ln.y1, ln.y2))
        out = []
        for by in range(by1, by2 + 1):
            for bx in range(bx1, bx2 + 1):
                x0 = self.ox + (bx << BLOCK_SHIFT)
                y0 = self.oy + (by << BLOCK_SHIFT)
                if seg_touches_box(ln.x1, ln.y1, ln.x2, ln.y2, x0, y0, x0 + BLOCK, y0 + BLOCK):
                    out.append(by * self.cols + bx)
        return out

    # ---- collision (mirrors the contract) ------------------------------------------------------ #
    def blocked(self, px: int, py: int, nx: int, ny: int) -> bool:
        """Move (px,py) -> (nx,ny), 16.16 coordinates. True if it crosses a blocking line."""
        x0, y0 = px >> FRAC, py >> FRAC
        x1, y1 = nx >> FRAC, ny >> FRAC
        bxa, bya = self.block_of(min(x0, x1), min(y0, y1))
        bxb, byb = self.block_of(max(x0, x1), max(y0, y1))
        bxa, bxb = max(bxa, 0), min(bxb, self.cols - 1)
        bya, byb = max(bya, 0), min(byb, self.rows - 1)
        mx, my = nx - px, ny - py
        seen = set()
        for by in range(bya, byb + 1):
            for bx in range(bxa, bxb + 1):
                for li in self.blocks[by * self.cols + bx]:
                    if li in seen:
                        continue
                    seen.add(li)
                    if line_blocks_move(self.lines[li], px, py, nx, ny, mx, my):
                        return True
        return False


def line_blocks_move(ln: BlockLine, px, py, nx, ny, mx, my) -> bool:
    dx = ln.x2 - ln.x1
    dy = ln.y2 - ln.y1
    c1 = dx * (py - (ln.y1 << FRAC)) - dy * (px - (ln.x1 << FRAC))   # start vs line (< 0: front side)
    c2 = dx * (ny - (ln.y1 << FRAC)) - dy * (nx - (ln.x1 << FRAC))   # target vs line
    if c1 == 0 or c2 == 0 or (c1 < 0) == (c2 < 0):
        return False    # no strict crossing
    e1 = mx * ((ln.y1 << FRAC) - py) - my * ((ln.x1 << FRAC) - px)   # endpoints vs move line
    e2 = mx * ((ln.y2 << FRAC) - py) - my * ((ln.x2 << FRAC) - px)
    if e1 == 0 or e2 == 0 or (e1 < 0) == (e2 < 0):
        return False
    return bool(ln.block_front) if c1 < 0 else bool(ln.block_back)


def seg_touches_box(x1, y1, x2, y2, bx0, by0, bx1, by1) -> bool:
    """Conservative segment/box test (Liang-Barsky on the closed box)."""
    t0, t1 = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for p, q in ((-dx, x1 - bx0), (dx, bx1 - x1), (-dy, y1 - by0), (dy, by1 - y1)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return t0 <= t1


# ---- cell encoding --------------------------------------------------------------------------- #
def encode_block(bm: Blockmap, idx: int) -> Cell:
    lines = [bm.lines[i] for i in bm.blocks[idx]]
    chunks = [lines[i : i + LINES_PER_CELL] for i in range(0, len(lines), LINES_PER_CELL)] or [[]]
    nxt = None
    for chunk in reversed(chunks):
        b = begin_cell().store_uint(len(chunk), 4)
        for ln in chunk:
            b.store_int(ln.x1, 16).store_int(ln.y1, 16).store_int(ln.x2, 16).store_int(ln.y2, 16)
            b.store_uint(ln.block_front, 1).store_uint(ln.block_back, 1)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
    return nxt


def encode_blockmap(bm: Blockmap) -> Cell:
    empty = begin_cell().store_uint(0, 4).end_cell()
    n = bm.cols * bm.rows
    leaves = [encode_block(bm, i) if i < n and bm.blocks[i] else empty for i in range(4 ** TREE_DEPTH)]
    level = leaves
    for _ in range(TREE_DEPTH):
        nxt = []
        for i in range(0, len(level), 4):
            b = begin_cell()
            for c in level[i : i + 4]:
                b.store_ref(c)
            nxt.append(b.end_cell())
        level = nxt
    tree = level[0]
    return (begin_cell().store_int(bm.ox, 16).store_int(bm.oy, 16).store_uint(bm.cols, 8).store_uint(bm.rows, 8)
            .store_ref(tree).end_cell())


if __name__ == "__main__":
    import json
    data = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "assets/e1m1.json"))
    from render import Level
    Level(data)  # opens doors in place
    bm = Blockmap(data)
    sizes = sorted(len(b) for b in bm.blocks)
    print(f"grid {bm.cols}x{bm.rows} origin ({bm.ox},{bm.oy}), blocking lines {len(bm.lines)}, "
          f"lines per block max {sizes[-1]} median {sizes[len(sizes)//2]}, empty {sizes.count(0)}")
