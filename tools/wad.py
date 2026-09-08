#!/usr/bin/env python3
"""
wad.py - stdlib-only reader for classic Doom WAD files (IWAD/PWAD).

Extracts a map's THINGS, LINEDEFS, SIDEDEFS, VERTEXES, SEGS, SSECTORS, NODES,
SECTORS (plus REJECT/BLOCKMAP sizes) into plain Python dicts/lists using the
classic (vanilla Doom 1.9) binary formats.  All multi-byte integers are
little-endian.

Binary formats implemented (field order, width, signedness):

  WAD header (12 bytes)
    0  char[4]  identification  "IWAD" or "PWAD"
    4  int32    numlumps
    8  int32    infotableofs     byte offset of the directory

  Directory entry (16 bytes, numlumps of them at infotableofs)
    0  int32    filepos
    4  int32    size
    8  char[8]  name             ASCII, NUL padded

  Map lumps follow the (size 0) map marker lump ("E1M1", "MAP01") in order:
  THINGS LINEDEFS SIDEDEFS VERTEXES SEGS SSECTORS NODES SECTORS REJECT BLOCKMAP

  THINGS (10 bytes)
    0  int16 x    2 int16 y    4 int16 angle (degrees 0..359)
    6  int16 type 8 int16 flags (bit0 skill1-2, bit1 skill3, bit2 skill4-5,
                                  bit3 ambush/deaf, bit4 multiplayer only)

  LINEDEFS (14 bytes)
    0  uint16 v1 (start vertex)   2 uint16 v2 (end vertex)
    4  int16  flags               6 int16  special      8 int16 tag
    10 uint16 sidedef_right (front; 0xFFFF = none -> -1)
    12 uint16 sidedef_left  (back;  0xFFFF = none -> -1)
    flags: 0x0001 BLOCKING 0x0002 BLOCKMONSTERS 0x0004 TWOSIDED
           0x0008 DONTPEGTOP 0x0010 DONTPEGBOTTOM 0x0020 SECRET
           0x0040 SOUNDBLOCK 0x0080 DONTDRAW 0x0100 MAPPED

  SIDEDEFS (30 bytes)
    0  int16 xoffset  2 int16 yoffset
    4  char[8] upper texture  12 char[8] lower texture  20 char[8] middle texture
    28 uint16 sector

  VERTEXES (4 bytes)
    0  int16 x  2 int16 y

  SEGS (12 bytes)
    0  uint16 v1  2 uint16 v2
    4  uint16 angle  (BAM16: 0=east, 0x4000=north, 0x8000=west, 0xC000=south;
                      vanilla stores it as a short and shifts <<16 into angle_t)
    6  uint16 linedef
    8  int16  direction (0 = same direction as linedef -> front/right sidedef,
                         1 = opposite -> back/left sidedef)
    10 int16  offset (distance along the linedef from its start vertex
                      (the linedef's v1 if direction 0, v2 if direction 1)
                      to this seg's start vertex, map units)

  SSECTORS (4 bytes)
    0  uint16 numsegs  2 uint16 firstseg

  NODES (28 bytes)
    0  int16 x   2 int16 y   4 int16 dx   6 int16 dy   (partition line)
    8  int16[4] right bbox: top(max y), bottom(min y), left(min x), right(max x)
    16 int16[4] left  bbox: top, bottom, left, right
    24 uint16 right child   26 uint16 left child
       child & 0x8000 set  -> subsector index (child & 0x7FFF)
       otherwise            -> node index
    Right (child 0) is the side where (dx,dy) x (p - (x,y)) <= 0 in vanilla
    R_PointOnSide, i.e. the "front" side.  The root node is the LAST node.

  SECTORS (26 bytes)
    0  int16 floor height   2 int16 ceiling height
    4  char[8] floor flat   12 char[8] ceiling flat
    20 int16 light level    22 int16 special   24 int16 tag

  REJECT: bit table, ceil(numsectors*numsectors/8) bytes (not decoded).
  BLOCKMAP: header int16 xorigin, int16 yorigin, uint16 columns, uint16 rows,
            then columns*rows uint16 offsets (in shorts) into the lump; each
            block list is 0x0000 <linedef...> 0xFFFF.  Only the header is
            decoded here.

CLI:
  python3 tools/wad.py assets/doom1.wad E1M1 --json assets/e1m1.json --stats
  python3 tools/wad.py assets/doom1.wad --list
  python3 tools/wad.py assets/doom1.wad E1M1 --bbox
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from typing import Any

MAP_LUMPS = ("THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES", "SEGS",
             "SSECTORS", "NODES", "SECTORS", "REJECT", "BLOCKMAP")

LINEDEF_FLAGS = {
    0x0001: "BLOCKING",
    0x0002: "BLOCKMONSTERS",
    0x0004: "TWOSIDED",
    0x0008: "DONTPEGTOP",
    0x0010: "DONTPEGBOTTOM",
    0x0020: "SECRET",
    0x0040: "SOUNDBLOCK",
    0x0080: "DONTDRAW",
    0x0100: "MAPPED",
}
ML_TWOSIDED = 0x0004

THING_FLAGS = {
    0x0001: "SKILL12",
    0x0002: "SKILL3",
    0x0004: "SKILL45",
    0x0008: "AMBUSH",
    0x0010: "MULTIPLAYER",
}

NF_SUBSECTOR = 0x8000
NO_SIDEDEF = 0xFFFF


def _name(b: bytes) -> str:
    """Decode an 8-byte NUL padded lump/texture name."""
    return b.split(b"\0", 1)[0].decode("ascii", errors="replace")


def _flag_names(value: int, table: dict[int, str]) -> list[str]:
    return [n for bit, n in table.items() if value & bit]


class Lump:
    __slots__ = ("index", "name", "filepos", "size")

    def __init__(self, index: int, name: str, filepos: int, size: int):
        self.index = index
        self.name = name
        self.filepos = filepos
        self.size = size

    def __repr__(self) -> str:
        return f"Lump({self.index}, {self.name!r}, pos={self.filepos}, size={self.size})"


class WAD:
    """A WAD file loaded fully into memory."""

    def __init__(self, data: bytes, path: str | None = None):
        self.path = path
        self.data = data
        if len(data) < 12:
            raise ValueError("file too small to be a WAD")
        ident, numlumps, infotableofs = struct.unpack_from("<4sii", data, 0)
        if ident not in (b"IWAD", b"PWAD"):
            raise ValueError(f"bad WAD identification {ident!r}")
        self.ident = ident.decode("ascii")
        self.numlumps = numlumps
        self.infotableofs = infotableofs
        if infotableofs + 16 * numlumps > len(data):
            raise ValueError("directory runs past end of file")
        self.lumps: list[Lump] = []
        for i in range(numlumps):
            filepos, size, name = struct.unpack_from("<ii8s", data, infotableofs + 16 * i)
            self.lumps.append(Lump(i, _name(name), filepos, size))

    @classmethod
    def open(cls, path: str) -> "WAD":
        with open(path, "rb") as f:
            return cls(f.read(), path)

    # -- lump access ---------------------------------------------------------

    def lump_data(self, lump: Lump) -> bytes:
        return self.data[lump.filepos:lump.filepos + lump.size]

    def find(self, name: str, start: int = 0) -> Lump | None:
        name = name.upper()
        for lump in self.lumps[start:]:
            if lump.name == name:
                return lump
        return None

    def map_names(self) -> list[str]:
        """All map marker lumps (ExMy / MAPxx) in directory order."""
        out = []
        for i, lump in enumerate(self.lumps):
            if lump.size == 0 and i + 1 < len(self.lumps) and self.lumps[i + 1].name == "THINGS":
                out.append(lump.name)
        return out

    def map_lumps(self, mapname: str) -> dict[str, Lump]:
        """Return {lumpname: Lump} for the lumps belonging to a map."""
        mapname = mapname.upper()
        marker = None
        for i, lump in enumerate(self.lumps):
            if lump.name == mapname and lump.size == 0 \
                    and i + 1 < len(self.lumps) and self.lumps[i + 1].name == "THINGS":
                marker = i
                break
        if marker is None:
            raise KeyError(f"map {mapname} not found in {self.path or 'WAD'}")
        out: dict[str, Lump] = {}
        for lump in self.lumps[marker + 1: marker + 1 + len(MAP_LUMPS)]:
            if lump.name not in MAP_LUMPS or lump.name in out:
                break
            out[lump.name] = lump
        return out

    # -- map decoding --------------------------------------------------------

    def read_map(self, mapname: str, with_derived: bool = True) -> dict[str, Any]:
        lumps = self.map_lumps(mapname)

        def raw(name: str) -> bytes:
            lump = lumps.get(name)
            return self.lump_data(lump) if lump else b""

        m: dict[str, Any] = {
            "wad": os.path.basename(self.path) if self.path else None,
            "wad_type": self.ident,
            "map": mapname.upper(),
            "things": parse_things(raw("THINGS")),
            "linedefs": parse_linedefs(raw("LINEDEFS")),
            "sidedefs": parse_sidedefs(raw("SIDEDEFS")),
            "vertexes": parse_vertexes(raw("VERTEXES")),
            "segs": parse_segs(raw("SEGS")),
            "ssectors": parse_ssectors(raw("SSECTORS")),
            "nodes": parse_nodes(raw("NODES")),
            "sectors": parse_sectors(raw("SECTORS")),
            "reject_bytes": len(raw("REJECT")),
            "blockmap": parse_blockmap_header(raw("BLOCKMAP")),
        }
        if with_derived:
            add_derived(m)
        return m


# ---------------------------------------------------------------------------
# lump parsers


def _records(data: bytes, size: int, lumpname: str) -> int:
    if len(data) % size:
        print(f"warning: {lumpname} size {len(data)} is not a multiple of {size}",
              file=sys.stderr)
    return len(data) // size


def parse_things(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 10, "THINGS")):
        x, y, angle, typ, flags = struct.unpack_from("<hhhhh", data, 10 * i)
        out.append({"x": x, "y": y, "angle": angle, "type": typ, "flags": flags,
                    "flag_names": _flag_names(flags, THING_FLAGS)})
    return out


def parse_linedefs(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 14, "LINEDEFS")):
        v1, v2, flags, special, tag, right, left = \
            struct.unpack_from("<HHhhhHH", data, 14 * i)
        out.append({
            "v1": v1, "v2": v2,
            "flags": flags, "flag_names": _flag_names(flags, LINEDEF_FLAGS),
            "two_sided": bool(flags & ML_TWOSIDED),
            "special": special, "tag": tag,
            "sidedef_right": -1 if right == NO_SIDEDEF else right,
            "sidedef_left": -1 if left == NO_SIDEDEF else left,
        })
    return out


def parse_sidedefs(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 30, "SIDEDEFS")):
        xoff, yoff, upper, lower, middle, sector = \
            struct.unpack_from("<hh8s8s8sH", data, 30 * i)
        out.append({"xoffset": xoff, "yoffset": yoff,
                    "upper": _name(upper), "lower": _name(lower), "middle": _name(middle),
                    "sector": sector})
    return out


def parse_vertexes(data: bytes) -> list[dict[str, int]]:
    out = []
    for i in range(_records(data, 4, "VERTEXES")):
        x, y = struct.unpack_from("<hh", data, 4 * i)
        out.append({"x": x, "y": y})
    return out


def parse_segs(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 12, "SEGS")):
        v1, v2, angle, linedef, direction, offset = \
            struct.unpack_from("<HHHHhh", data, 12 * i)
        out.append({"v1": v1, "v2": v2,
                    "angle": angle,                      # BAM16, unsigned
                    "angle_deg": angle * 360.0 / 65536.0,
                    "linedef": linedef, "direction": direction, "offset": offset})
    return out


def parse_ssectors(data: bytes) -> list[dict[str, int]]:
    out = []
    for i in range(_records(data, 4, "SSECTORS")):
        numsegs, firstseg = struct.unpack_from("<HH", data, 4 * i)
        out.append({"numsegs": numsegs, "firstseg": firstseg})
    return out


def _child(raw: int) -> dict[str, Any]:
    if raw & NF_SUBSECTOR:
        return {"raw": raw, "is_subsector": True, "index": raw & 0x7FFF}
    return {"raw": raw, "is_subsector": False, "index": raw}


def parse_nodes(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 28, "NODES")):
        f = struct.unpack_from("<hhhh" "hhhh" "hhhh" "HH", data, 28 * i)
        x, y, dx, dy = f[0:4]
        rb = f[4:8]
        lb = f[8:12]
        rchild, lchild = f[12:14]
        out.append({
            "x": x, "y": y, "dx": dx, "dy": dy,
            "bbox_right": {"top": rb[0], "bottom": rb[1], "left": rb[2], "right": rb[3]},
            "bbox_left": {"top": lb[0], "bottom": lb[1], "left": lb[2], "right": lb[3]},
            "child_right": _child(rchild),
            "child_left": _child(lchild),
        })
    return out


def parse_sectors(data: bytes) -> list[dict[str, Any]]:
    out = []
    for i in range(_records(data, 26, "SECTORS")):
        floor_h, ceil_h, floor_t, ceil_t, light, special, tag = \
            struct.unpack_from("<hh8s8shhh", data, 26 * i)
        out.append({"floor": floor_h, "ceiling": ceil_h,
                    "floor_tex": _name(floor_t), "ceiling_tex": _name(ceil_t),
                    "light": light, "special": special, "tag": tag})
    return out


def parse_blockmap_header(data: bytes) -> dict[str, int] | None:
    if len(data) < 8:
        return None
    xo, yo, cols, rows = struct.unpack_from("<hhHH", data, 0)
    return {"xorigin": xo, "yorigin": yo, "columns": cols, "rows": rows,
            "bytes": len(data)}


# ---------------------------------------------------------------------------
# derived data useful for a renderer


def add_derived(m: dict[str, Any]) -> None:
    """Attach per-seg front/back sector indices and per-linedef sectors."""
    linedefs, sidedefs = m["linedefs"], m["sidedefs"]
    for ld in linedefs:
        r, l = ld["sidedef_right"], ld["sidedef_left"]
        ld["sector_right"] = sidedefs[r]["sector"] if r >= 0 and r < len(sidedefs) else -1
        ld["sector_left"] = sidedefs[l]["sector"] if l >= 0 and l < len(sidedefs) else -1
    for seg in m["segs"]:
        ld = linedefs[seg["linedef"]]
        if seg["direction"] == 0:
            front, back = ld["sidedef_right"], ld["sidedef_left"]
        else:
            front, back = ld["sidedef_left"], ld["sidedef_right"]
        seg["sidedef"] = front
        seg["front_sector"] = sidedefs[front]["sector"] if front >= 0 else -1
        seg["back_sector"] = sidedefs[back]["sector"] if back >= 0 else -1
    # subsector -> sector (all segs of a subsector share one front sector)
    for ss in m["ssectors"]:
        first = ss["firstseg"]
        ss["sector"] = m["segs"][first]["front_sector"] if ss["numsegs"] else -1


# ---------------------------------------------------------------------------
# statistics


def bsp_depths(nodes: list[dict[str, Any]]) -> tuple[int, float, int, int]:
    """Return (max depth, mean leaf depth, min leaf depth, leaf count).

    Depth = number of NODES traversed from the root to reach a subsector leaf.
    The root is the last node.  Iterative DFS (Python recursion limit safe).
    """
    if not nodes:
        return 0, 0.0, 0, 0
    root = len(nodes) - 1
    total = 0
    leaves = 0
    maxd = 0
    mind = 1 << 30
    stack = [(root, 1)]
    while stack:
        idx, d = stack.pop()
        node = nodes[idx]
        for key in ("child_right", "child_left"):
            c = node[key]
            if c["is_subsector"]:
                leaves += 1
                total += d
                maxd = max(maxd, d)
                mind = min(mind, d)
            else:
                stack.append((c["index"], d + 1))
    return maxd, (total / leaves if leaves else 0.0), (mind if leaves else 0), leaves


def _bits_signed(lo: int, hi: int) -> int:
    """Bits for a two's complement int covering [lo, hi]."""
    n = 1
    while not (-(1 << (n - 1)) <= lo and hi < (1 << (n - 1))):
        n += 1
    return n


