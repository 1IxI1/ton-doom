#!/usr/bin/env python3
"""Shootable targets (imps) for Doom on TON, reference for contracts/Doom.tolk (must match bit for bit).

Monsters stand still. A monster record: x y z (map units; z = floor of its sector), hp (hits left),
state (0 idle, 1 pain, 2 dying, 3 dead), timer. Every frame: pain counts down to idle; dying counts up,
one death frame per DEATH_TICS frames, then dead (the corpse stays). A shot (fire input) hits the nearest
living monster whose centre is within HIT_RADIUS of the view axis and not behind a blocking line.

Rendering happens inside the BSP walk, when the monster's subsector is visited (front to back): the sprite
is drawn into the still-open rows of its columns and closes them, so nearer walls hide it and it hides
farther walls. If the viewer stands in that subsector the sprite is drawn before the subsector's segs
(which are all behind it), otherwise after them (its front-facing segs are all in front of it).

Storage (Doom.tolk Storage.monsters): count:4 then per monster x:int16 y:int16 z:int16 hp:uint8
state:uint4 timer:uint8 (68 bits).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boc import Cell, begin_cell  # noqa: E402
from render import FRAC, fdiv  # noqa: E402
from sprites import MIP_ROWS, MONSTER_REF_PX  # noqa: E402

# E1M1 targets: two straight ahead of the start, the rest along the hangar routes
MONSTERS = [(1056, -3440), (1150, -3300), (1056, -3050), (1400, -3350), (2300, -3300), (1700, -2900)]
HP = 6                 # pistol hits to kill
PAIN_FRAMES = 4
DEATH_TICS = 4         # frames per death animation frame
DEATH_FRAMES = 5       # sprite frames 2..6
HIT_RADIUS = 20        # map units either side of the view axis
HIT_RANGE = 2048
MIN_DEPTH = 16         # sprites closer than this are not drawn
MIN_ROWS = 5           # nor smaller than this (projected idle height in rows)
# level k is used when the projected idle height >= LEVEL_MIN_ROWS[k] (nearest level by ratio, 1.216 = sqrt(1.48))
LEVEL_MIN_ROWS = [max(1, -(-r * 1000 // 1216)) for r in MIP_ROWS]
IDLE, PAIN, DYING, DEAD = 0, 1, 2, 3


class Monster:
    __slots__ = ("x", "y", "z", "hp", "state", "timer")

    def __init__(self, x, y, z, hp=HP, state=IDLE, timer=0):
        self.x, self.y, self.z, self.hp, self.state, self.timer = x, y, z, hp, state, timer

    @property
    def alive(self):
        return self.state <= PAIN

    @property
    def frame(self):
        if self.state == IDLE:
            return 0
        if self.state == PAIN:
            return 1
        if self.state == DYING:
            return 2 + self.timer // DEATH_TICS
        return 6

    def tick(self):
        if self.state == PAIN:
            self.timer -= 1
            if self.timer == 0:
                self.state = IDLE
        elif self.state == DYING:
            self.timer += 1
            if self.timer >= DEATH_TICS * DEATH_FRAMES:
                self.state = DEAD
                self.timer = 0

    def hit(self):
        self.hp -= 1
        if self.hp == 0:
            self.state, self.timer = DYING, 0
        else:
            self.state, self.timer = PAIN, PAIN_FRAMES


def initial_monsters(level) -> list:
    out = []
    for x, y in MONSTERS:
        ss = level.point_in_subsector(x << FRAC, y << FRAC)
        out.append(Monster(x, y, ss.floor))
    return out


def monster_subsectors(level) -> dict:
    """subsector index -> list of monster ids standing in it."""
    d = {}
    for i, (x, y) in enumerate(MONSTERS):
        d.setdefault(level.point_in_subsector(x << FRAC, y << FRAC).idx, []).append(i)
    return d


def encode_monsters(monsters: list) -> Cell:
    b = begin_cell().store_uint(len(monsters), 4)
    for m in monsters:
        b.store_int(m.x, 16).store_int(m.y, 16).store_int(m.z, 16).store_uint(m.hp, 8).store_uint(m.state, 4).store_uint(m.timer, 8)
    return b.end_cell()


def view_of(px, py, sn, cs, x, y):
    """World integer point -> view space (s right, d forward), 16.16 (mirrors Renderer.to_view)."""
    dx = (x << FRAC) - px
    dy = (y << FRAC) - py
    return (dx * sn - dy * cs) >> FRAC, (dx * cs + dy * sn) >> FRAC


def shoot(level, monsters: list, px, py, sn, cs) -> int:
    """A shot along the view axis: returns the index of the monster hit (its state is updated) or -1."""
    best, best_d = -1, HIT_RANGE << FRAC
    for i, m in enumerate(monsters):
        if not m.alive:
            continue
        s, d = view_of(px, py, sn, cs, m.x, m.y)
        if d > 0 and abs(s) <= (HIT_RADIUS << FRAC) and d < best_d:
            best, best_d = i, d
    if best >= 0:
        m = monsters[best]
        if level.blockmap.blocked(px, py, m.x << FRAC, m.y << FRAC):
            return -1
        m.hit()
    return best


def pick_level(rows52: int) -> int:
    """Largest level whose minimum projected height is reached (the contract uses a nibble table, capped at 127)."""
    k = -1
    for i, r in enumerate(LEVEL_MIN_ROWS):
        if min(rows52, 127) >= r:
            k = i
    return k


def draw_monster(r, m: Monster, sprites) -> bool:
    """Draw monster m into renderer r's framebuffer (r.fb, r.solid). sprites[frame][level] as in
    sprites.monster_sprites. Returns True if anything could be drawn (mirrors Doom.tolk drawMonster)."""
    H, W, FOCAL, CX, CY = r.H, r.W, r.FOCAL, r.CX, r.CY
    FULL = r.FULL
    s, d = r.to_view(m.x, m.y)
    if d < (MIN_DEPTH << FRAC):
        return False
    rows52 = fdiv(MONSTER_REF_PX * r.ASPECT_Y * (FOCAL << FRAC), d)
    if rows52 < MIN_ROWS:
        return False
    level = pick_level(rows52)
    xc = r.project(s, d)
    scale24 = fdiv(FOCAL << 40, d)
    worldbottom = (m.z - (r.viewz >> FRAC)) * r.ASPECT_Y
    bot = ((CY << 24) - worldbottom * scale24) >> 24        # screen row of the feet
    rows, cols, xoff, columns = sprites[m.frame][level]
    if bot < 0 or bot - rows + 1 >= H:
        return False
    sh = H - 1 - bot
    sh1, sh2 = max(sh, 0), max(-sh, 0)
    x = xc - xoff
    for pix, mask in columns:
        if 0 <= x < W:
            # pixels are a subset of the mask and pixel bits of open rows are 0, so OR/AND NOT become +/-
            e = r.fb[x]
            open_ = e >> H
            r.fb[x] = e + (((pix << sh1) >> sh2) & open_) - ((((mask << sh1) >> sh2) & open_) << H)
        x += 1
    return True
