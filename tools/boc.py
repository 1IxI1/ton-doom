"""Minimal, dependency-free TON cell / BOC library.

Covers exactly what ton-doom needs:
  * Cell / Builder / Slice with uint/int/bits/refs
  * representation hash + depth (for StateInit hashes -> contract address)
  * BOC serialization (with CRC32C) and deserialization
  * user-friendly address encoding/decoding
"""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass, field
from typing import Iterable, List, Optional


# --------------------------------------------------------------------------- #
# CRC helpers
# --------------------------------------------------------------------------- #
def _crc32c_table():
    poly = 0x82F63B78
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C_TABLE = _crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32C_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


# --------------------------------------------------------------------------- #
# Cell
# --------------------------------------------------------------------------- #
@dataclass(eq=False)
class Cell:
    """Ordinary cell. `bits` is an int holding `nbits` bits (MSB first)."""

    bits: int = 0
    nbits: int = 0
    refs: List["Cell"] = field(default_factory=list)
    _hash: Optional[bytes] = field(default=None, repr=False)
    _depth: Optional[int] = field(default=None, repr=False)

    # -- data bytes --------------------------------------------------------- #
    def data_bytes(self) -> bytes:
        """Data padded to a byte boundary the TON way (1 then 0s if unaligned)."""
        nbytes = (self.nbits + 7) // 8
        if self.nbits % 8 == 0:
            return self.bits.to_bytes(nbytes, "big") if nbytes else b""
        pad = nbytes * 8 - self.nbits
        v = (self.bits << pad) | (1 << (pad - 1))
        return v.to_bytes(nbytes, "big")

    def descriptors(self) -> bytes:
        d1 = len(self.refs)  # ordinary cell, level 0
        d2 = (self.nbits // 8) + ((self.nbits + 7) // 8)
        return bytes([d1, d2])

    def depth(self) -> int:
        if self._depth is None:
            self._depth = 0 if not self.refs else 1 + max(r.depth() for r in self.refs)
        return self._depth

    def hash(self) -> bytes:
        if self._hash is None:
            h = hashlib.sha256()
            h.update(self.descriptors())
            h.update(self.data_bytes())
            for r in self.refs:
                h.update(r.depth().to_bytes(2, "big"))
            for r in self.refs:
                h.update(r.hash())
            self._hash = h.digest()
        return self._hash

    def begin_parse(self) -> "Slice":
        return Slice(self)

    def to_boc(self, with_crc: bool = True) -> bytes:
        return serialize_boc(self, with_crc=with_crc)

    def to_boc_b64(self) -> str:
        return base64.b64encode(self.to_boc()).decode()

    def to_boc_hex(self) -> str:
        return self.to_boc().hex()

    def __repr__(self) -> str:  # pragma: no cover
        return f"Cell({self.nbits} bits, {len(self.refs)} refs, {self.hash().hex()[:8]})"


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class Builder:
    def __init__(self):
        self.bits = 0
        self.nbits = 0
        self.refs: List[Cell] = []

    def remaining_bits(self) -> int:
        return 1023 - self.nbits

    def store_uint(self, value: int, n: int) -> "Builder":
        if n == 0:
            return self
        if value < 0 or value >= (1 << n):
            raise ValueError(f"store_uint: {value} does not fit in {n} bits")
        self.bits = (self.bits << n) | value
        self.nbits += n
        if self.nbits > 1023:
            raise ValueError("cell overflow (>1023 bits)")
        return self

    def store_int(self, value: int, n: int) -> "Builder":
        lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
        if value < lo or value > hi:
            raise ValueError(f"store_int: {value} does not fit in {n} bits")
        return self.store_uint(value & ((1 << n) - 1), n)

    def store_bit(self, b: bool) -> "Builder":
        return self.store_uint(1 if b else 0, 1)

    def store_bits(self, bits: int, n: int) -> "Builder":
        return self.store_uint(bits, n)

    def store_bytes(self, data: bytes) -> "Builder":
        for b in data:
            self.store_uint(b, 8)
        return self

    def store_coins(self, amount: int) -> "Builder":
        if amount == 0:
            return self.store_uint(0, 4)
        nbytes = (amount.bit_length() + 7) // 8
        return self.store_uint(nbytes, 4).store_uint(amount, nbytes * 8)

    def store_ref(self, c: Cell) -> "Builder":
        if len(self.refs) >= 4:
            raise ValueError("cell overflow (>4 refs)")
        self.refs.append(c)
        return self

    def store_slice(self, s: "Slice") -> "Builder":
        rem = s.remaining_bits()
        if rem:
            self.store_uint(s.preload_uint(rem), rem)
        for r in s.remaining_refs():
            self.store_ref(r)
        return self

    def store_address(self, addr: Optional["Address"]) -> "Builder":
        if addr is None:
            return self.store_uint(0, 2)
        self.store_uint(0b100, 3)  # addr_std$10 anycast:nothing$0
        self.store_int(addr.workchain, 8)
        self.store_uint(int.from_bytes(addr.hash, "big"), 256)
        return self

    def end_cell(self) -> Cell:
        return Cell(self.bits, self.nbits, list(self.refs))


def begin_cell() -> Builder:
    return Builder()


# --------------------------------------------------------------------------- #
# Slice
# --------------------------------------------------------------------------- #
class Slice:
    def __init__(self, cell: Cell):
        self.cell = cell
        self.pos = 0
        self.ref_pos = 0

    def remaining_bits(self) -> int:
        return self.cell.nbits - self.pos

    def remaining_refs(self) -> List[Cell]:
        return self.cell.refs[self.ref_pos :]

    def preload_uint(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > self.cell.nbits:
            raise ValueError("slice underflow")
        shift = self.cell.nbits - self.pos - n
        return (self.cell.bits >> shift) & ((1 << n) - 1)

    def load_uint(self, n: int) -> int:
        v = self.preload_uint(n)
        self.pos += n
        return v

    def load_int(self, n: int) -> int:
        v = self.load_uint(n)
        if v >= 1 << (n - 1):
            v -= 1 << n
        return v

    def load_bit(self) -> bool:
        return self.load_uint(1) == 1

    def load_bytes(self, n: int) -> bytes:
        return self.load_uint(8 * n).to_bytes(n, "big") if n else b""

    def load_coins(self) -> int:
        n = self.load_uint(4)
        return self.load_uint(8 * n) if n else 0

    def load_ref(self) -> Cell:
        if self.ref_pos >= len(self.cell.refs):
            raise ValueError("ref underflow")
        r = self.cell.refs[self.ref_pos]
        self.ref_pos += 1
        return r

    def preload_ref(self) -> Cell:
        return self.cell.refs[self.ref_pos]

    def skip_bits(self, n: int) -> "Slice":
        self.pos += n
        return self

    def load_address(self) -> Optional["Address"]:
        tag = self.load_uint(2)
        if tag == 0:
            return None
        if tag != 2:
            raise ValueError(f"unsupported address tag {tag}")
        anycast = self.load_uint(1)
        if anycast:
            raise ValueError("anycast not supported")
        wc = self.load_int(8)
        h = self.load_bytes(32)
        return Address(wc, h)


# --------------------------------------------------------------------------- #
# Address
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Address:
    workchain: int
    hash: bytes

    @staticmethod
    def parse(s: str) -> "Address":
        s = s.strip()
        if ":" in s:
            wc, h = s.split(":")
            return Address(int(wc), bytes.fromhex(h))
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)) if ("-" in s or "_" in s) else base64.b64decode(s)
        if len(raw) != 36:
            raise ValueError("bad friendly address length")
        body, crc = raw[:34], raw[34:]
        if crc16_xmodem(body).to_bytes(2, "big") != crc:
            raise ValueError("bad address checksum")
        wc = body[1] if body[1] < 128 else body[1] - 256
        return Address(wc, body[2:34])

    def to_raw(self) -> str:
        return f"{self.workchain}:{self.hash.hex()}"

    def to_friendly(self, bounceable: bool = True, testnet: bool = True, url_safe: bool = True) -> str:
        tag = 0x11 if bounceable else 0x51
        if testnet:
            tag |= 0x80
        body = bytes([tag, self.workchain & 0xFF]) + self.hash
        raw = body + crc16_xmodem(body).to_bytes(2, "big")
        enc = base64.urlsafe_b64encode(raw) if url_safe else base64.b64encode(raw)
        return enc.decode()

    def __str__(self) -> str:
        return self.to_raw()


