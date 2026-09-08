"""Tiny stdlib-only PNG writer (8-bit grayscale or RGB)."""
import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path: str, width: int, height: int, rows, gray: bool = True) -> None:
    """rows: iterable of bytes-like rows (width bytes for gray, 3*width for RGB)."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    color_type = 0 if gray else 2
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw, 9))
    png += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def write_bitmap_png(path: str, columns, W: int, H: int, scale: int = 4) -> None:
    """columns: list of W ints, bit (H-1-y) of column x is pixel (x,y); 1 = white."""
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            v = 255 if (columns[x] >> (H - 1 - y)) & 1 else 0
            row += bytes([v]) * scale
        for _ in range(scale):
            rows.append(row)
    write_png(path, W * scale, H * scale, rows, gray=True)
