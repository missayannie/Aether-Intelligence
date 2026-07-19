"""Proof of concept: read FFXIV's own database (EXD sheets) straight from the
installed game client, no Garland/XIVAPI. Ground truth to validate against:
PlaceName row 40 = "Ul'dah - Steps of Nald", 271 = "Horizon", 223 = "Aleport".
"""
import struct
import zlib
from pathlib import Path

GAME = Path(r"C:\Program Files (x86)\SquareEnix\FINAL FANTASY XIV - A Realm Reborn\game\sqpack\ffxiv")


def ffxiv_hash(path: str) -> int:
    """FFXIV path hash = bitwise NOT of standard crc32 (try both variants)."""
    c = zlib.crc32(path.lower().encode("utf-8")) & 0xFFFFFFFF
    return (~c) & 0xFFFFFFFF


class SqPack:
    def __init__(self, category: str):
        self.index2 = (GAME / f"{category}.win32.index2").read_bytes()
        self.dats = {}
        self.category = category
        # SqPack header: size at 0x0C; then segment header right after.
        sq_size = struct.unpack_from("<I", self.index2, 0x0C)[0]
        # Index header: u32 size, then segment 1 (files): u32 count?, u32 offset, u32 size
        ih = sq_size
        self.entries = {}
        seg1_off = struct.unpack_from("<I", self.index2, ih + 0x08)[0]
        seg1_size = struct.unpack_from("<I", self.index2, ih + 0x0C)[0]
        for pos in range(seg1_off, seg1_off + seg1_size, 8):
            h, data = struct.unpack_from("<II", self.index2, pos)
            self.entries[h] = data

    def _dat(self, n: int) -> bytes:
        if n not in self.dats:
            self.dats[n] = (GAME / f"{self.category}.win32.dat{n}").read_bytes()
        return self.dats[n]

    def read(self, path: str) -> bytes | None:
        h = ffxiv_hash(path)
        data = self.entries.get(h)
        if data is None:
            # try the un-complemented crc32 too
            h2 = zlib.crc32(path.lower().encode()) & 0xFFFFFFFF
            data = self.entries.get(h2)
            if data is None:
                return None
        dat_id = (data & 0b1110) >> 1
        offset = (data & ~0xF) * 8
        buf = self._dat(dat_id)
        hdr_len, content_type, uncomp_size = struct.unpack_from("<III", buf, offset)
        if content_type != 2:   # 2 = binary
            raise ValueError(f"unsupported content type {content_type} for {path}")
        num_blocks = struct.unpack_from("<I", buf, offset + 0x14)[0]
        out = bytearray()
        for i in range(num_blocks):
            boff, csize, usize = struct.unpack_from("<IHH", buf, offset + 0x18 + i * 8)
            base = offset + hdr_len + boff
            bhdr, _, comp_len, uncomp_len = struct.unpack_from("<IIII", buf, base)
            payload = buf[base + bhdr: base + bhdr + (uncomp_len if comp_len >= 32000 else comp_len)]
            if comp_len >= 32000:
                out += payload
            else:
                out += zlib.decompress(payload, -15)
        return bytes(out)


def read_exh(raw: bytes):
    assert raw[:4] == b"EXHF", raw[:4]
    row_size, col_count, page_count, lang_count = struct.unpack_from(">HHHH", raw, 6)
    variant = raw[0x11]
    row_count = struct.unpack_from(">I", raw, 0x14)[0]
    cols = [struct.unpack_from(">HH", raw, 0x20 + i * 4) for i in range(col_count)]
    pbase = 0x20 + col_count * 4
    pages = [struct.unpack_from(">II", raw, pbase + i * 8) for i in range(page_count)]
    lbase = pbase + page_count * 8
    langs = [struct.unpack_from("<H", raw, lbase + i * 2)[0] for i in range(lang_count)]
    return {"row_size": row_size, "cols": cols, "pages": pages, "langs": langs,
            "variant": variant, "row_count": row_count}


def read_exd_rows(raw: bytes, exh) -> dict:
    assert raw[:4] == b"EXDF", raw[:4]
    index_size = struct.unpack_from(">I", raw, 0x08)[0]
    rows = {}
    for i in range(index_size // 8):
        row_id, off = struct.unpack_from(">II", raw, 0x20 + i * 8)
        rows[row_id] = off
    return rows


def row_strings(raw: bytes, off: int, exh) -> list[str]:
    """All string columns of a (variant 1) row."""
    data_size, _sub = struct.unpack_from(">IH", raw, off)
    fixed = off + 6
    str_base = fixed + exh["row_size"]
    out = []
    for ctype, coff in exh["cols"]:
        if ctype == 0:  # string
            rel = struct.unpack_from(">I", raw, fixed + coff)[0]
            end = raw.index(b"\0", str_base + rel)
            out.append(raw[str_base + rel:end].decode("utf-8", "replace"))
    return out


sq = SqPack("0a0000")
root = sq.read("exd/root.exl")
print("root.exl:", "OK," if root else "MISS,", len(root or b""), "bytes,",
      len((root or b'').splitlines()), "sheets listed")

exh_raw = sq.read("exd/placename.exh")
exh = read_exh(exh_raw)
print("placename.exh:", exh["row_count"], "rows,", len(exh["cols"]), "cols, langs", exh["langs"])

exd_raw = sq.read("exd/placename_0_en.exd")
rows = read_exd_rows(exd_raw, exh)
for rid, want in [(40, "Ul'dah - Steps of Nald"), (271, "Horizon"), (223, "Aleport")]:
    got = row_strings(exd_raw, rows[rid], exh)
    ok = any(want in s for s in got)
    print(f"row {rid}: {'OK ' if ok else 'FAIL'} {got[:2]}")
