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

    def drop_top(self):
        self.stack.pop()
        self.emit("DROP")

    def blkdrop_top(self, n):
        for _ in range(n):
            self.stack.pop()
        self.emit(f"{n} BLKDROP")


def mask_rows_above(a: Asm, src: str, ceil: bool, H: int):
    """Push mask of rows above boundary: rows [0, n-1] with n = ceil(src/2^24) (ceil=True) or floor(src/2^24)+1.
    Uses the frame register `rows` (tuple: rows[n] = mask of the top n rows, n in 0..H)."""
    a.push(src)
    if ceil:
        a.const("24 PUSHPOW2DEC")
        a.binop("ADD")
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


def gen_column(a: Asm, variant: str, H: int, wall: str, floor: str):
    """One column: reads fb[x], writes it back, advances x. Names of boundaries: top, bot, high, low."""
    a.push2("fb", "x")
    e = a.binop("INDEXVAR"); a.rename_top("e")
    a.emit("DUP"); a.stack.append("M0")
    a.unop(f"{H} RSHIFT#"); a.rename_top("M")
    A = mask_rows_above(a, "top", True, H); a.rename_top("A")
    B = mask_rows_above(a, "bot", False, H); a.rename_top("B")
    if variant in ("up", "both"):
        mask_rows_above(a, "high", False, H); a.rename_top("Ahi")
    if variant in ("lo", "both"):
        mask_rows_above(a, "low", True, H); a.rename_top("Blo")
    upper = "Ahi" if variant in ("up", "both") else "A"
    lower = "Blo" if variant in ("lo", "both") else "B"
    # wall piece: M & ((upper ^ A) | (B ^ lower))  (terms that are identical vanish)
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
    # floor piece: floor & (M ^ (M & B))
    a.push2("M", "B"); a.binop("AND")
    a.push("M"); a.binop("XOR")
    a.push(floor); a.binop("AND")
    a.binop("OR")                      # wp | fp
    a.push("e"); a.binop("OR")         # e | pieces
    a.const(f"{H} PUSHPOW2DEC"); a.binop("AND"); a.rename_top("pix")
    if variant != "solid":
        # new opening M' = M & lower & ~upper
        a.push2("M", lower); a.binop("AND")
        a.push(upper); a.unop("NOT"); a.binop("AND")
        a.unop(f"{H} LSHIFT#")
        a.binop("OR"); a.rename_top("pix")
    # drop temporaries between pix and cnt: everything above cnt except pix
    ic = a.stack.index("cnt")
    n_tmp = len(a.stack) - 1 - ic - 1
    if n_tmp:
        a.emit(f"s0 s{n_tmp} XCHG")
        a.stack[ic + 1], a.stack[-1] = a.stack[-1], a.stack[ic + 1]
        a.blkdrop_top(n_tmp)
    # stack: ... fb x cnt pix  -> arrange (fb pix x') with a copy of x, then SETINDEXVAR
    a.push("x"); a.rename_top("x2")
    a.xchg("fb", "x")
    a.xchg("fb", "cnt")             # ... x cnt fb pix x2
    a.emit("SETINDEXVAR")
    a.stack.pop(); a.stack.pop(); a.stack.pop(); a.stack.append("fb")
    a.emit("ROT"); a.emit("ROT")    # x cnt fb -> fb x cnt
    x_, c_, f_ = a.stack[-3], a.stack[-2], a.stack[-1]
    a.stack[-3], a.stack[-2], a.stack[-1] = f_, x_, c_
    a.emit("SWAP INC SWAP")
    # steps
    for b, st in (("top", "ts"), ("bot", "bs"), ("high", "hs"), ("low", "ls")):
        if b not in a.stack:
            continue
        a.push2(b, st); t = a.binop("ADD")
        a.xchg(t, b)
        a.drop_top()
        a.stack[a.stack.index(t)] = b


