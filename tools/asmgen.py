#!/usr/bin/env python3
"""Generates the hand-written TVM asm column loops for contracts/Doom.tolk.

A tiny symbolic stack tracker turns named-variable code into PUSH s(i)/XCHG sequences, and
tools/stacksim.py verifies every generated loop against the reference formulas before printing.

Usage: python3 tools/asmgen.py [H] > /dev/stdout   (prints the Tolk asm functions)
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stacksim import Stack, run  # noqa: E402


class Asm:
    """Symbolic stack: self.stack is a list of names, bottom..top."""

    def __init__(self, names):
        self.stack = list(names)
        self.code = []
        self.tmp = 0

    def emit(self, s):
        self.code.append(s)

    def idx(self, name):
        return len(self.stack) - 1 - self.stack.index(name)

    def newtmp(self):
        self.tmp += 1
        return f"t{self.tmp}"

    def push(self, name):
        i = self.idx(name)
        self.emit(f"s{i} PUSH" if i < 16 else f"{i} s() PUSH")
        t = self.newtmp()
        self.stack.append(t)
        return t

    def push2(self, a, b):
        i = self.idx(a)
        j = self.idx(b)  # index before any push; PUSH2 s(i) s(j) = PUSH s(i); PUSH s(j+1)
        if i < 16 and j < 16:
            self.emit(f"s{i} s{j} PUSH2")
        else:
            self.emit(f"s{i} PUSH" if i < 16 else f"{i} s() PUSH")
            j2 = j + 1
            self.emit(f"s{j2} PUSH" if j2 < 16 else f"{j2} s() PUSH")
        t1, t2 = self.newtmp(), self.newtmp()
        self.stack += [t1, t2]
        return t1, t2

    def binop(self, op):
        self.stack.pop()
        self.stack.pop()
        self.emit(op)
        t = self.newtmp()
        self.stack.append(t)
        return t

    def ternop(self, op):
        self.stack.pop(); self.stack.pop(); self.stack.pop()
        self.emit(op)
        t = self.newtmp()
        self.stack.append(t)
        return t

    def unop(self, op):
        self.stack.pop()
        self.emit(op)
        t = self.newtmp()
        self.stack.append(t)
        return t

    def const(self, op):
        self.emit(op)
        t = self.newtmp()
        self.stack.append(t)
        return t

    def rename_top(self, name):
        self.stack[-1] = name

    def xchg(self, a, b):
        i, j = self.idx(a), self.idx(b)
        if i == j:
            return
        if i > j:
            i, j = j, i
        self.emit(f"s{i} s{j} XCHG" if j < 16 else f"s{i} {j} s() XCHG")
        ia, ib = self.stack.index(a), self.stack.index(b)
        self.stack[ia], self.stack[ib] = self.stack[ib], self.stack[ia]

    def pop_into(self, name):
        """POP s(i): store the top into the slot named `name` (the top is removed)."""
        i = self.idx(name)  # index before popping; POP s(i) refers to the slot below the top
        assert i >= 1
        self.emit(f"s{i} POP" if i < 16 else f"{i} s() POP")
        top = self.stack.pop()
        self.stack[self.stack.index(name)] = top
        self.stack[self.stack.index(top)] = name

    def drop_top(self):
        self.stack.pop()
        self.emit("DROP")

    def discard(self, name):
        """Drop the named entry wherever it is (swap it to the top first)."""
        top = self.stack[-1]
        if top != name:
            self.xchg(name, top)
        self.drop_top()

    def blkdrop_top(self, n):
        for _ in range(n):
            self.stack.pop()
        self.emit(f"{n} BLKDROP")


def mask_rows_above(a: Asm, src: str, mode, H: int):
    """Push mask of rows above boundary n: mode True/"ceil": n = ceil(src/2^24); False/"floor1": floor(src/2^24)+1;
    "shift": n = src >> 24 (the caller pre-added 2^24-1 for a ceil).
    Uses the frame register `rows` (tuple: rows[n] = mask of the top n rows, n in 0..H)."""
    a.push(src)
    if mode is True or mode == "ceil":
        a.const("24 PUSHPOW2DEC")
        a.binop("ADD")
        a.unop("24 RSHIFT#")
    elif mode == "shift":
        a.unop("24 RSHIFT#")
    else:
        a.unop("24 RSHIFT#")
        a.unop("INC")
    a.const("0 PUSHINT")
    a.binop("MAX")                    # n' = max(n, 0)
    a.const(f"{H} PUSHINT")
    a.binop("MIN")                    # n'' = min(n', H)
    a.push("rows")
    a.emit("SWAP")
    a.stack[-1], a.stack[-2] = a.stack[-2], a.stack[-1]
    return a.binop("INDEXVAR")        # rows[n'']


def gen_column(a: Asm, variant: str, H: int, wall: str, floor: str, preoffset: bool = True):
    """One column: reads fb[x], writes it back, advances x (x must be on top of the stack).
    Boundary names: top, bot, high, low (top and low pre-offset by 2^24-1 when preoffset). Reads rows, wA.., fA.."""
    a.push2("fb", "x")
    a.binop("INDEXVAR"); a.rename_top("e")
    a.emit("DUP"); a.stack.append("M0")
    a.unop(f"{H} RSHIFT#"); a.rename_top("M")
    ceilmode = "shift" if preoffset else "ceil"
    mask_rows_above(a, "top", ceilmode, H); a.rename_top("A")
    mask_rows_above(a, "bot", "floor1", H); a.rename_top("B")
    if variant in ("up", "both"):
        mask_rows_above(a, "high", "floor1", H); a.rename_top("Ahi")
    if variant in ("lo", "both"):
        mask_rows_above(a, "low", ceilmode, H); a.rename_top("Blo")
    upper = "Ahi" if variant in ("up", "both") else "A"
    lower = "Blo" if variant in ("lo", "both") else "B"
    if variant == "solid":
        a.push2("A", "B"); a.binop("XOR")
    elif variant == "up":
        a.push2("Ahi", "A"); a.binop("XOR")
    elif variant == "lo":
        a.push2("B", "Blo"); a.binop("XOR")
    else:
        a.push2("Ahi", "A"); a.binop("XOR")
        a.push2("B", "Blo"); a.binop("XOR")
        a.binop("OR")
    a.push("M"); a.binop("AND")
    a.push(wall); a.binop("AND"); a.rename_top("wp")
    a.push("B"); a.unop("NOT")
    a.push("M"); a.binop("AND")
    a.push(floor); a.binop("AND")
    a.binop("OR")
    a.push("e"); a.binop("OR")
    a.const(f"{H} PUSHPOW2DEC"); a.binop("AND"); a.rename_top("pix")
    if variant != "solid":
        a.push2("M", lower); a.binop("AND")
        a.push(upper); a.unop("NOT"); a.binop("AND")
        a.unop(f"{H} LSHIFT#")
        a.binop("OR"); a.rename_top("pix")
    # drop temporaries between x and pix
    ix = a.stack.index("x")
    n_tmp = len(a.stack) - 1 - ix - 1
    if n_tmp:
        a.emit(f"s0 s{n_tmp} XCHG")
        a.stack[ix + 1], a.stack[-1] = a.stack[-1], a.stack[ix + 1]
        a.blkdrop_top(n_tmp)
    # [.. fb .. x pix] -> fb[x] = pix ; x += 1
    a.push("x"); a.rename_top("xc")
    a.xchg("fb", "xc")                 # [.. xc .. x pix fb]
    a.emit("SWAP"); a.stack[-1], a.stack[-2] = a.stack[-2], a.stack[-1]   # [.. x fb pix]
    a.push("xc")                       # [.. x fb pix xc']
    a.emit("SETINDEXVAR")
    a.stack.pop(); a.stack.pop(); a.stack.pop(); a.stack.append("fbn")
    a.xchg("fbn", "xc")                # fb' back into its slot, xc on top
    a.stack[a.stack.index("fbn")] = "fb"
    a.drop_top()                       # drop xc -> x on top
    a.emit("INC")
    # steps
    for b, st in (("top", "ts"), ("bot", "bs"), ("high", "hs"), ("low", "ls")):
        if b not in a.stack:
            continue
        a.push2(b, st); a.binop("ADD")
        a.pop_into(b)


def gen_runs(a: Asm, variant: str, H: int, W: int, budget: int, col_cost: int):
    """Run loop over the set bits of `cols`. Frame must contain: rows wA wB fA fB ts bs [hs ls] wt wb [wh wl]
    numBase dd2 negC fb cols solidAdd partial. Leaves the frame unchanged (cols = 0 at the end)."""
    CY24 = (H // 2) << 24
    R = (1 << 24) - 1
    worlds = {"solid": ["wt", "wb"], "up": ["wt", "wb", "wh"], "lo": ["wt", "wb", "wl"], "both": ["wt", "wb", "wh", "wl"]}[variant]
    bounds = {"wt": "top", "wb": "bot", "wh": "high", "wl": "low"}
    frame = list(a.stack)
    a.code.append("WHILE:<{")
    a.push("cols"); a.emit("0 NEQINT"); a.stack.pop(); a.stack.append("cond")
    a.stack.pop()
    a.code.append("}>DO<{")
    a.push("cols"); a.emit("DUP NEGATE AND"); a.stack.pop(); a.stack.append("loBit")
    a.unop("UBITSIZE"); a.unop("DEC"); a.rename_top("lo")
    a.push("cols"); a.push("lo"); a.binop("RSHIFT"); a.rename_top("t")
    a.emit("DUP INC SWAP NOT AND"); a.stack.pop(); a.stack.append("t2")
    a.unop("UBITSIZE"); a.unop("DEC"); a.rename_top("run")
    a.push("run"); a.emit("POW2 DEC"); a.stack.pop(); a.stack.append("m1")
    a.push("lo"); a.binop("LSHIFT"); a.unop("NOT")
    a.push("cols"); a.binop("AND"); a.pop_into("cols")
    a.emit("GASCONSUMED"); a.stack.append("g")
    a.push("run"); a.unop(f"{col_cost} MULCONST"); a.binop("ADD")
    a.emit(f"{budget} PUSHINT GREATER"); a.stack.pop(); a.stack.append("over")
    a.stack.pop()
    before = list(a.stack)
    a.code.append("IF:<{")
    a.const("-1 PUSHINT"); a.pop_into("partial")
    a.const("0 PUSHINT"); a.pop_into("cols")
    a.drop_top(); a.drop_top()
    assert a.stack == frame, (a.stack, frame)
    a.code.append("}>ELSE<{")
    a.stack = list(before)
    a.push2("lo", "dd2"); a.binop("MUL")
    a.push("numBase"); a.binop("SUB"); a.emit("NEGATE"); a.stack.pop(); a.stack.append("num")
    a.unop("39 LSHIFT#")
    a.push("negC"); a.binop("DIV"); a.rename_top("scale")
    for wname in worlds:
        b = bounds[wname]
        a.push2(wname, "scale"); a.binop("MUL")
        a.const(f"{CY24 + (R if b in ('top', 'low') else 0)} PUSHINT")
        a.emit("SWAP SUB"); a.stack.pop(); a.stack.pop(); a.stack.append(b)
    a.xchg("scale", bounds[worlds[-1]])
    a.drop_top()
    a.push("lo"); a.rename_top("x")
    a.push("lo"); a.emit("1 PUSHINT AND"); a.stack.pop(); a.stack.append("odd")
    a.stack.pop()
    pre = list(a.stack)
    a.code.append("IF:<{")
    a.xchg("wA", "wB"); a.xchg("fA", "fB")
    a.stack = list(pre)
    a.code.append("}>")
    a.push("run"); a.unop("1 RSHIFT#")
    a.stack.pop()
    a.code.append("REPEAT:<{")
    gen_column(a, variant, H, "wA", "fA")
    gen_column(a, variant, H, "wB", "fB")
    assert a.stack == pre, (a.stack, pre)
    a.code.append("}>")
    a.push("run"); a.emit("1 PUSHINT AND"); a.stack.pop()
    a.code.append("IF:<{")
    gen_column(a, variant, H, "wA", "fA")
    assert a.stack == pre
    a.code.append("}>")
    a.push("lo"); a.emit("1 PUSHINT AND"); a.stack.pop()
    a.code.append("IF:<{")
    a.xchg("wA", "wB"); a.xchg("fA", "fB")
    a.stack = list(pre)
    a.code.append("}>")
    a.drop_top()
    for _ in worlds:
        a.drop_top()
    if variant == "solid":
        a.push("run"); a.emit("POW2 DEC"); a.stack.pop(); a.stack.append("m")
        a.push("lo"); a.binop("LSHIFT")
        a.push("solidAdd"); a.binop("OR"); a.pop_into("solidAdd")
    a.drop_top(); a.drop_top()
    assert a.stack == frame, (a.stack, frame)
    a.code.append("}>")
    a.code.append("}>")


def gen_seg(variant: str, H: int, W: int, budget: int = 950000, col_cost: int = 2600):
    """Seg drawer given precomputed projection/setup values (kept for tests)."""
    steps = {"solid": ["ts", "bs"], "up": ["ts", "bs", "hs"], "lo": ["ts", "bs", "ls"], "both": ["ts", "bs", "hs", "ls"]}[variant]
    worlds = {"solid": ["wt", "wb"], "up": ["wt", "wb", "wh"], "lo": ["wt", "wb", "wl"], "both": ["wt", "wb", "wh", "wl"]}[variant]
    params = ["rows", "w01", "f01"] + steps + worlds + ["numBase", "dd2", "negC", "fb", "cols"]
    a = Asm(params)
    a.push("w01"); a.unop(f"{H} RSHIFT#"); a.rename_top("wB")
    a.push("w01"); a.const(f"{H} PUSHPOW2DEC"); a.binop("AND"); a.rename_top("wA")
    a.push("f01"); a.unop(f"{H} RSHIFT#"); a.rename_top("fB")
    a.push("f01"); a.const(f"{H} PUSHPOW2DEC"); a.binop("AND"); a.rename_top("fA")
    a.const("0 PUSHINT"); a.rename_top("solidAdd")
    a.const("0 PUSHINT"); a.rename_top("partial")
    gen_runs(a, variant, H, W, budget, col_cost)
    a.xchg("fb", "fA")
    n_junk = len(a.stack) - 3
    while n_junk > 0:
        k = min(15, n_junk)
        a.emit(f"{k} 3 BLKDROP2")
        n_junk -= k
    a.stack = ["fb", "solidAdd", "partial"]
    return "\n".join(a.code), params


def gen_seg_full(variant: str, H: int, W: int, budget: int = 950000, col_cost: int = 2600, shade_unit: int = 96 << 16):
    """Full seg: view transform, clipping/projection, setup, shading, run loop, column loop.
    Params (15): rows pats f01 cam solid x1w y1w x2w y2w ffloor fceil lightAdj bfloor bceil fb
      cam = tuple [px py sn cs vz]; pats = tuple of 10 wall patterns (lvl*2 even, lvl*2+1 odd)
    Returns (fb, solidAdd, partial). Never throws (divisions guarded); culled segs leave fb untouched."""
    CX = W // 2
    FOCAL = W // 2
    params = ["rows", "pats", "f01", "cam", "solid", "x1w", "y1w", "x2w", "y2w", "ffloor", "fceil", "lightAdj", "bfloor", "bceil", "fb"]
    a = Asm(params)
    # camera fields
    for k, name in enumerate(["px", "py", "sn", "cs", "vz"]):
        a.push("cam"); a.emit(f"{k} INDEX"); a.rename_top(name)
    # view transform
    def to_view(xw, yw, sname, dname):
        a.push(xw); a.unop("16 LSHIFT#"); a.push("px"); a.binop("SUB"); a.rename_top("dx")
        a.push(yw); a.unop("16 LSHIFT#"); a.push("py"); a.binop("SUB"); a.rename_top("dy")
        a.push2("dx", "sn"); a.binop("MUL")
        a.push2("dy", "cs"); a.binop("MUL"); a.binop("SUB"); a.unop("16 RSHIFT#"); a.rename_top(sname)
        a.push2("dx", "cs"); a.binop("MUL")
        a.push2("dy", "sn"); a.binop("MUL"); a.binop("ADD"); a.unop("16 RSHIFT#"); a.rename_top(dname)
        a.discard("dx"); a.discard("dy")
    to_view("x1w", "y1w", "s1", "d1")
    to_view("x2w", "y2w", "s2", "d2")
    # C = s1*d2 - d1*s2 ; negC = -C ; front = C < 0
    a.push2("s1", "d2"); a.binop("MUL")
    a.push2("d1", "s2"); a.binop("MUL"); a.binop("SUB"); a.rename_top("C")
    a.push("C"); a.emit("0 LESSINT"); a.stack.pop(); a.stack.append("front")
    a.push("C"); a.unop("NEGATE"); a.rename_top("negC")
    # behind = (d1 <= 0) & (d2 <= 0)
    a.push("d1"); a.emit("1 LESSINT"); a.stack.pop(); a.stack.append("b1")
    a.push("d2"); a.emit("1 LESSINT"); a.stack.pop(); a.stack.append("b2")
    a.binop("AND"); a.rename_top("behind")
    # v1: in1 = (d1 > 0) & (s1 + d1 >= 0) & (s1 <= d1) ; cullL = (d1 > 0) & (s1 > d1)
    a.push("d1"); a.emit("0 GTINT"); a.stack.pop(); a.stack.append("dp1")
    a.push2("s1", "d1"); a.binop("ADD"); a.emit("-1 GTINT"); a.stack.pop(); a.stack.append("q")   # s1+d1 >= 0
    a.push2("s1", "d1"); a.binop("LEQ")                                                           # s1 <= d1
    a.binop("AND"); a.push("dp1"); a.binop("AND"); a.rename_top("in1")
    a.push2("s1", "d1"); a.binop("GREATER"); a.push("dp1"); a.binop("AND"); a.rename_top("cullL")
    # v2: in2 = (d2 > 0) & (s2 + d2 >= 0) & (s2 <= d2) ; cullR = (d2 > 0) & (s2 + d2 < 0)
    a.push("d2"); a.emit("0 GTINT"); a.stack.pop(); a.stack.append("dp2")
    a.push2("s2", "d2"); a.binop("ADD"); a.emit("-1 GTINT"); a.stack.pop(); a.stack.append("q2")
    a.push2("s2", "d2"); a.binop("LEQ")
    a.binop("AND"); a.push("dp2"); a.binop("AND"); a.rename_top("in2")
    a.push2("s2", "d2"); a.binop("ADD"); a.emit("0 LESSINT"); a.stack.pop(); a.stack.append("q3")
    a.push("dp2"); a.binop("AND"); a.rename_top("cullR")
    # projections with guarded divisors: x = ((CX<<16) + (s*FOCAL << 16) / d' + 2^15) >> 16
    def project(sname, dname, inname, default, out):
        a.push(inname); a.push(dname); a.const("1 PUSHINT"); a.ternop("CONDSEL"); a.rename_top("dg")
        a.push(sname); a.unop(f"{FOCAL} MULCONST"); a.unop("16 LSHIFT#")
        a.push("dg"); a.binop("DIV")
        a.const(f"{(CX << 16) + (1 << 15)} PUSHINT"); a.binop("ADD"); a.unop("16 RSHIFT#"); a.rename_top("xp")
        a.push(inname); a.push("xp"); a.const(f"{default} PUSHINT"); a.ternop("CONDSEL"); a.rename_top(out)
        a.discard("dg"); a.discard("xp")
    project("s1", "d1", "in1", 0, "x1")
    project("s2", "d2", "in2", W, "x2")
    # ok = front & ~behind & ~cullL & ~cullR & (x1 < x2)
    a.push("front"); a.push("behind"); a.unop("NOT"); a.binop("AND")
    a.push("cullL"); a.unop("NOT"); a.binop("AND")
    a.push("cullR"); a.unop("NOT"); a.binop("AND")
    a.push2("x1", "x2"); a.binop("LESS"); a.binop("AND"); a.rename_top("ok")
    # cols = ok ? colMask(x1, max(x2, x1)) & ~solid : 0
    a.push2("x2", "x1"); a.binop("MAX"); a.push("x1"); a.binop("SUB"); a.emit("POW2 DEC"); a.stack.pop(); a.stack.append("cm")
    a.push("x1"); a.binop("LSHIFT")
    a.push("solid"); a.unop("NOT"); a.binop("AND"); a.rename_top("cm2")
    a.push("ok"); a.push("cm2"); a.const("0 PUSHINT"); a.ternop("CONDSEL"); a.rename_top("cols")
    # setup: dd, ds, numBase, dd2, step24, worlds, steps
    a.push2("d2", "d1"); a.binop("SUB"); a.rename_top("dd")
    a.push2("s2", "s1"); a.binop("SUB"); a.unop(f"{2 * FOCAL} MULCONST")
    a.push("dd"); a.unop(f"{2 * CX - 1} MULCONST"); a.binop("ADD"); a.rename_top("numBase")
    a.push("dd"); a.unop("2 MULCONST"); a.rename_top("dd2")
    # step24 = (dd << 40) / C  (C != 0 when front; guard: if C == 0 use -1)
    a.push("C"); a.emit("DUP 0 EQINT -1 PUSHINT ROT CONDSEL"); a.stack.pop(); a.stack.append("Cg")
    a.push("dd"); a.unop("40 LSHIFT#"); a.push("Cg"); a.binop("DIV"); a.rename_top("step24")
    worlds = {"solid": ["wt", "wb"], "up": ["wt", "wb", "wh"], "lo": ["wt", "wb", "wl"], "both": ["wt", "wb", "wh", "wl"]}[variant]
    src = {"wt": "fceil", "wb": "ffloor", "wh": "bceil", "wl": "bfloor"}
    for wname in worlds:
        a.push2(src[wname], "vz"); a.binop("SUB"); a.rename_top(wname)
    stepname = {"wt": "ts", "wb": "bs", "wh": "hs", "wl": "ls"}
    for wname in worlds:
        a.push2(wname, "step24"); a.binop("MUL"); a.unop("NEGATE"); a.rename_top(stepname[wname])
    # shade: depth16 = 2*FOCAL*negC / max(numBase - xm*dd2, 1) ; lvl = min(4, ubitsize(max(depth16,0) / SHADE_UNIT)) - lightAdj, clamped
    a.push2("x1", "x2"); a.binop("ADD"); a.unop("1 RSHIFT#"); a.rename_top("xm")
    a.push2("xm", "dd2"); a.binop("MUL"); a.push("numBase"); a.emit("SWAP SUB"); a.stack.pop(); a.stack.pop(); a.stack.append("den")
    a.emit("1 PUSHINT MAX"); a.stack.pop(); a.stack.append("deng")
    a.push("negC"); a.unop(f"{2 * FOCAL} MULCONST"); a.push("deng"); a.binop("DIV"); a.rename_top("depth16")
    a.emit("0 PUSHINT MAX"); a.stack.pop(); a.stack.append("dep")
    a.const(f"{shade_unit} PUSHINT"); a.binop("DIV"); a.unop("UBITSIZE"); a.rename_top("lvl0")
    a.emit("4 PUSHINT MIN"); a.stack.pop(); a.stack.append("lvl1")
    a.push("lightAdj"); a.binop("SUB"); a.emit("0 PUSHINT MAX 4 PUSHINT MIN"); a.stack.pop(); a.stack.append("lvl")
    a.unop("1 LSHIFT#"); a.rename_top("li")
    a.push("pats"); a.push("li"); a.binop("INDEXVAR"); a.rename_top("wA")
    a.push("pats"); a.push("li"); a.unop("INC"); a.binop("INDEXVAR"); a.rename_top("wB")
    a.push("f01"); a.const(f"{H} PUSHPOW2DEC"); a.binop("AND"); a.rename_top("fA")
    a.push("f01"); a.unop(f"{H} RSHIFT#"); a.rename_top("fB")
    a.const("0 PUSHINT"); a.rename_top("solidAdd")
    a.const("0 PUSHINT"); a.rename_top("partial")
    # run loop needs: rows wA wB fA fB ts bs [hs ls] wt wb [wh wl] numBase dd2 negC fb cols solidAdd partial -- all present by name
    gen_runs(a, variant, H, W, budget, col_cost)
    # epilogue: return fb solidAdd partial
    a.xchg("fb", a.stack[-3])   # bring fb to s2
    n_junk = len(a.stack) - 3
    while n_junk > 0:
        k = min(15, n_junk)
        a.emit(f"{k} 3 BLKDROP2")
        n_junk -= k
    a.stack = ["fb", "solidAdd", "partial"]
    return "\n".join(a.code), params


def verify_seg(variant, H, W, trials=120, budget=10**9):
    from render import draw_runs
    code, params = gen_seg(variant, H, W, budget=budget)
    FULL = (1 << H) - 1
    rnd = random.Random(hash(variant) & 0xFFF)
    gas_per_col = []
    for _ in range(trials):
        fb = [rnd.getrandbits(2 * H) if rnd.random() < 0.6 else (FULL << H) for _ in range(W)]
        cols = rnd.getrandbits(W)
        wt = rnd.randrange(-200, 300); wb = rnd.randrange(-300, wt + 1)
        wh = rnd.randrange(wb, wt + 1); wl = rnd.randrange(wb, wh + 1)
        # a plausible seg: numBase, dd2, negC from two view-space points
        s1 = rnd.randrange(-400, 400) << 16; d1 = rnd.randrange(8, 600) << 16
        s2 = s1 + rnd.randrange(1, 300) << 16; d2 = rnd.randrange(8, 600) << 16
        C = s1 * d2 - d1 * s2
        if C >= 0:
            s1, s2 = s2, s1; C = s1 * d2 - d1 * s2
            if C >= 0:
                continue
        negC = -C; dd = d2 - d1
        numBase = 2 * (W // 2) * (s2 - s1) + (2 * (W // 2) - 1) * dd
        dd2 = 2 * dd
        step24 = (dd << 40) // C
        ts, bs, hs, ls = -wt * step24, -wb * step24, -wh * step24, -wl * step24
        w0, w1, f0, f1 = [rnd.getrandbits(H) for _ in range(4)]
        vals = {"rows": [FULL ^ ((1 << (H - n)) - 1) for n in range(H + 1)], "w01": w0 | (w1 << H), "f01": f0 | (f1 << H),
                "ts": ts, "bs": bs, "hs": hs, "ls": ls, "wt": wt, "wb": wb, "wh": wh, "wl": wl,
                "numBase": numBase, "dd2": dd2, "negC": negC, "fb": fb, "cols": cols}
        st = Stack([vals[n] for n in params])
        run(st, code)
        assert len(st.items) == 3, st.items
        fb2 = list(fb)
        solid_add, partial, (nr, nc) = draw_runs(variant, H, W // 2, W // 2, H // 2, fb2, cols, (w0, w1), (f0, f1),
                                                 ts, bs, hs, ls, wt, wb, wh, wl, numBase, dd2, negC,
                                                 gas_left=None if budget >= 10**9 else (lambda: 0))
        assert st.items[0] == fb2, (variant, "fb mismatch")
        assert st.items[1] == (solid_add if variant == "solid" else 0), (variant, "solidAdd")
        assert (st.items[2] != 0) == partial, (variant, "partial", st.items[2], partial)
        if nc:
            gas_per_col.append(st.gas / nc)
    return sum(gas_per_col) / max(1, len(gas_per_col))


def verify_seg_full(variant, H, W, trials=150, budget=10**9):
    """Compare gen_seg_full against Renderer.add_seg on random segs/cameras."""
    from render import Renderer, Seg, SOLID, PORTAL, sin_a, cos_a, FRAC
    code, params = gen_seg_full(variant, H, W, budget=budget)
    FULL = (1 << H) - 1
    rnd = random.Random(hash(variant) & 0xFFFF)

    class FakeLevel:
        pass
    r = Renderer(FakeLevel(), W, H)
    rows = [FULL ^ ((1 << (H - n)) - 1) for n in range(H + 1)]
    pats = [p for pair in r.wall_pats for p in pair]
    f01 = r.floor_pat[0] | (r.floor_pat[1] << H)
    gas = []
    tested = 0
    while tested < trials:
        px = rnd.randrange(-500, 500) << 16
        py = rnd.randrange(-500, 500) << 16
        angle = rnd.randrange(512)
        vz = rnd.randrange(-20, 120)
        x1w, y1w = rnd.randrange(-600, 600), rnd.randrange(-600, 600)
        x2w, y2w = x1w + rnd.randrange(-300, 300), y1w + rnd.randrange(-300, 300)
        if (x1w, y1w) == (x2w, y2w):
            continue
        # world-space backface test (the Tolk caller does this first)
        dx1 = (x1w << 16) - px; dy1 = (y1w << 16) - py
        if (x2w - x1w) * (-dy1) - (y2w - y1w) * (-dx1) >= 0:
            continue
        fceil = rnd.randrange(vz - 100, vz + 300); ffloor = rnd.randrange(fceil - 400, fceil)
        bceil = rnd.randrange(ffloor + 1, fceil + 1) if variant in ("up", "both") else fceil
        bfloor = rnd.randrange(ffloor, bceil) if variant in ("lo", "both") else ffloor
        light_adj = rnd.randrange(-1, 2)
        seg = Seg(x1=x1w, y1=y1w, x2=x2w, y2=y2w, kind=SOLID if variant == "solid" else PORTAL,
                  ffloor=ffloor, fceil=fceil, flight=160, fsky=False, bfloor=bfloor, bceil=bceil, idx=0)
        seg.has_upper = variant in ("up", "both"); seg.has_lower = variant in ("lo", "both"); seg.light_adj = light_adj
        fb = [rnd.getrandbits(2 * H) if rnd.random() < 0.5 else (FULL << H) for _ in range(W)]
        solid = rnd.getrandbits(W) if rnd.random() < 0.5 else 0
        # reference
        r.px, r.py, r.sin, r.cos, r.viewz = px, py, sin_a(angle), cos_a(angle), vz << FRAC
        r.fb = list(fb); r.solid = solid; r.partial = False; r.stats = None
        r.add_seg(seg)
        cam = [px, py, sin_a(angle), cos_a(angle), vz]
        vals = {"rows": rows, "pats": pats, "f01": f01, "cam": cam, "solid": solid, "x1w": x1w, "y1w": y1w, "x2w": x2w, "y2w": y2w,
                "ffloor": ffloor, "fceil": fceil, "lightAdj": light_adj, "bfloor": bfloor, "bceil": bceil, "fb": fb}
        st = Stack([vals[n] for n in params])
        run(st, code)
        assert len(st.items) == 3, st.items
        assert st.items[0] == r.fb, (variant, "fb mismatch", tested)
        assert (solid | st.items[1]) == r.solid, (variant, "solid", tested)
        if budget >= 10**9:
            assert st.items[2] == 0
        tested += 1
        gas.append(st.gas)
    return sum(gas) / len(gas)


SEG_FULL_SIGS = {
    "solid": "drawSegSolid", "up": "drawSegUpper", "lo": "drawSegLower", "both": "drawSegBoth",
}
SEG_FULL_SIG = ("rows: array<int>, pats: array<int>, f01: int, cam: array<int>, solid: int, x1w: int, y1w: int, x2w: int, y2w: int, "
                "ffloor: int, fceil: int, lightAdj: int, bfloor: int, bceil: int, fb: array<int>")


SEG_SIGS = {
    "solid": ("drawSegSolid", "rows: array<int>, w01: int, f01: int, ts: int, bs: int, wt: int, wb: int, numBase: int, dd2: int, negC: int, fb: array<int>, cols: int"),
    "up": ("drawSegUpper", "rows: array<int>, w01: int, f01: int, ts: int, bs: int, hs: int, wt: int, wb: int, wh: int, numBase: int, dd2: int, negC: int, fb: array<int>, cols: int"),
    "lo": ("drawSegLower", "rows: array<int>, w01: int, f01: int, ts: int, bs: int, ls: int, wt: int, wb: int, wl: int, numBase: int, dd2: int, negC: int, fb: array<int>, cols: int"),
    "both": ("drawSegBoth", "rows: array<int>, w01: int, f01: int, ts: int, bs: int, hs: int, ls: int, wt: int, wb: int, wh: int, wl: int, numBase: int, dd2: int, negC: int, fb: array<int>, cols: int"),
}


SIGS = {
    "solid": ("fillSolidRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, top: int, bot: int, fb: array<int>, xRun: int"),
    "up": ("fillUpperRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, hs: int, top: int, bot: int, high: int, fb: array<int>, xRun: int"),
    "lo": ("fillLowerRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, ls: int, top: int, bot: int, low: int, fb: array<int>, xRun: int"),
    "both": ("fillBothRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, hs: int, ls: int, top: int, bot: int, high: int, low: int, fb: array<int>, xRun: int"),
}


def main():
    H = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    W = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    budget = int(sys.argv[3]) if len(sys.argv) > 3 else 950000
    out = []
    out.append("// ---- GENERATED by tools/asmgen.py (H=%d, W=%d, gas budget %d) — do not edit by hand ----" % (H, W, budget))
    for variant in ("solid", "up", "lo", "both"):
        g = verify_seg(variant, H, W)
        verify_seg(variant, H, W, trials=10, budget=1)   # guard path: everything skipped, partial set
        code, params = gen_seg(variant, H, W, budget=budget)
        name, sig = SEG_SIGS[variant]
        out.append(f"// {variant}: ~{g:.0f} gas per column incl. run overhead on random (short) runs (simulator estimate)")
        out.append(f"// params: {' '.join(params)}; returns (fb, solidAdd, partial): solidAdd = mask of columns closed (solid only),")
        out.append("//         partial != 0 if the gas guard (budget above) stopped before a run. Never throws.")
        out.append("@pure")
        out.append(f"fun {name}({sig}): (array<int>, int, int)")
        out.append('    asm """')
        out.extend("    " + line for line in code.split("\n"))
        out.append('    """')
        out.append("")
    out.append("// ---- END GENERATED ----")
    print("\n".join(out))


if __name__ == "__main__":
    main()
