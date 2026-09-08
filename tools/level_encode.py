#!/usr/bin/env python3
"""Encode a Doom level (from tools/wad.py JSON) into the cell tree the Doom contract reads.

Layout (must match contracts/Doom.tolk):

  level root cell:
      bits: rootIsSubsector:1
      ref0: BSP root (node cell or subsector cell)
      ref1: sin table root

  node cell:
      bits: d1:3 d0:3 x:int16 y:int16 dx:int16 dy:int16   (d_s = (flags6 >> (3*s)) & 7)
            bbox0 (top,bottom,left,right: int16 x4) bbox1 (same)          = 198 bits
      d_s (for the viewer on side s): bit0 = child s is a subsector, bit1 = child 1-s is a subsector,
            bit2 = the far child (1-s) deserves a bbox test (subtree has >= BBOX_TEST_MIN_SS subsectors)
      ref0: child0 (right / front), ref1: child1 (left / back)

  subsector cell:
      bits: floor:int16 ceil:int16 nsegs:uint4, then nsegs seg records:
            kind:uint2 (0 solid, 1 upper wall only, 2 lower only, 3 both)
            x1:int16 y1:int16 x2:int16 y2:int16 ffloor:int16 fceil:int16
            lightAdj:int2 bfloor:int16 bceil:int16                            = 132 bits each
      ref0 (optional): continuation subsector cell with more segs (same layout, floor/ceil repeated)

  sin table: root -> up to 4 branch cells -> leaf cells with 56 x int18 entries (16.16 fixed point sin)

Usage: python3 tools/level_encode.py assets/e1m1.json assets/e1m1-level.boc
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boc import Cell, begin_cell  # noqa: E402
from render import ANGLES, SIN, SKIP, Level, Seg  # noqa: E402

SEGS_PER_CELL = 7
BBOX_TEST_MIN_SS = 4   # test the bbox of a far child only if its subtree has at least this many subsectors
SIN_PER_LEAF = 56
SIN_BITS = 18


def encode_seg(b, seg: Seg):
    b.store_uint(seg.code(), 2)
    b.store_int(seg.x1, 16).store_int(seg.y1, 16).store_int(seg.x2, 16).store_int(seg.y2, 16)
    b.store_int(seg.ffloor, 16).store_int(seg.fceil, 16)
    b.store_int(seg.light_adj, 2)
    b.store_int(seg.bfloor, 16).store_int(seg.bceil, 16)


def encode_subsector(ss) -> Cell:
    segs = [s for s in ss.segs if s.kind != SKIP]
    chunks = [segs[i : i + SEGS_PER_CELL] for i in range(0, len(segs), SEGS_PER_CELL)] or [[]]
    nxt = None
    for chunk in reversed(chunks):
        b = begin_cell().store_int(ss.floor, 16).store_int(ss.ceil, 16).store_uint(len(chunk), 4)
        for s in chunk:
            encode_seg(b, s)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
    return nxt


def subtree_size(level: Level, child) -> int:
    is_ss, idx = child
    if is_ss:
        return 1
    n = level.nodes[idx]
    return subtree_size(level, n.child[0]) + subtree_size(level, n.child[1])


def encode_node(level: Level, node, cache) -> Cell:
    children = []
    for is_ss, idx in node.child:
        children.append(encode_child(level, is_ss, idx, cache))
    b = begin_cell()
    is_ss = [1 if node.child[0][0] else 0, 1 if node.child[1][0] else 0]
    test = [1 if subtree_size(level, node.child[k]) >= BBOX_TEST_MIN_SS else 0 for k in (0, 1)]
    for side in (1, 0):   # d1 first (high bits), d0 last: the contract reads (flags >> (side*3)) & 7
        far = 1 - side
        b.store_uint(is_ss[side] | (is_ss[far] << 1) | (test[far] << 2), 3)
    b.store_int(node.x, 16).store_int(node.y, 16).store_int(node.dx, 16).store_int(node.dy, 16)
    for side in (0, 1):
        for v in node.bbox[side]:
            b.store_int(v, 16)
    b.store_ref(children[0]).store_ref(children[1])
    return b.end_cell()


def encode_child(level: Level, is_ss: bool, idx: int, cache) -> Cell:
    key = (is_ss, idx)
    if key in cache:
        return cache[key]
    c = encode_subsector(level.subsectors[idx]) if is_ss else encode_node(level, level.nodes[idx], cache)
    cache[key] = c
    return c


def encode_sin_table() -> Cell:
    leaves = []
    for i in range(0, ANGLES, SIN_PER_LEAF):
        b = begin_cell()
        for v in SIN[i : i + SIN_PER_LEAF]:
            b.store_int(v, SIN_BITS)
        leaves.append(b.end_cell())
    branches = []
    for i in range(0, len(leaves), 4):
        b = begin_cell()
        for leaf in leaves[i : i + 4]:
            b.store_ref(leaf)
        branches.append(b.end_cell())
    assert len(branches) <= 4
    root = begin_cell()
    for br in branches:
        root.store_ref(br)
    return root.end_cell()


def encode_level(level: Level) -> Cell:
    cache = {}
    is_ss, idx = level.root
    root_child = encode_child(level, is_ss, idx, cache)
    return begin_cell().store_uint(1 if is_ss else 0, 1).store_ref(root_child).store_ref(encode_sin_table()).end_cell()


def count_cells(c: Cell, seen=None) -> int:
    seen = seen if seen is not None else set()
    if c.hash() in seen:
        return 0
    seen.add(c.hash())
    return 1 + sum(count_cells(r, seen) for r in c.refs)


def main(argv=None):
    argv = argv or sys.argv[1:]
    src, dst = argv[0], argv[1]
    with open(src) as f:
        level = Level(json.load(f))
    root = encode_level(level)
    data = root.to_boc()
    with open(dst, "wb") as f:
        f.write(data)
    max_segs = max(len([s for s in ss.segs if s.kind != SKIP]) for ss in level.subsectors)
    kinds = [s.kind for s in level.segs]
    print(f"level cell: hash={root.hash().hex()} cells={count_cells(root)} boc_bytes={len(data)} depth={root.depth()}")
    print(f"segs: total={len(kinds)} solid={kinds.count(1)} portal={kinds.count(2)} skip={kinds.count(0)} max_per_subsector(non-skip)={max_segs}")
    print(f"player start: {level.player_start}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