def gen_loop(variant: str, H: int) -> tuple[str, list[str]]:
    bounds = {"solid": ["top", "bot"], "up": ["top", "bot", "high"], "lo": ["top", "bot", "low"],
              "both": ["top", "bot", "high", "low"]}[variant]
    steps = {"top": "ts", "bot": "bs", "high": "hs", "low": "ls"}
    frame = ["rows", "wA", "wB", "fA", "fB"] + [steps[b] for b in bounds] + bounds + ["fb", "x", "cnt"]
    a = Asm(frame)
    # body: two columns (phase A then phase B), no pattern swapping
    gen_column(a, variant, H, "wA", "fA")
    gen_column(a, variant, H, "wB", "fB")
    assert a.stack == frame, (a.stack, frame)
    body2 = a.code
    a = Asm(frame)
    gen_column(a, variant, H, "wA", "fA")
    assert a.stack == frame
    body1 = a.code
    n = len(frame)
    # x carries run in bits 16.. : run = xRun >> 16, x = xRun & 0xFFFF
    # stack on entry: frame[:-1] + [xRun]
    code = [
        "DUP 16 RSHIFT# SWAP 16 PUSHPOW2DEC AND SWAP",   # ... fb xRun -> ... fb x run   (run = cnt slot)
        "DUP 1 RSHIFT#",                                  # ... fb x run half
        "REPEAT:<{",
        *["    " + c for c in body2],
        "}>",
        "1 PUSHINT AND",                                  # ... fb x odd
        "DUP",                                            # ... fb x odd odd   (cnt slot = odd during IF)
        "IF:<{",
        *["    " + c for c in body1],
        "}>",
        "2 BLKDROP",                                      # ... fb
        f"s0 s{n - 3} XCHG {n - 3} BLKDROP",
    ]
    return "\n".join(code), frame


def ref_run(variant, H, fb, x, run, top, bot, high, low, ts, bs, hs, ls, wA, wB, fA, fB):
    from render import fill_run
    return fill_run(variant, H, list(fb), x, run, top, bot, high, low, ts, bs, hs, ls, wA, wB, fA, fB)


def verify(variant, H, code, frame, trials=150):
    FULL = (1 << H) - 1
    rnd = random.Random(hash(variant) & 0xFFFF)
    gas = []
    for _ in range(trials):
        W = 20
        fb = [rnd.getrandbits(2 * H) if rnd.random() < 0.5 else (FULL << H) for _ in range(W)]
        x = rnd.randrange(0, W - 1); run_ = rnd.randrange(0, W - x + 1)
        top = rnd.randrange(-300 << 24, 300 << 24); bot = top + rnd.randrange(0, 300 << 24)
        high = top + rnd.randrange(0, bot - top + 1); low = high + rnd.randrange(0, bot - high + 1)
        ts, bs, hs, ls = [rnd.randrange(-3 << 24, 3 << 24) for _ in range(4)]
        wA, wB, fA, fB = [rnd.getrandbits(H) for _ in range(4)]
        rows = [FULL ^ ((1 << (H - n)) - 1) for n in range(H + 1)]
        vals = {"rows": rows, "wA": wA, "wB": wB, "fA": fA, "fB": fB, "ts": ts, "bs": bs, "hs": hs, "ls": ls,
                "top": top, "bot": bot, "high": high, "low": low, "fb": fb}
        st = Stack([vals[n] for n in frame[:-2]] + [x | (run_ << 16)])
        run(st, code)
        assert len(st.items) == 1, st.items
        exp = ref_run(variant, H, fb, x, run_, top, bot, high, low, ts, bs, hs, ls, wA, wB, fA, fB)
        assert st.items[0] == exp, variant
        if run_:
            gas.append(st.gas / run_)
    return sum(gas) / len(gas)


SIGS = {
    "solid": ("fillSolidRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, top: int, bot: int, fb: array<int>, xRun: int"),
    "up": ("fillUpperRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, hs: int, top: int, bot: int, high: int, fb: array<int>, xRun: int"),
    "lo": ("fillLowerRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, ls: int, top: int, bot: int, low: int, fb: array<int>, xRun: int"),
    "both": ("fillBothRun", "rows: array<int>, wA: int, wB: int, fA: int, fB: int, ts: int, bs: int, hs: int, ls: int, top: int, bot: int, high: int, low: int, fb: array<int>, xRun: int"),
}


def main():
    H = int(sys.argv[1]) if len(sys.argv) > 1 else 96
    out = []
    out.append("// ---- GENERATED by tools/asmgen.py (H=%d) — do not edit by hand ----" % H)
    for variant in ("solid", "up", "lo", "both"):
        code, frame = gen_loop(variant, H)
        g = verify(variant, H, code, frame)
        name, sig = SIGS[variant]
        out.append(f"// {variant}: ~{g:.0f} gas per column (simulator estimate). Frame: {' '.join(frame)}")
        out.append("@pure")
        out.append(f"fun {name}({sig}): array<int>")
        out.append('    asm """')
        out.extend("    " + line for line in code.split("\n"))
        out.append('    """')
        out.append("")
    out.append("// ---- END GENERATED ----")
    print("\n".join(out))


if __name__ == "__main__":
    main()