def _bits_unsigned(hi: int) -> int:
    return max(1, hi.bit_length())


def compute_stats(m: dict[str, Any]) -> dict[str, Any]:
    v = m["vertexes"]
    ld = m["linedefs"]
    sec = m["sectors"]
    segs = m["segs"]
    ss = m["ssectors"]
    nodes = m["nodes"]

    xs = [p["x"] for p in v] or [0]
    ys = [p["y"] for p in v] or [0]
    one_sided = sum(1 for l in ld if l["sidedef_left"] < 0 or l["sidedef_right"] < 0)
    two_sided_flag = sum(1 for l in ld if l["two_sided"])
    two_sided_geom = sum(1 for l in ld if l["sidedef_left"] >= 0 and l["sidedef_right"] >= 0)
    maxd, avgd, mind, leaves = bsp_depths(nodes)

    floors = [s["floor"] for s in sec] or [0]
    ceils = [s["ceiling"] for s in sec] or [0]
    lights = [s["light"] for s in sec] or [0]
    heights = [s["ceiling"] - s["floor"] for s in sec] or [0]

    starts = [t for t in m["things"] if t["type"] == 1]
    p1 = starts[0] if starts else None

    seg_offsets = [s["offset"] for s in segs] or [0]
    seg_lengths = []
    for s in segs:
        a, b = v[s["v1"]], v[s["v2"]]
        seg_lengths.append(math.hypot(b["x"] - a["x"], b["y"] - a["y"]))
    line_lengths = []
    for l in ld:
        a, b = v[l["v1"]], v[l["v2"]]
        line_lengths.append(math.hypot(b["x"] - a["x"], b["y"] - a["y"]))

    node_vals = []
    bbox_vals = []
    for n in nodes:
        node_vals += [n["x"], n["y"], n["dx"], n["dy"]]
        for k in ("bbox_right", "bbox_left"):
            bbox_vals += list(n[k].values())

    stats = {
        "map": m["map"],
        "counts": {
            "things": len(m["things"]),
            "linedefs": len(ld),
            "sidedefs": len(m["sidedefs"]),
            "vertexes": len(v),
            "segs": len(segs),
            "ssectors": len(ss),
            "nodes": len(nodes),
            "sectors": len(sec),
            "reject_bytes": m["reject_bytes"],
            "blockmap": m["blockmap"],
        },
        "vertex_bbox": {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys),
                        "width": max(xs) - min(xs), "height": max(ys) - min(ys)},
        "linedefs_one_sided": one_sided,
        "linedefs_two_sided": two_sided_geom,
        "linedefs_twosided_flag": two_sided_flag,
        "linedef_len_min": min(line_lengths) if line_lengths else 0,
        "linedef_len_max": max(line_lengths) if line_lengths else 0,
        "bsp_depth_max": maxd,
        "bsp_depth_avg": round(avgd, 3),
        "bsp_depth_min": mind,
        "bsp_leaves": leaves,
        "segs_per_subsector_max": max((s["numsegs"] for s in ss), default=0),
        "segs_per_subsector_avg": round(sum(s["numsegs"] for s in ss) / len(ss), 3) if ss else 0,
        "seg_offset_min": min(seg_offsets), "seg_offset_max": max(seg_offsets),
        "seg_len_min": round(min(seg_lengths), 2) if seg_lengths else 0,
        "seg_len_max": round(max(seg_lengths), 2) if seg_lengths else 0,
        "sectors": len(sec),
        "floor_min": min(floors), "floor_max": max(floors),
        "ceiling_min": min(ceils), "ceiling_max": max(ceils),
        "sector_height_min": min(heights), "sector_height_max": max(heights),
        "light_min": min(lights), "light_max": max(lights),
        "distinct_lights": sorted(set(lights)),
        "node_partition_min": min(node_vals) if node_vals else 0,
        "node_partition_max": max(node_vals) if node_vals else 0,
        "node_bbox_min": min(bbox_vals) if bbox_vals else 0,
        "node_bbox_max": max(bbox_vals) if bbox_vals else 0,
        "player1_start": ({"x": p1["x"], "y": p1["y"], "angle": p1["angle"]} if p1 else None),
        "player_starts": len(starts),
        "thing_types": len(set(t["type"] for t in m["things"])),
    }
    # bit budget hints (for packing into TON cells)
    stats["bits"] = {
        "vertex_index": _bits_unsigned(max(len(v) - 1, 0)),
        "linedef_index": _bits_unsigned(max(len(ld) - 1, 0)),
        "sidedef_index": _bits_unsigned(max(len(m["sidedefs"]) - 1, 0)),
        "seg_index": _bits_unsigned(max(len(segs) - 1, 0)),
        "ssector_index": _bits_unsigned(max(len(ss) - 1, 0)),
        "node_index": _bits_unsigned(max(len(nodes) - 1, 0)),
        "sector_index": _bits_unsigned(max(len(sec) - 1, 0)),
        "vertex_x": _bits_signed(min(xs), max(xs)),
        "vertex_y": _bits_signed(min(ys), max(ys)),
        "seg_offset": _bits_signed(min(seg_offsets), max(seg_offsets)),
        "floor_ceiling": _bits_signed(min(floors + ceils), max(floors + ceils)),
        "light": _bits_unsigned(max(lights)),
        "node_partition": _bits_signed(min(node_vals), max(node_vals)) if node_vals else 0,
        "node_bbox": _bits_signed(min(bbox_vals), max(bbox_vals)) if bbox_vals else 0,
    }
    return stats


