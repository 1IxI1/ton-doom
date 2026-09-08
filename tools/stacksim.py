#!/usr/bin/env python3
"""Tiny simulator for the TVM stack-op subset used in the hand-written asm loops of Doom.tolk.

Semantics follow the TVM spec (s0 = top). Values: Python ints, or Python lists for tuples.
Also reports an approximate gas cost using the base formula (10 + opcode bits).

Usage as a library:
    st = Stack([..bottom..top..]); run(st, "s1 s0 PUSH2 INDEXVAR ...")
"""
from __future__ import annotations

import re

MASK257 = (1 << 257) - 1


def sgn257(v: int) -> int:
    lo, hi = -(1 << 256), (1 << 256) - 1
    if v < lo or v > hi:
        raise OverflowError(f"int overflow {v}")
    return v


class Stack:
    def __init__(self, items=None):
        self.items = list(items or [])  # bottom .. top
        self.gas = 0

    # s(i) access: s0 = top
    def s(self, i):
        return self.items[-1 - i]

    def set_s(self, i, v):
        self.items[-1 - i] = v

    def push(self, v):
        self.items.append(v)

    def pop(self):
        return self.items.pop()

    def depth(self):
        return len(self.items)

    def __repr__(self):
        return "[" + " ".join(fmt(v) for v in self.items) + "]"


def fmt(v):
    if isinstance(v, list):
        return f"T{len(v)}"
    if isinstance(v, int) and abs(v) >= 1 << 32:
        return f"0x{v:x}" if v >= 0 else f"-0x{-v:x}"
    return str(v)


def tokenize(src: str):
    src = re.sub(r"//.*", "", src)
    return src.replace("\n", " ").split()


def run(st: Stack, src: str, trace=False):
    toks = tokenize(src)
    i = 0
    args = []

    def gas(n):
        st.gas += n

    while i < len(toks):
        t = toks[i]
        i += 1
        if re.fullmatch(r"-?\d+", t):
            args.append(int(t))
            continue
        m = re.fullmatch(r"s(\d+)", t)
        if m:
            args.append(int(m.group(1)))
            continue
        if t == "s()":
            continue  # "N s()" form: the number is already in args
        op = t
        a = args
        args = []
        if op == "PUSH":
            (k,) = a
            st.push(st.s(k))
            gas(18 if k < 16 else 26)
        elif op == "PUSH2":
            k, j = a
            st.push(st.s(k))
            st.push(st.s(j + 1))
            gas(34)
        elif op == "XCHG":
            if len(a) == 1:
                k, j = 0, a[0]
            else:
                k, j = a
            vk, vj = st.s(k), st.s(j)
            st.set_s(k, vj)
            st.set_s(j, vk)
            gas(18 if (k == 0 and j < 16) else 26)
        elif op == "DUP":
            st.push(st.s(0)); gas(18)
        elif op == "OVER":
            st.push(st.s(1)); gas(18)
        elif op == "SWAP":
            x = st.pop(); y = st.pop(); st.push(x); st.push(y); gas(18)
        elif op == "ROT":
            c = st.pop(); b = st.pop(); a_ = st.pop(); st.push(b); st.push(c); st.push(a_); gas(18)
        elif op == "ROTREV" or op == "-ROT":
            c = st.pop(); b = st.pop(); a_ = st.pop(); st.push(c); st.push(a_); st.push(b); gas(18)
        elif op == "DROP":
            st.pop(); gas(18)
        elif op == "NIP":
            x = st.pop(); st.pop(); st.push(x); gas(18)
        elif op == "TUCK":
            x = st.pop(); y = st.pop(); st.push(x); st.push(y); st.push(x); gas(18)
        elif op == "BLKDROP":
            (n,) = a
            for _ in range(n):
                st.pop()
            gas(26)
        elif op == "BLKSWAP":
            n, m_ = a
            top = [st.pop() for _ in range(m_)][::-1]
            below = [st.pop() for _ in range(n)][::-1]
            for v in top: st.push(v)
            for v in below: st.push(v)
            gas(26)
        elif op == "INC":
            st.push(sgn257(st.pop() + 1)); gas(18)
        elif op == "DEC":
            st.push(sgn257(st.pop() - 1)); gas(18)
        elif op == "ADD":
            y = st.pop(); x = st.pop(); st.push(sgn257(x + y)); gas(18)
        elif op == "SUB":
            y = st.pop(); x = st.pop(); st.push(sgn257(x - y)); gas(18)
        elif op == "MUL":
            y = st.pop(); x = st.pop(); st.push(sgn257(x * y)); gas(18)
        elif op == "AND":
            y = st.pop(); x = st.pop(); st.push(x & y); gas(18)
        elif op == "OR":
            y = st.pop(); x = st.pop(); st.push(x | y); gas(18)
        elif op == "XOR":
            y = st.pop(); x = st.pop(); st.push(x ^ y); gas(18)
        elif op == "NOT":
            st.push(~st.pop()); gas(18)
        elif op == "NEGATE":
            st.push(-st.pop()); gas(18)
        elif op == "MAX":
            y = st.pop(); x = st.pop(); st.push(max(x, y)); gas(26)
        elif op == "MIN":
            y = st.pop(); x = st.pop(); st.push(min(x, y)); gas(26)
        elif op == "RSHIFT#":
            (n,) = a
            st.push(st.pop() >> n); gas(26)
        elif op == "LSHIFT#":
            (n,) = a
            st.push(sgn257(st.pop() << n)); gas(26)
        elif op == "RSHIFT":
            n = st.pop()
            if not 0 <= n <= 1023:
                raise ValueError(f"RSHIFT range {n}")
            st.push(st.pop() >> n); gas(18)
        elif op == "LSHIFT":
            n = st.pop()
            if not 0 <= n <= 1023:
                raise ValueError(f"LSHIFT range {n}")
            st.push(sgn257(st.pop() << n)); gas(18)
        elif op == "PUSHPOW2DEC":
            (n,) = a
            st.push((1 << n) - 1); gas(26)
        elif op == "PUSHPOW2":
            (n,) = a
            st.push(1 << n); gas(26)
        elif op == "PUSHINT":
            (n,) = a
            st.push(n); gas(18 if -5 <= n <= 10 else (26 if -128 <= n <= 127 else 34))
        elif op == "INDEXVAR":
            k = st.pop(); tup = st.pop()
            if not isinstance(tup, list):
                raise TypeError(f"INDEXVAR on non-tuple {fmt(tup)}")
            st.push(tup[k]); gas(26)
        elif op == "SETINDEXVAR":
            k = st.pop(); v = st.pop(); tup = st.pop()
            if not isinstance(tup, list):
                raise TypeError(f"SETINDEXVAR on non-tuple {fmt(tup)}")
            tup = list(tup); tup[k] = v; st.push(tup); gas(26 + len(tup))
        elif op in ("REPEAT:<{", "IF:<{", "IFNOT:<{"):
            # find matching }>
            depth = 1
            j = i
            while depth:
                if toks[j].endswith(":<{") or toks[j] == "<{":
                    depth += 1
                elif toks[j] == "}>":
                    depth -= 1
                j += 1
            body = " ".join(toks[i : j - 1])
            i = j
            n = st.pop()
            gas(18)
            if op == "REPEAT:<{":
                for _ in range(n):
                    gas(5)
                    run(st, body, trace)
            elif (op == "IF:<{") == (n != 0):
                gas(5)
                run(st, body, trace)
        else:
            raise ValueError(f"unknown op {op} (args {a})")
        if trace:
            print(f"{op:12s} {a} -> {st}")
    return st
