"""Read FFXIV's own database straight from the installed game client.

The client ships its data in SqPack archives (game/sqpack/ffxiv/*.index2 + .dat*),
and the relational sheets — Item, PlaceName, GatheringPoint, every table the
game itself runs on — as EXH/EXD files inside category 0a0000. This module is a
pure-stdlib reader for exactly that chain:

    index2 (path hash -> dat offset) -> dat entry (zlib blocks) -> EXH/EXD sheets

Nothing here is Garland- or XIVAPI-shaped; it returns raw decoded rows. The
COLUMN MEANINGS ("column 9 of Item is the name") live in exd_schema.json and
gameclient.py — this file only knows the container formats, which are stable
and self-describing (the EXH header carries every column's type and offset).

Format notes, hard-won and worth keeping:
  - Path hash: bitwise NOT of zlib crc32 over the LOWERCASED full path. (Both
    variants are tried on lookup, so a format doc being wrong here degrades to
    a miss, not a wrong file.)
  - index2 locator u32: bit0 synonym flag, bits1-3 dat file number,
    remaining bits (low nibble masked) * 8 = byte offset into the dat.
  - Dat entry blocks are raw-deflate (wbits=-15); a "compressed length" of
    32000 marks a stored (uncompressed) block.
  - EXH/EXD integers are BIG-endian; the SqPack/index layer is little-endian.
  - EXD strings are SeStrings: UTF-8 with 0x02 <type> <len> ... 0x03 macro
    payloads embedded (colors, conditionals, icons). sestring_to_text() strips
    the payloads and keeps the prose.
"""
from __future__ import annotations

import mmap
import struct
import zlib
from pathlib import Path

# EXD language suffixes (Language enum in the client).
LANG_SUFFIX = {0: "", 1: "ja", 2: "en", 3: "de", 4: "fr", 5: "chs", 6: "cht", 7: "ko"}

# Column types (EXH). 0x19.. are packed bools: bit index = type - 0x19.
COL_STRING = 0x0
COL_BOOL = 0x1
_INT_FMTS = {0x2: ">b", 0x3: ">B", 0x4: ">h", 0x5: ">H", 0x6: ">i", 0x7: ">I",
             0xA: ">q", 0xB: ">Q"}
COL_FLOAT = 0x9
_PACKED_BOOL_FIRST = 0x19


def ffxiv_hash(path: str) -> int:
    """The game's path hash: ~crc32 of the lowercase path."""
    return (~zlib.crc32(path.lower().encode("utf-8"))) & 0xFFFFFFFF


def sestring_to_text(raw: bytes) -> str:
    """Flatten a SeString to plain prose: keep text, drop macro payloads.

    0x02 starts a macro: <type byte> <payload length as SeString-packed int>
    <payload> 0x03. NewLine (type 0x10) becomes '\\n'; everything else is
    dropped whole — the visible prose sits BETWEEN macros, not inside them,
    for every sheet this app reads (item/action/status descriptions).
    """
    out = []
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if b != 0x02:
            out.append(b)
            i += 1
            continue
        if i + 2 >= n:
            break
        mtype = raw[i + 1]
        plen, j = _packed_int(raw, i + 2)
        i = j + plen
        if i < n and raw[i] == 0x03:
            i += 1
        if mtype == 0x10:          # NewLine
            out.append(0x0A)
        elif mtype == 0x16:        # SoftHyphen — drop, words rejoin
            pass
    return bytes(out).decode("utf-8", "replace")


def _packed_int(buf: bytes, i: int) -> tuple[int, int]:
    """SeString packed integer: one byte when < 0xF0, else a marker whose low
    bits say which of the next 4 (big-endian) bytes are present."""
    b = buf[i]
    i += 1
    if b < 0xF0:
        return b - 1, i
    flags = (b + 1) & 0xF
    v = 0
    for bit in (8, 4, 2, 1):
        v <<= 8
        if flags & bit:
            v |= buf[i]
            i += 1
    return v, i