# --------------------------------------------------------------------------- #
# BOC serialization
# --------------------------------------------------------------------------- #
def _topo_order(root: Cell) -> List[Cell]:
    """Reverse post-order: every cell precedes all of its descendants."""
    seen = {}
    post: List[Cell] = []

    def visit(c: Cell):
        key = c.hash()
        if key in seen:
            return
        seen[key] = True
        for r in c.refs:
            visit(r)
        post.append(c)

    visit(root)
    post.reverse()
    return post


def serialize_boc(root: Cell, with_crc: bool = True) -> bytes:
    cells = _topo_order(root)
    index = {c.hash(): i for i, c in enumerate(cells)}
    # dedupe identical cells that are distinct objects
    uniq: List[Cell] = []
    seen = set()
    for c in cells:
        if c.hash() not in seen:
            seen.add(c.hash())
            uniq.append(c)
    cells = uniq
    index = {c.hash(): i for i, c in enumerate(cells)}
    n = len(cells)
    ref_size = max(1, (n.bit_length() + 7) // 8)

    payload = bytearray()
    for c in cells:
        payload += c.descriptors()
        payload += c.data_bytes()
        for r in c.refs:
            payload += index[r.hash()].to_bytes(ref_size, "big")
    tot = len(payload)
    off_size = max(1, (tot.bit_length() + 7) // 8)

    out = bytearray()
    out += b"\xb5\xee\x9c\x72"
    flags = (0x40 if with_crc else 0) | ref_size
    out.append(flags)
    out.append(off_size)
    out += n.to_bytes(ref_size, "big")
    out += (1).to_bytes(ref_size, "big")  # roots
    out += (0).to_bytes(ref_size, "big")  # absent
    out += tot.to_bytes(off_size, "big")
    out += (0).to_bytes(ref_size, "big")  # root index
    out += payload
    if with_crc:
        out += crc32c(bytes(out)).to_bytes(4, "little")
    return bytes(out)


def deserialize_boc(data: bytes) -> Cell:
    roots = deserialize_boc_roots(data)
    return roots[0]


def deserialize_boc_roots(data: bytes) -> List[Cell]:
    if data[:4] != b"\xb5\xee\x9c\x72":
        raise ValueError("bad BOC magic")
    flags = data[4]
    has_idx = bool(flags & 0x80)
    has_crc = bool(flags & 0x40)
    ref_size = flags & 7
    off_size = data[5]
    p = 6

    def rd(sz):
        nonlocal p
        v = int.from_bytes(data[p : p + sz], "big")
        p += sz
        return v

    n = rd(ref_size)
    nroots = rd(ref_size)
    _absent = rd(ref_size)
    _tot = rd(off_size)
    root_idx = [rd(ref_size) for _ in range(nroots)]
    if has_idx:
        p += n * off_size
    raw = []
    for _ in range(n):
        d1, d2 = data[p], data[p + 1]
        p += 2
        nrefs = d1 & 7
        if d1 & 8:
            raise ValueError("exotic cells not supported")
        nbytes = (d2 + 1) // 2
        chunk = data[p : p + nbytes]
        p += nbytes
        if d2 & 1:
            v = int.from_bytes(chunk, "big")
            # strip padding: last 1 bit and following zeros
            pad = (v & -v).bit_length()  # position of lowest set bit (1-based)
            nbits = nbytes * 8 - pad
            bits = v >> pad
        else:
            nbits = nbytes * 8
            bits = int.from_bytes(chunk, "big") if nbytes else 0
        refs = [rd(ref_size) for _ in range(nrefs)]
        raw.append((bits, nbits, refs))
    if has_crc:
        expect = int.from_bytes(data[p : p + 4], "little")
        if crc32c(data[:p]) != expect:
            raise ValueError("BOC crc32c mismatch")
    cells: List[Optional[Cell]] = [None] * n
    for i in range(n - 1, -1, -1):
        bits, nbits, refs = raw[i]
        for r in refs:
            if r <= i or cells[r] is None:
                raise ValueError(f"bad ref order {i}->{r}")
        cells[i] = Cell(bits, nbits, [cells[r] for r in refs])
    return [cells[i] for i in root_idx]


# --------------------------------------------------------------------------- #
# StateInit / address helpers
# --------------------------------------------------------------------------- #
def state_init_cell(code: Cell, data: Cell) -> Cell:
    # _ split_depth:(Maybe (## 5)) special:(Maybe TickTock) code:(Maybe ^Cell) data:(Maybe ^Cell) library:(Maybe ^Cell)
    return begin_cell().store_uint(0b00110, 5).store_ref(code).store_ref(data).end_cell()


def contract_address(code: Cell, data: Cell, workchain: int = 0) -> Address:
    return Address(workchain, state_init_cell(code, data).hash())


def external_message(dest: Address, body: Cell, state_init: Optional[Cell] = None) -> Cell:
    """ext_in_msg_info$10 src:MsgAddressExt dest:MsgAddressInt import_fee:Grams init:(Maybe (Either StateInit ^StateInit)) body:(Either X ^X)"""
    b = begin_cell()
    b.store_uint(0b10, 2)  # ext_in_msg_info
    b.store_uint(0, 2)  # src addr_none
    b.store_address(dest)
    b.store_coins(0)  # import fee
    if state_init is None:
        b.store_uint(0, 1)
    else:
        b.store_uint(0b11, 2)  # init present, stored as ref
        b.store_ref(state_init)
    # body: always as a ref (simplest, always fits)
    b.store_uint(1, 1)
    b.store_ref(body)
    return b.end_cell()


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    # Known vector: empty cell hash
    empty = begin_cell().end_cell()
    assert empty.hash().hex() == "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7", empty.hash().hex()
    # roundtrip
    inner = begin_cell().store_uint(0xDEADBEEF, 32).store_int(-5, 8).end_cell()
    c = begin_cell().store_uint(5, 3).store_ref(inner).store_ref(inner).store_bits(0b1011, 4).end_cell()
    boc = c.to_boc()
    c2 = deserialize_boc(boc)
    assert c2.hash() == c.hash(), (c2, c)
    s = c2.begin_parse()
    assert s.load_uint(3) == 5 and s.load_uint(4) == 0b1011
    s2 = s.load_ref().begin_parse()
    assert s2.load_uint(32) == 0xDEADBEEF and s2.load_int(8) == -5
    # address roundtrip
    a = Address.parse("kQBkb28fExJEllBL1lRBvA0Gd2RaOx5GCJbwopnxPlNiWv43")
    assert a.to_friendly() == "kQBkb28fExJEllBL1lRBvA0Gd2RaOx5GCJbwopnxPlNiWv43", a.to_friendly()
    assert Address.parse(a.to_raw()) == a
    # decode a real config cell from toncenter (gas prices config 21 testnet)
    cfg = deserialize_boc(base64.b64decode("te6cckEBAQEATAAAlNEAAAAAAAAAZAAAAAAAABoL3gAAAAAAQqqrAAAAAAAPQkAAAAAAAA9CQAAAAAAAACcQAAAAAACYloAAAAAABfXhAAAAAAA7msoAgyFv5Q=="))
    s = cfg.begin_parse()
    assert s.load_uint(8) == 0xD1
    s.load_uint(64); s.load_uint(64)
    assert s.load_uint(8) == 0xDE
    s.load_uint(64)
    assert s.load_uint(64) == 1_000_000
    print("boc.py self-test OK", file=sys.stderr)