def print_counts(stats: dict[str, Any]) -> None:
    c = stats["counts"]
    print(f"map {stats['map']}")
    for k in ("things", "linedefs", "sidedefs", "vertexes", "segs", "ssectors", "nodes", "sectors"):
        print(f"  {k:10s} {c[k]:6d}")
    print(f"  {'reject':10s} {c['reject_bytes']:6d} bytes")
    if c["blockmap"]:
        b = c["blockmap"]
        print(f"  {'blockmap':10s} {b['bytes']:6d} bytes, origin ({b['xorigin']},{b['yorigin']}), "
              f"{b['columns']}x{b['rows']} blocks")


def print_bbox_stats(stats: dict[str, Any]) -> None:
    bb = stats["vertex_bbox"]
    print(f"vertex x range      [{bb['min_x']}, {bb['max_x']}]  (width {bb['width']})")
    print(f"vertex y range      [{bb['min_y']}, {bb['max_y']}]  (height {bb['height']})")
    print(f"linedefs            one-sided {stats['linedefs_one_sided']}, "
          f"two-sided {stats['linedefs_two_sided']} "
          f"(TWOSIDED flag set on {stats['linedefs_twosided_flag']})")
    print(f"linedef length      min {stats['linedef_len_min']:.2f}, max {stats['linedef_len_max']:.2f}")
    print(f"BSP depth           max {stats['bsp_depth_max']}, avg {stats['bsp_depth_avg']}, "
          f"min {stats['bsp_depth_min']} (nodes on root->leaf path; {stats['bsp_leaves']} leaves)")
    print(f"segs per subsector  max {stats['segs_per_subsector_max']}, avg {stats['segs_per_subsector_avg']}")
    print(f"seg offset range    [{stats['seg_offset_min']}, {stats['seg_offset_max']}]")
    print(f"seg length          min {stats['seg_len_min']}, max {stats['seg_len_max']}")
    print(f"sectors             {stats['sectors']}")
    print(f"floor height        [{stats['floor_min']}, {stats['floor_max']}]")
    print(f"ceiling height      [{stats['ceiling_min']}, {stats['ceiling_max']}]")
    print(f"sector height       [{stats['sector_height_min']}, {stats['sector_height_max']}]")
    print(f"light level         [{stats['light_min']}, {stats['light_max']}]  distinct {stats['distinct_lights']}")
    print(f"node partition vals [{stats['node_partition_min']}, {stats['node_partition_max']}]")
    print(f"node bbox vals      [{stats['node_bbox_min']}, {stats['node_bbox_max']}]")
    p1 = stats["player1_start"]
    if p1:
        print(f"player 1 start      x={p1['x']} y={p1['y']} angle={p1['angle']} "
              f"({stats['player_starts']} start thing(s))")
    else:
        print("player 1 start      NOT FOUND")
    print(f"thing types         {stats['thing_types']} distinct")
    print("bit widths needed   " + ", ".join(f"{k}={v}" for k, v in stats["bits"].items()))


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classic Doom WAD map extractor (stdlib only).")
    ap.add_argument("wad", help="path to IWAD/PWAD")
    ap.add_argument("map", nargs="?", help="map name, e.g. E1M1 or MAP01")
    ap.add_argument("--list", action="store_true", help="list maps (and with -v all lumps)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", metavar="FILE", help="write decoded map as JSON")
    ap.add_argument("--compact", action="store_true", help="compact JSON (no indentation)")
    ap.add_argument("--no-derived", action="store_true",
                    help="do not add derived fields (seg front/back sectors etc.)")
    ap.add_argument("--stats", action="store_true", help="print lump counts + geometry/BSP stats")
    ap.add_argument("--bbox", action="store_true", help="print geometry/BSP stats only")
    args = ap.parse_args(argv)

    wad = WAD.open(args.wad)

    if args.list or not args.map:
        print(f"{wad.ident} {args.wad}: {wad.numlumps} lumps, directory at {wad.infotableofs}")
        print("maps:", " ".join(wad.map_names()))
        if args.verbose:
            for l in wad.lumps:
                print(f"  {l.index:5d} {l.name:8s} pos={l.filepos:8d} size={l.size}")
        if not args.map:
            return 0

    m = wad.read_map(args.map, with_derived=not args.no_derived)
    stats = compute_stats(m)

    if args.json:
        m["stats"] = stats
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            if args.compact:
                json.dump(m, f, separators=(",", ":"))
            else:
                json.dump(m, f, indent=1)
        print(f"wrote {args.json} ({os.path.getsize(args.json)} bytes)")

    if args.stats:
        print_counts(stats)
    if args.stats or args.bbox:
        print_bbox_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
