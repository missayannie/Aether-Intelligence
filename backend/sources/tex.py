"""Decode the game's .tex textures to PNG — icons and map tiles, locally.

The client stores UI art as .tex: an 80-byte header (format id, dimensions)
followed by mip payloads. The formats that actually occur in the sheets this
app renders are a small set: raw BGRA variants and the BC1/BC2/BC3/BC4 block
compressions. Anything newer (BC5/BC7) returns None and the caller falls back
to XIVAPI's rendered PNG — decode what's cheap, never break on what isn't.

Everything is pure stdlib. The PNG writer emits filter-0 scanlines through
zlib — no Pillow, no numpy; a 2048² map decodes in seconds and is disk-cached
after its first view.
"""
from __future__ import annotations

import struct
import zlib

# .tex format ids (the client's TextureFormat enum).
FMT_L8 = 0x1130
FMT_A8 = 0x1131
FMT_B4G4R4A4 = 0x1440
FMT_B5G5R5A1 = 0x1441
FMT_B8G8R8A8 = 0x1450
FMT_B8G8R8X8 = 0x1451
FMT_BC1 = 0x3420
FMT_BC2 = 0x3430
FMT_BC3 = 0x3431
FMT_BC4 = 0x6120


def parse_header(raw: bytes) -> tuple[int, int, int, int]:
    """(format, width, height, header_size). Header is 80 bytes."""
    fmt = struct.unpack_from("<I", raw, 4)[0]
    w, h = struct.unpack_from("<HH", raw, 8)
    return fmt, w, h, 80


def decode_rgba(raw: bytes) -> tuple[bytes, int, int] | None:
    """Whole .tex (header + mip0) -> (RGBA bytes, w, h), or None if the
    format isn't one we decode."""
    fmt, w, h, hdr = parse_header(raw)
    data = raw[hdr:]
    if fmt in (FMT_B8G8R8A8, FMT_B8G8R8X8):
        n = w * h * 4
        src = data[:n]
        out = bytearray(n)
        out[0::4] = src[2::4]
        out[1::4] = src[1::4]
        out[2::4] = src[0::4]
        out[3::4] = src[3::4] if fmt == FMT_B8G8R8A8 else b"\xff" * (n // 4)
        return bytes(out), w, h
    if fmt == FMT_B4G4R4A4:
        return _decode_b4g4r4a4(data, w, h), w, h
    if fmt == FMT_B5G5R5A1:
        return _decode_b5g5r5a1(data, w, h), w, h
    if fmt in (FMT_L8, FMT_A8):
        out = bytearray(w * h * 4)
        src = data[:w * h]
        if fmt == FMT_L8:
            out[0::4] = src
            out[1::4] = src
            out[2::4] = src
            out[3::4] = b"\xff" * (w * h)
        else:
            out[3::4] = src
        return bytes(out), w, h
    if fmt == FMT_BC1:
        return _decode_bc1(data, w, h), w, h
    if fmt == FMT_BC2:
        return _decode_bc23(data, w, h, bc3=False), w, h
    if fmt == FMT_BC3:
        return _decode_bc23(data, w, h, bc3=True), w, h
    if fmt == FMT_BC4:
        return _decode_bc4(data, w, h), w, h
    return None


def _decode_b4g4r4a4(data: bytes, w: int, h: int) -> bytes:
    out = bytearray(w * h * 4)
    vals = struct.unpack_from(f"<{w * h}H", data)
    for i, v in enumerate(vals):
        j = i * 4
        out[j] = ((v >> 8) & 0xF) * 17       # R
        out[j + 1] = ((v >> 4) & 0xF) * 17   # G
        out[j + 2] = (v & 0xF) * 17          # B
        out[j + 3] = ((v >> 12) & 0xF) * 17  # A
    return bytes(out)


def _decode_b5g5r5a1(data: bytes, w: int, h: int) -> bytes:
    out = bytearray(w * h * 4)
    vals = struct.unpack_from(f"<{w * h}H", data)
    for i, v in enumerate(vals):
        j = i * 4
        out[j] = ((v >> 10) & 0x1F) * 255 // 31
        out[j + 1] = ((v >> 5) & 0x1F) * 255 // 31
        out[j + 2] = (v & 0x1F) * 255 // 31
        out[j + 3] = 255 if v & 0x8000 else 0
    return bytes(out)


def _rgb565(v: int) -> tuple[int, int, int]:
    return ((v >> 11) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63,
            (v & 0x1F) * 255 // 31)


def _decode_bc1(data: bytes, w: int, h: int, out: bytearray | None = None,
                alpha: bool = True) -> bytes:
    """BC1/DXT1: 4x4 blocks, two RGB565 endpoints + 2-bit indices."""
    out = out if out is not None else bytearray(w * h * 4)
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            c0, c1, bits = struct.unpack_from("<HHI", data, pos)
            pos += 8
            r0, g0, b0 = _rgb565(c0)
            r1, g1, b1 = _rgb565(c1)
            if c0 > c1:
                pal = ((r0, g0, b0, 255), (r1, g1, b1, 255),
                       ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
                       ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255))
            else:
                pal = ((r0, g0, b0, 255), (r1, g1, b1, 255),
                       ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255),
                       (0, 0, 0, 0 if alpha else 255))
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                row = (y * w + bx * 4) * 4
                for px in range(4):
                    if bx * 4 + px >= w:
                        break
                    r, g, b, a = pal[(bits >> ((py * 4 + px) * 2)) & 3]
                    j = row + px * 4
                    out[j] = r
                    out[j + 1] = g
                    out[j + 2] = b
                    if alpha:
                        out[j + 3] = a
    if alpha:
        return bytes(out)
    return bytes(out)