class SqPackReader:
    """One SqPack category (e.g. 0a0000 = EXD) in one repository directory."""

    def __init__(self, repo_dir: Path, category: str):
        self.repo_dir = repo_dir
        self.category = category
        self._dats: dict[int, mmap.mmap] = {}
        self._dat_files: list = []      # keep file objects alive for the mmaps
        idx_path = repo_dir / f"{category}.win32.index2"
        raw = idx_path.read_bytes()
        # SqPack header size at 0x0C; the index header follows; its files
        # segment (offset, size) sits at +0x08/+0x0C.
        sq_size = struct.unpack_from("<I", raw, 0x0C)[0]
        seg_off, seg_size = struct.unpack_from("<II", raw, sq_size + 0x08)
        self._entries: dict[int, int] = {}
        for pos in range(seg_off, seg_off + seg_size, 8):
            h, data = struct.unpack_from("<II", raw, pos)
            self._entries[h] = data

    def _dat(self, n: int) -> mmap.mmap:
        if n not in self._dats:
            f = open(self.repo_dir / f"{self.category}.win32.dat{n}", "rb")
            self._dat_files.append(f)
            self._dats[n] = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        return self._dats[n]

    def exists(self, path: str) -> bool:
        return (ffxiv_hash(path) in self._entries
                or (zlib.crc32(path.lower().encode()) & 0xFFFFFFFF) in self._entries)

    def _locate(self, path: str) -> tuple | None:
        data = self._entries.get(ffxiv_hash(path))
        if data is None:
            data = self._entries.get(zlib.crc32(path.lower().encode()) & 0xFFFFFFFF)
        if data is None:
            return None
        buf = self._dat((data & 0b1110) >> 1)
        return buf, (data & ~0xF) * 8

    def read_texture(self, path: str) -> bytes | None:
        """A content-type-4 entry reassembled into plain .tex bytes: the raw
        80-byte texture header (stored uncompressed between the entry header
        and the first mip) followed by mip 0's decompressed payload. Only the
        top mip — the app renders icons and maps at full size."""
        loc = self._locate(path)
        if loc is None:
            return None
        buf, offset = loc
        hdr_len, content_type = struct.unpack_from("<II", buf, offset)
        if content_type != 4:
            raise ValueError(f"not a texture entry ({content_type}) for {path}")
        num_lods = struct.unpack_from("<I", buf, offset + 0x14)[0]
        comp_off, _comp_size, _decomp, block_off, block_count = struct.unpack_from(
            "<5I", buf, offset + 24)
        # Per-block compressed sizes: a u16 table after the lod entries — the
        # cursor advances by table entries, not by the block headers.
        sizes_base = offset + 24 + num_lods * 20
        out = bytearray(buf[offset + hdr_len: offset + hdr_len + comp_off])
        cur = offset + hdr_len + comp_off
        for i in range(block_count):
            bhdr, _, comp_len, uncomp_len = struct.unpack_from("<IIII", buf, cur)
            start = cur + bhdr
            if comp_len >= 32000:
                out += buf[start:start + uncomp_len]
            else:
                out += zlib.decompress(buf[start:start + comp_len], -15)
            cur += struct.unpack_from("<H", buf, sizes_base + (block_off + i) * 2)[0]
        return bytes(out)

    def read(self, path: str) -> bytes | None:
        loc = self._locate(path)
        if loc is None:
            return None
        buf, offset = loc
        hdr_len, content_type = struct.unpack_from("<II", buf, offset)
        if content_type != 2:   # 2 = binary; textures go through read_texture
            raise ValueError(f"content type {content_type} unsupported ({path})")
        num_blocks = struct.unpack_from("<I", buf, offset + 0x14)[0]
        out = bytearray()
        for i in range(num_blocks):
            boff, csize, usize = struct.unpack_from("<IHH", buf, offset + 0x18 + i * 8)
            base = offset + hdr_len + boff
            bhdr, _, comp_len, uncomp_len = struct.unpack_from("<IIII", buf, base)
            start = base + bhdr
            if comp_len >= 32000:     # stored, not compressed
                out += buf[start:start + uncomp_len]
            else:
                out += zlib.decompress(buf[start:start + comp_len], -15)
        return bytes(out)


