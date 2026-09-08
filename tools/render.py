#!/usr/bin/env python3
"""Reference 1-bit Doom wall renderer, integer-only, mirroring the TVM contract.

Every arithmetic step here is meant to be reproduced bit-exactly on-chain:
  * 257-bit integers, floor division (mulDivFloor semantics),
  * per-column framebuffer ints: low H bits = pixels (bit H-1-y is row y, so the
    MSB is the top row), next H bits = "opening" mask of rows still undrawn,
  * BSP traversal front-to-back with bbox culling against the FOV and a
    W-bit mask of fully closed columns.

Usage:
  python3 tools/render.py assets/e1m1.json --x 1056 --y -3616 --angle 128 --png out.png
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FRAC = 16
ONE = 1 << FRAC
ANGLES = 512  # angle units per full turn
VIEWHEIGHT = 41

SOLID, PORTAL, SKIP = 1, 2, 0
BBOX_TEST_MIN_SS = 4  # must match tools/level_encode.py
STEP_MIN = 16         # upper/lower wall pieces smaller than this (map units) are not drawn


def make_sin_table():
    """sin in 16.16 for ANGLES steps. Generated with floats but stored as ints (goes on-chain as data)."""
    return [int(round(math.sin(2 * math.pi * i / ANGLES) * ONE)) for i in range(ANGLES)]


SIN = make_sin_table()


def sin_a(a):
    return SIN[a % ANGLES]


def cos_a(a):
    return SIN[(a + ANGLES // 4) % ANGLES]


def fdiv(a, b):
    """Floor division as in TVM (rounds toward -inf)."""
    return a // b


# --------------------------------------------------------------------------- #
# Level model (what gets encoded into cells)
# --------------------------------------------------------------------------- #
class Seg:
    __slots__ = ("x1", "y1", "x2", "y2", "kind", "ffloor", "fceil", "flight", "fsky", "bfloor", "bceil", "idx",
                 "has_upper", "has_lower", "light_adj")

    def code(self) -> int:
        """2-bit kind code stored on-chain: 0 solid, 1 upper wall only, 2 lower only, 3 both."""
        if self.kind == SOLID:
            return 0
        return (1 if self.has_upper else 0) | (2 if self.has_lower else 0)

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Subsector:
    __slots__ = ("floor", "ceil", "light", "sky", "segs", "idx")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Node:
    __slots__ = ("x", "y", "dx", "dy", "bbox", "child", "idx")  # bbox[side] = (top, bottom, left, right); child[side] = (is_ss, idx)

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


DOOR_SPECIALS = {1, 26, 27, 28, 31, 32, 33, 34, 117, 118}


def open_doors(data: dict) -> int:
    """Permanently open door sectors (ceiling = lowest neighbour ceiling - 4, as Doom's open state)."""
    sectors = data["sectors"]
    lines = data["linedefs"]
    door_sectors = set()
    for ld in lines:
        if ld["special"] in DOOR_SPECIALS and ld["sector_left"] >= 0:
            door_sectors.add(ld["sector_left"])
    for ds in door_sectors:
        neigh = []
        for ld in lines:
            a, b = ld["sector_right"], ld["sector_left"]
            if a == ds and b >= 0:
                neigh.append(sectors[b]["ceiling"])
            elif b == ds and a >= 0:
                neigh.append(sectors[a]["ceiling"])
        if neigh:
            sectors[ds]["ceiling"] = min(neigh) - 4
    return len(door_sectors)


class Level:
    def __init__(self, data: dict, doors_open: bool = True):
        if doors_open:
            open_doors(data)
        sectors = data["sectors"]
        verts = data["vertexes"]
        self.sectors = sectors
        self.segs = []
        for i, s in enumerate(data["segs"]):
            v1, v2 = verts[s["v1"]], verts[s["v2"]]
            fs = sectors[s["front_sector"]]
            bs = sectors[s["back_sector"]] if s["back_sector"] >= 0 else None
            fsky = fs["ceiling_tex"] == "F_SKY1"
            if bs is None:
                kind = SOLID
                bfloor, bceil = 0, 0
            else:
                bsky = bs["ceiling_tex"] == "F_SKY1"
                if bs["ceiling"] <= fs["floor"] or bs["floor"] >= fs["ceiling"]:
                    kind = SOLID  # closed door
                    bfloor, bceil = 0, 0
                else:
                    bceil_eff = bs["ceiling"]
                    if fsky and bsky:
                        bceil_eff = fs["ceiling"]  # sky hack: no upper wall between two sky sectors
                    # effective back heights: upper wall only if back ceiling lower, lower wall only if back floor higher
                    bceil_eff = min(bceil_eff, fs["ceiling"])
                    bfloor_eff = max(bs["floor"], fs["floor"])
                    if fs["ceiling"] - bceil_eff <= STEP_MIN:
                        bceil_eff = fs["ceiling"]
                    if bfloor_eff - fs["floor"] <= STEP_MIN:
                        bfloor_eff = fs["floor"]
                    if bceil_eff == fs["ceiling"] and bfloor_eff == fs["floor"]:
                        kind = SKIP  # nothing to draw, nothing to clip
                        bfloor, bceil = 0, 0
                    else:
                        kind = PORTAL
                        bfloor, bceil = bfloor_eff, bceil_eff
            seg = Seg(x1=v1["x"], y1=v1["y"], x2=v2["x"], y2=v2["y"], kind=kind,
                      ffloor=fs["floor"], fceil=fs["ceiling"], flight=fs["light"], fsky=fsky,
                      bfloor=bfloor, bceil=bceil, idx=i)
            seg.has_upper = kind == PORTAL and bceil < fs["ceiling"]
            seg.has_lower = kind == PORTAL and bfloor > fs["floor"]
            seg.light_adj = max(-1, min(1, (fs["light"] - 160) // 48))
            self.segs.append(seg)
        self.subsectors = []
        for i, ss in enumerate(data["ssectors"]):
            sec = sectors[ss["sector"]]
            segs = [self.segs[j] for j in range(ss["firstseg"], ss["firstseg"] + ss["numsegs"])]
            self.subsectors.append(Subsector(floor=sec["floor"], ceil=sec["ceiling"], light=sec["light"],
                                             sky=sec["ceiling_tex"] == "F_SKY1", segs=segs, idx=i))
        self.nodes = []
        for i, n in enumerate(data["nodes"]):
            br, bl = n["bbox_right"], n["bbox_left"]
            self.nodes.append(Node(x=n["x"], y=n["y"], dx=n["dx"], dy=n["dy"],
                                   bbox=[(br["top"], br["bottom"], br["left"], br["right"]),
                                         (bl["top"], bl["bottom"], bl["left"], bl["right"])],
                                   child=[(n["child_right"]["is_subsector"], n["child_right"]["index"]),
                                          (n["child_left"]["is_subsector"], n["child_left"]["index"])],
                                   idx=i))
        self.root = (False, len(self.nodes) - 1)
        # bbox test policy: only test far children whose subtree has >= BBOX_TEST_MIN_SS subsectors
        self.test_far = {}
        for n in self.nodes:
            self.test_far[n.idx] = [self.subtree_size(n.child[0]) >= BBOX_TEST_MIN_SS,
                                    self.subtree_size(n.child[1]) >= BBOX_TEST_MIN_SS]
        p1 = [t for t in data["things"] if t["type"] == 1][0]
        self.player_start = (p1["x"], p1["y"], p1["angle"])

    def subtree_size(self, child) -> int:
        is_ss, idx = child
        if is_ss:
            return 1
        n = self.nodes[idx]
        return self.subtree_size(n.child[0]) + self.subtree_size(n.child[1])

    # Doom R_PointOnSide. px, py in 16.16. Returns 0 (front/right child) or 1 (back/left child).
    def point_on_side(self, px, py, node: Node) -> int:
        dx = px - (node.x << FRAC)
        dy = py - (node.y << FRAC)
        left = node.dy * dx
        right = dy * node.dx
        return 0 if right < left else 1

    def point_in_subsector(self, px, py) -> Subsector:
        is_ss, idx = self.root
        while not is_ss:
            node = self.nodes[idx]
            is_ss, idx = node.child[self.point_on_side(px, py, node)]
        return self.subsectors[idx]


# --------------------------------------------------------------------------- #
# Dither patterns
# --------------------------------------------------------------------------- #
# Ordered 4x4 Bayer matrix; shade level L in 0..16 = number of lit cells of 16.
BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def column_pattern(level: int, phase: int, H: int) -> int:
    """H-bit column pattern for shade `level` (0..16) at column phase (x & 3). Row y is bit H-1-y."""
    v = 0
    for y in range(H):
        if BAYER[y & 3][phase & 3] < level:
            v |= 1 << (H - 1 - y)
    return v


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #
class Renderer:
    def __init__(self, level: Level, W: int = 128, H: int = 96):
        self.level = level
        self.W, self.H = W, H
        self.CX = W // 2
        self.CY = H // 2
        self.FOCAL = W // 2  # 90 degree horizontal FOV
        self.FULL = (1 << H) - 1
        self.FULLW = (1 << W) - 1
        # wall shade levels by depth (map units); mirrors contracts/Doom.tolk
        self.shade_depths = [96, 192, 384, 768]
        REP = self.FULL // 15                      # one nibble per 4 rows
        FULL = self.FULL
        # pattern levels: (even column, odd column)
        self.PAT = {
            0: (0, 0),
            1: (0b1000 * REP, 0),                  # 12.5 %  sky
            2: (0b1010 * REP, 0),                  # 25 %    floor, far walls
            3: (0b1010 * REP, 0b0101 * REP),       # 50 %
            4: (0b1010 * REP, 0b1111 * REP),       # 75 %
            5: (FULL, FULL),                       # 100 %
        }
        self.floor_pat = self.PAT[2]
        self.ceil_pat = self.PAT[0]
        self.sky_pat = self.PAT[1]
        # depth level 0 (near) .. 4 (far) -> pattern level
        self.wall_pats = [self.PAT[5], self.PAT[4], self.PAT[3], self.PAT[2], self.PAT[2]]
        self.stats = None

    # ---- helpers ------------------------------------------------------------ #
    def rowmask_top(self, n: int) -> int:
        """Mask of the top n rows (rows 0..n-1), n clamped to [0, H]."""
        n = max(0, min(self.H, n))
        return self.FULL ^ ((1 << (self.H - n)) - 1)

    def colmask(self, x1: int, x2: int) -> int:
        """Mask of columns x1..x2-1 (W-bit int, bit x = column x)."""
        return ((1 << (x2 - x1)) - 1) << x1

    def project(self, s: int, d: int) -> int:
        """Screen column of a view-space point (s right, d forward, both 16.16, d > 0)."""
        # x = CX + s*FOCAL/d, rounded to nearest column boundary
        return (((self.CX << FRAC) + fdiv(s * self.FOCAL * ONE, d)) + (ONE >> 1)) >> FRAC

    # ---- frame ------------------------------------------------------------- #
    def render(self, px: int, py: int, angle: int, viewz: int, stats: bool = False):
        """px, py, viewz in 16.16; angle in ANGLES units. Returns list of W column ints (pixels only)."""
        self.px, self.py, self.viewz = px, py, viewz
        self.sin = sin_a(angle)
        self.cos = cos_a(angle)
        H = self.H
        self.fb = [self.FULL << H] * self.W  # pixels 0, opening = all rows
        self.solid = 0
        self.stats = {"nodes": 0, "bbox_tests": 0, "bbox_culled": 0, "subsectors": 0, "segs": 0,
                      "segs_backface": 0, "segs_offscreen": 0, "segs_occluded": 0, "segs_drawn": 0,
                      "runs": 0, "col_events": 0} if stats else None
        self.render_node(self.level.root)
        return [c & self.FULL for c in self.fb]

    def render_node(self, child):
        is_ss, idx = child
        if is_ss:
            self.render_subsector(self.level.subsectors[idx])
            return
        if self.stats is not None:
            self.stats["nodes"] += 1
        node = self.level.nodes[idx]
        side = self.level.point_on_side(self.px, self.py, node)
        self.render_node(node.child[side])
        if self.solid == self.FULLW:
            return
        if (not self.level.test_far[idx][side ^ 1]) or self.check_bbox(node.bbox[side ^ 1]):
            self.render_node(node.child[side ^ 1])

    # Doom checkcoord table: for each viewer region (boxy*4+boxx) the two extreme corners
    # as (x-index, y-index) pairs into bbox tuple (top=0, bottom=1, left=2, right=3).
    # packed Doom checkcoord table: nibble per boxpos = x1sel:1 y1sel:1 x2sel:1 y2sel:1 (1 = right / bottom)
    CHECKCOORD = 9 | (8 << 4) | (12 << 8) | (1 << 16) | (14 << 24) | (3 << 32) | (7 << 36) | (6 << 40)

    def to_view(self, x: int, y: int):
        """World integer coords -> view-space (s, d) in 16.16."""
        dx = (x << FRAC) - self.px
        dy = (y << FRAC) - self.py
        s = (dx * self.sin - dy * self.cos) >> FRAC
        d = (dx * self.cos + dy * self.sin) >> FRAC
        return s, d

    def check_bbox(self, bbox) -> bool:
        top, bottom, left, right = bbox
        if self.stats is not None:
            self.stats["bbox_tests"] += 1
        vx, vy = self.px >> FRAC, self.py >> FRAC
        boxx = 0 if vx <= left else (1 if vx < right else 2)
        boxy = 0 if vy >= top else (1 if vy > bottom else 2)
        boxpos = boxy * 4 + boxx
        if boxpos == 5:
            return True
        sel = (self.CHECKCOORD >> (boxpos * 4)) & 15
        s1, d1 = self.to_view(right if sel & 8 else left, bottom if sel & 4 else top)
        s2, d2 = self.to_view(right if sel & 2 else left, bottom if sel & 1 else top)
        # arc from corner1 clockwise to corner2 must span < 180 degrees, else "on the line": visible
        if s1 * d2 - d1 * s2 > 0:
            return True
        in1 = (s1 + d1 >= 0) and (s1 <= d1)  # corner1 inside FOV
        in2 = (s2 + d2 >= 0) and (s2 <= d2)
        if not in1 and not in2:
            # FOV edge L=(-1,1) inside arc?  cross(c1,L)<=0: s1+d1<=0 ; cross(L,c2)<=0: s2+d2>=0
            l_in = (s1 + d1 <= 0) and (s2 + d2 >= 0)
            r_in = (s1 <= d1) and (s2 >= d2)
            if not l_in and not r_in:
                if self.stats is not None:
                    self.stats["bbox_culled"] += 1
                return False
        x1 = self.project(s1, d1) if in1 else 0
        x2 = self.project(s2, d2) if in2 else self.W
        if x1 >= x2:
            return False
        if self.colmask(x1, x2) & ~self.solid == 0:
            if self.stats is not None:
                self.stats["bbox_culled"] += 1
            return False
        return True

    def render_subsector(self, ss: Subsector):
        if self.stats is not None:
            self.stats["subsectors"] += 1
        for seg in ss.segs:
            if seg.kind != SKIP:
                self.add_seg(seg)

    def add_seg(self, seg: Seg):
        st = self.stats
        if st is not None:
            st["segs"] += 1
        s1, d1 = self.to_view(seg.x1, seg.y1)
        s2, d2 = self.to_view(seg.x2, seg.y2)
        C = s1 * d2 - d1 * s2
        if C >= 0:  # back-facing or edge-on
            if st is not None:
                st["segs_backface"] += 1
            return
        if d1 <= 0 and d2 <= 0:
            if st is not None:
                st["segs_offscreen"] += 1
            return
        # column range
        if d1 <= 0 or s1 + d1 < 0:
            x1 = 0
        elif s1 > d1:
            if st is not None:
                st["segs_offscreen"] += 1
            return
        else:
            x1 = self.project(s1, d1)
        if d2 <= 0 or s2 > d2:
            x2 = self.W
        elif s2 + d2 < 0:
            if st is not None:
                st["segs_offscreen"] += 1
            return
        else:
            x2 = self.project(s2, d2)
        if x1 >= x2:
            if st is not None:
                st["segs_offscreen"] += 1
            return
        cols = self.colmask(x1, x2) & ~self.solid
        if cols == 0:
            if st is not None:
                st["segs_occluded"] += 1
            return
        if st is not None:
            st["segs_drawn"] += 1

        H = self.H
        ds = s2 - s1
        dd = d2 - d1
        negC = -C
        # scale24 at column i: (numBase - 2*i*dd) * 2^39 / negC ;  numBase = 2*FOCAL*ds + (2*CX - 1)*dd
        numBase = 2 * self.FOCAL * ds + (2 * self.CX - 1) * dd
        dd2 = 2 * dd
        step24 = fdiv(dd << 40, C)
        vz = self.viewz >> FRAC
        worldtop = seg.fceil - vz
        worldbottom = seg.ffloor - vz
        CY24 = self.CY << 24
        topstep = -worldtop * step24
        botstep = -worldbottom * step24
        variant = "solid"
        if seg.kind == PORTAL:
            variant = {(True, False): "up", (False, True): "lo", (True, True): "both"}[(seg.has_upper, seg.has_lower)]
        worldhigh = seg.bceil - vz
        worldlow = seg.bfloor - vz
        highstep = -worldhigh * step24
        lowstep = -worldlow * step24
        # shade: depth (16.16) at the middle visible column; level 0 (near) .. 4 (far)
        xm = (x1 + x2) >> 1
        num = numBase - xm * dd2
        depth16 = fdiv(2 * self.FOCAL * negC, num)
        lvl = min(4, (fdiv(depth16, self.shade_depths[0] << FRAC)).bit_length())
        lvl = max(0, min(4, lvl - seg.light_adj))
        wall_pats = self.wall_pats[lvl]
        floor_pats = self.floor_pat
        fb = self.fb

        while cols:
            lo_bit = cols & -cols
            lo = lo_bit.bit_length() - 1
            t = cols >> lo
            run = ((t + 1) & ~t).bit_length() - 1
            cols &= ~(((1 << run) - 1) << lo)
            if st is not None:
                st["runs"] += 1
                st["col_events"] += run
            scale24 = fdiv((numBase - lo * dd2) << 39, negC)
            top24 = CY24 - worldtop * scale24
            bot24 = CY24 - worldbottom * scale24
            high24 = CY24 - worldhigh * scale24
            low24 = CY24 - worldlow * scale24
            odd = lo & 1
            wA, wB = (wall_pats[1], wall_pats[0]) if odd else (wall_pats[0], wall_pats[1])
            fA, fB = (floor_pats[1], floor_pats[0]) if odd else (floor_pats[0], floor_pats[1])
            fill_run(variant, H, fb, lo, run, top24, bot24, high24, low24, topstep, botstep, highstep, lowstep,
                     wA, wB, fA, fB)
            if variant == "solid":
                self.solid |= ((1 << run) - 1) << lo


def rows_top(H: int, n: int) -> int:
    """Mask of the top n rows (n clamped to [0, H]); row y is bit H-1-y."""
    FULL = (1 << H) - 1
    n = max(0, min(H, n))
    return FULL ^ ((1 << (H - n)) - 1)


def fill_run(variant, H, fb, x, run, top, bot, high, low, ts, bs, hs, ls, wA, wB, fA, fB):
    """Reference semantics of the asm loops (fillSolidRun / fillUpperRun / fillLowerRun / fillBothRun).
    Mutates fb in place. Ceilings are black (no pattern)."""
    FULL = (1 << H) - 1
    for _ in range(run):
        e = fb[x]
        M = e >> H
        A = rows_top(H, (top + (1 << 24) - 1) >> 24)
        B = rows_top(H, (bot >> 24) + 1)
        Ahi = rows_top(H, (high >> 24) + 1) if variant in ("up", "both") else A
        Blo = rows_top(H, (low + (1 << 24) - 1) >> 24) if variant in ("lo", "both") else B
        wallrows = (A ^ B) if variant == "solid" else ((Ahi ^ A) | (B ^ Blo))
        pix = (e | (wA & (M & wallrows)) | (fA & (M ^ (M & B)))) & FULL
        if variant != "solid":
            pix |= (M & Blo & ~Ahi) << H
        fb[x] = pix
        wA, wB = wB, wA
        fA, fB = fB, fA
        top += ts
        bot += bs
        high += hs
        low += ls
        x += 1
    return fb


# --------------------------------------------------------------------------- #
# Player / camera
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self, level: Level, x: int, y: int, angle_deg: int):
        self.level = level
        self.x = x << FRAC
        self.y = y << FRAC
        self.angle = (angle_deg * ANGLES) // 360

    def viewz(self) -> int:
        ss = self.level.point_in_subsector(self.x, self.y)
        return (ss.floor + VIEWHEIGHT) << FRAC

    def move(self, turn: int, forward: int, strafe: int, speed_fwd: int = 8, speed_side: int = 6):
        """turn in ANGLES units, forward/strafe in -1..1 * units; noclip movement."""
        self.angle = (self.angle + turn) % ANGLES
        c, s = cos_a(self.angle), sin_a(self.angle)
        # forward vector (c, s); right vector (s, -c)
        self.x += forward * speed_fwd * c + strafe * speed_side * s
        self.y += forward * speed_fwd * s - strafe * speed_side * c


def load_level(path: str) -> Level:
    with open(path) as f:
        return Level(json.load(f))


def pack_frame(columns, W: int, H: int) -> bytes:
    """Frame bytes exactly as the on-chain message packs them: W columns of H bits, MSB first."""
    v = 0
    for c in columns:
        v = (v << H) | c
    nbits = W * H
    return v.to_bytes((nbits + 7) // 8, "big") if nbits % 8 == 0 else (v << (8 - nbits % 8)).to_bytes((nbits + 7) // 8, "big")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("level")
    ap.add_argument("--x", type=int)
    ap.add_argument("--y", type=int)
    ap.add_argument("--angle", type=int, help="angle in ANGLES units (0..511, 128 = north)")
    ap.add_argument("--angle-deg", type=int)
    ap.add_argument("--W", type=int, default=128)
    ap.add_argument("--H", type=int, default=96)
    ap.add_argument("--png")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--ascii", action="store_true")
    args = ap.parse_args(argv)

    level = load_level(args.level)
    sx, sy, sa = level.player_start
    x = args.x if args.x is not None else sx
    y = args.y if args.y is not None else sy
    angle = args.angle if args.angle is not None else (args.angle_deg if args.angle_deg is not None else sa) * ANGLES // 360
    player = Player(level, x, y, 0)
    player.angle = angle % ANGLES
    r = Renderer(level, args.W, args.H)
    cols = r.render(player.x, player.y, player.angle, player.viewz(), stats=args.stats)
    if args.stats:
        print(json.dumps(r.stats))
    if args.png:
        from png import write_bitmap_png
        write_bitmap_png(args.png, cols, args.W, args.H, args.scale)
    if args.ascii:
        for yy in range(0, args.H, 2):
            print("".join("#" if (cols[xx] >> (args.H - 1 - yy)) & 1 else "." for xx in range(args.W)))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------- #
# Cell packing (must match contracts/Doom.tolk renderFrame)
# --------------------------------------------------------------------------- #
def frame_cell_chain(columns, W: int, H: int):
    """Chain of cells: COLS_PER_CELL columns of H bits each, next cell in ref 0."""
    from boc import begin_cell

    cols_per_cell = 1023 // H
    nxt = None
    x = W
    while x > 0:
        start = max(x - cols_per_cell, 0)
        b = begin_cell()
        for i in range(start, x):
            b.store_uint(columns[i], H)
        if nxt is not None:
            b.store_ref(nxt)
        nxt = b.end_cell()
        x = start
    return nxt