def _bc_alpha_table(a0: int, a1: int) -> tuple:
    if a0 > a1:
        return (a0, a1, (6 * a0 + a1) // 7, (5 * a0 + 2 * a1) // 7,
                (4 * a0 + 3 * a1) // 7, (3 * a0 + 4 * a1) // 7,
                (2 * a0 + 5 * a1) // 7, (a0 + 6 * a1) // 7)
    return (a0, a1, (4 * a0 + a1) // 5, (3 * a0 + 2 * a1) // 5,
            (2 * a0 + 3 * a1) // 5, (a0 + 4 * a1) // 5, 0, 255)


def _decode_bc23(data: bytes, w: int, h: int, bc3: bool) -> bytes:
    """BC2 (explicit 4-bit alpha) / BC3 (interpolated alpha) + BC1 color."""
    out = bytearray(w * h * 4)
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            ablock = data[pos:pos + 8]
            pos += 8
            c0, c1, bits = struct.unpack_from("<HHI", data, pos)
            pos += 8
            r0, g0, b0 = _rgb565(c0)
            r1, g1, b1 = _rgb565(c1)
            pal = ((r0, g0, b0), (r1, g1, b1),
                   ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                   ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3))
            if bc3:
                atab = _bc_alpha_table(ablock[0], ablock[1])
                abits = int.from_bytes(ablock[2:8], "little")
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                row = (y * w + bx * 4) * 4
                for px in range(4):
                    if bx * 4 + px >= w:
                        break
                    t = py * 4 + px
                    r, g, b = pal[(bits >> (t * 2)) & 3]
                    if bc3:
                        a = atab[(abits >> (t * 3)) & 7]
                    else:
                        nib = (ablock[t // 2] >> (4 * (t % 2))) & 0xF
                        a = nib * 17
                    j = row + px * 4
                    out[j] = r
                    out[j + 1] = g
                    out[j + 2] = b
                    out[j + 3] = a
    return bytes(out)


def _decode_bc4(data: bytes, w: int, h: int) -> bytes:
    """BC4: one interpolated channel — rendered as opaque grayscale."""
    out = bytearray(w * h * 4)
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            tab = _bc_alpha_table(data[pos], data[pos + 1])
            bits = int.from_bytes(data[pos + 2:pos + 8], "little")
            pos += 8
            for py in range(4):
                y = by * 4 + py
                if y >= h:
                    break
                row = (y * w + bx * 4) * 4
                for px in range(4):
                    if bx * 4 + px >= w:
                        break
                    v = tab[(bits >> ((py * 4 + px) * 3)) & 7]
                    j = row + px * 4
                    out[j] = v
                    out[j + 1] = v
                    out[j + 2] = v
                    out[j + 3] = 255
    return bytes(out)


def multiply(base: bytes, mask: bytes) -> bytes:
    """Per-channel multiply blend — how the game composes a field map's base
    texture with its terrain mask. Alpha forced opaque. Single pass: a 2048²
    map is 16M channel ops, and per-channel generator passes tripled the cost."""
    out = bytearray(len(base))
    for i in range(0, len(base), 4):
        out[i] = base[i] * mask[i] // 255
        out[i + 1] = base[i + 1] * mask[i + 1] // 255
        out[i + 2] = base[i + 2] * mask[i + 2] // 255
        out[i + 3] = 255
    return bytes(out)


def to_png(rgba: bytes, w: int, h: int) -> bytes:
    """Minimal PNG encoder: 8-bit RGBA, filter 0, one IDAT."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body)))

    stride = w * 4
    scanlines = bytearray()
    for y in range(h):
        scanlines.append(0)
        scanlines += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
            + chunk(b"IEND", b""))