class Sheet:
    """One EXD sheet: pages and languages resolved, rows decoded on demand."""

    def __init__(self, pack: SqPackReader, name: str, lang: str = "en"):
        self.name = name
        self._pack = pack
        raw = pack.read(f"exd/{name.lower()}.exh")
        if raw is None:
            raise KeyError(f"no such sheet: {name}")
        assert raw[:4] == b"EXHF", f"bad EXH magic for {name}"
        self.row_size, col_count, page_count, lang_count = struct.unpack_from(">HHHH", raw, 6)
        self.variant = raw[0x11]            # 1 = rows, 2 = subrows
        self.row_count = struct.unpack_from(">I", raw, 0x14)[0]
        self.columns = [struct.unpack_from(">HH", raw, 0x20 + i * 4)
                        for i in range(col_count)]          # (type, offset)
        pbase = 0x20 + col_count * 4
        self.pages = [struct.unpack_from(">II", raw, pbase + i * 8)
                      for i in range(page_count)]           # (start, count)
        lbase = pbase + page_count * 8
        langs = [struct.unpack_from("<H", raw, lbase + i * 2)[0] for i in range(lang_count)]
        # Monolingual sheets (lang 0) have no suffix; else pick the wanted one.
        self._suffix = "" if langs == [0] else LANG_SUFFIX.get(
            2 if 2 in langs else langs[0], "en")
        if lang and self._suffix and lang in LANG_SUFFIX.values():
            self._suffix = lang if any(LANG_SUFFIX.get(l) == lang for l in langs) else self._suffix
        self._page_cache: dict[int, tuple[bytes, dict[int, int]]] = {}

    # ---- internals ----

    def _page(self, start: int) -> tuple[bytes, dict[int, int]]:
        if start not in self._page_cache:
            suffix = f"_{self._suffix}" if self._suffix else ""
            raw = self._pack.read(f"exd/{self.name.lower()}_{start}{suffix}.exd")
            if raw is None:
                raise KeyError(f"missing page {start} of {self.name}")
            assert raw[:4] == b"EXDF"
            index_size = struct.unpack_from(">I", raw, 0x08)[0]
            offsets = {}
            for i in range(index_size // 8):
                row_id, off = struct.unpack_from(">II", raw, 0x20 + i * 8)
                offsets[row_id] = off
            self._page_cache[start] = (raw, offsets)
        return self._page_cache[start]

    def _decode(self, raw: bytes, fixed: int, cols=None) -> list:
        str_base = fixed + self.row_size
        out = []
        for idx in (cols if cols is not None else range(len(self.columns))):
            ctype, coff = self.columns[idx]
            pos = fixed + coff
            if ctype == COL_STRING:
                rel = struct.unpack_from(">I", raw, pos)[0]
                end = raw.index(b"\0", str_base + rel)
                out.append(sestring_to_text(raw[str_base + rel:end]))
            elif ctype == COL_BOOL:
                out.append(raw[pos] != 0)
            elif ctype in _INT_FMTS:
                out.append(struct.unpack_from(_INT_FMTS[ctype], raw, pos)[0])
            elif ctype == COL_FLOAT:
                out.append(struct.unpack_from(">f", raw, pos)[0])
            elif ctype >= _PACKED_BOOL_FIRST:
                out.append(bool(raw[pos] & (1 << (ctype - _PACKED_BOOL_FIRST))))
            else:
                out.append(None)
        return out

    # ---- public ----

    def row(self, row_id: int, cols=None) -> list | None:
        """Decoded column values for one row (variant 1 sheets)."""
        for start, count in self.pages:
            if start <= row_id < start + count:
                raw, offsets = self._page(start)
                off = offsets.get(row_id)
                if off is None:
                    return None
                return self._decode(raw, off + 6, cols)
        return None

    def subrows(self, row_id: int, cols=None) -> list[list]:
        """All subrows of one row (variant 2 sheets)."""
        if self.variant != 2:
            raise ValueError(f"{self.name} is not a subrow sheet")
        for start, count in self.pages:
            if start <= row_id < start + count:
                raw, offsets = self._page(start)
                off = offsets.get(row_id)
                if off is None:
                    return []
                n_sub = struct.unpack_from(">H", raw, off + 4)[0]
                out = []
                pos = off + 6
                for _ in range(n_sub):
                    out.append(self._decode(raw, pos + 2, cols))  # skip u16 subrow id
                    pos += 2 + self.row_size
                return out
        return []

    def rows(self, cols=None):
        """Iterate (row_id, values) across all pages. Pass cols (column index
        list) to decode only what you need — the difference between seconds
        and minutes when indexing the 52k-row Item sheet."""
        for start, count in self.pages:
            raw, offsets = self._page(start)
            for row_id in sorted(offsets):
                yield row_id, self._decode(raw, offsets[row_id] + 6, cols)


class GameData:
    """The installed client's data. One instance per game directory."""

    def __init__(self, game_dir: Path):
        self.game_dir = Path(game_dir)
        self._packs: dict[str, SqPackReader] = {}
        self._sheets: dict[tuple[str, str], Sheet] = {}

    def _pack(self, category: str) -> SqPackReader:
        if category not in self._packs:
            self._packs[category] = SqPackReader(
                self.game_dir / "sqpack" / "ffxiv", category)
        return self._packs[category]

    def file(self, path: str) -> bytes | None:
        """Read any file from category 0a (EXD). Other categories on demand."""
        return self._pack("0a0000").read(path)

    def texture(self, path: str) -> bytes | None:
        """A ui/ texture (category 06) as plain .tex bytes, or None."""
        try:
            return self._pack("060000").read_texture(path)
        except (KeyError, ValueError):
            return None

    def sheet(self, name: str, lang: str = "en") -> Sheet:
        key = (name.lower(), lang)
        if key not in self._sheets:
            self._sheets[key] = Sheet(self._pack("0a0000"), name, lang)
        return self._sheets[key]

    def sheet_names(self) -> list[str]:
        root = self.file("exd/root.exl") or b""
        return [line.split(b",")[0].decode() for line in root.splitlines()[1:] if line]

    def version(self) -> str:
        """The installed client version — THE patch signal. ffxivgame.ver flips
        the moment the launcher finishes patching; expansion .ver files are
        appended so an ex-only patch still changes the stamp."""
        parts = []
        base = self.game_dir / "ffxivgame.ver"
        if base.exists():
            parts.append(base.read_text().strip())
        for ex in sorted((self.game_dir / "sqpack").glob("ex*/")):
            v = ex / f"{ex.name}.ver"
            if v.exists():
                parts.append(v.read_text().strip())
        return "+".join(parts)
