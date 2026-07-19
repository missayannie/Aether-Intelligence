"""Read the game's own GEARSET.DAT — gear for EVERY job, not just the last logout.

This is the answer to the Lodestone's central limitation. The Lodestone only ever
publishes the set you logged out wearing, so a Ninja question after a Botanist logout
is unanswerable from it. The game, meanwhile, saves every gearset you've made to a
file in your own Documents folder, and updates it the moment you save a set.

We only READ that file, and only while it sits on disk. No game process is touched,
no memory is read, nothing is injected or modified — this is the same data Square
Enix's own "Back Up Character Settings" feature uploads.

FORMAT (reverse-engineered and verified against a live file):
    - 0x10-byte header; u32 @0x04 is the payload length
    - the payload is XOR-masked with 0x73 (the giveaway is long runs of 0x73, i.e.
      masked zeros)
    - each gearset record:
        +0x06  name (NUL-terminated)
        +0x36  ClassJob id (30 = Ninja)
        +0x3d  14 item slots, 28 bytes apart, u32 item id first in each
               (ids >= 1_000_000 are the HQ variant of id-1_000_000)
    - the file holds fixed slots for every gearset; unused ones are zeroed

Records are located by SIGNATURE rather than a hardcoded stride. A stride is a guess
about a format we don't control; a signature either matches or doesn't. If a patch
moves things, this finds nothing and the caller falls back to the Lodestone — it
never reads garbage at a wrong offset and reports it as your gear.

Item ids are resolved to names elsewhere (sources/garland): this file knows only ids.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

MASK = 0x73
HEADER = 0x10

# Slot order within a record, 28 bytes apart from +0x3d. Verified live: MainHand held
# the weapon and Head held the head piece, matching both the Lodestone scrape and an
# in-game screenshot. "Waist" is vestigial — belts were removed from the game and the
# slot now always reads 0.
SLOTS = ["MainHand", "OffHand", "Head", "Body", "Hands", "Waist", "Legs", "Feet",
         "Ears", "Neck", "Wrists", "Ring1", "Ring2", "SoulCrystal"]
ITEMS_AT = 0x3D
ITEM_STRIDE = 28
NAME_AT = 0x06
JOB_AT = 0x36
HQ_OFFSET = 1_000_000

# ClassJob ids. Hardcoded rather than fetched: it's stable, and a gearset reader that
# needs the network to name a job would be useless offline — which is half the point.
JOB_BY_ID = {
    1: "Gladiator", 2: "Pugilist", 3: "Marauder", 4: "Lancer", 5: "Archer",
    6: "Conjurer", 7: "Thaumaturge", 8: "Carpenter", 9: "Blacksmith", 10: "Armorer",
    11: "Goldsmith", 12: "Leatherworker", 13: "Weaver", 14: "Alchemist",
    15: "Culinarian", 16: "Miner", 17: "Botanist", 18: "Fisher", 19: "Paladin",
    20: "Monk", 21: "Warrior", 22: "Dragoon", 23: "Bard", 24: "White Mage",
    25: "Black Mage", 26: "Arcanist", 27: "Summoner", 28: "Scholar", 29: "Rogue",
    30: "Ninja", 31: "Machinist", 32: "Dark Knight", 33: "Astrologian",
    34: "Samurai", 35: "Red Mage", 36: "Blue Mage", 37: "Gunbreaker", 38: "Dancer",
    39: "Reaper", 40: "Sage", 41: "Viper", 42: "Pictomancer",
}

GAME_DIR = "FINAL FANTASY XIV - A Realm Reborn"


@dataclass
class Gearset:
    name: str
    job_id: int
    job: str
    # slot -> item id (only slots that hold something)
    items: dict = field(default_factory=dict)


def settings_roots() -> list[Path]:
    """Every plausible "My Games/FINAL FANTASY XIV" directory, newest first.

    The documented path (Documents/My Games/...) is frequently WRONG: OneDrive
    redirects Documents, and leaves stale copies behind. On the machine this was
    written against, the real folder was under OneDrive/Documents(1)/ and a
    two-year-old copy sat in OneDrive/Documents_1/. So search, then pick by mtime —
    never trust the canonical path.
    """
    home = Path.home()
    candidates: list[Path] = []
    for parent in (home, home / "OneDrive"):
        if not parent.exists():
            continue
        for docs in parent.glob("Documents*"):
            p = docs / "My Games" / GAME_DIR
            if p.is_dir():
                candidates.append(p)
    p = home / "Documents" / "My Games" / GAME_DIR
    if p.is_dir() and p not in candidates:
        candidates.append(p)
    return sorted(candidates, key=_mtime, reverse=True)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def gearset_files() -> list[Path]:
    """Every FFXIV_CHR*/GEARSET.DAT we can see, most recently written first."""
    out: list[Path] = []
    for root in settings_roots():
        for chr_dir in root.glob("FFXIV_CHR*"):
            f = chr_dir / "GEARSET.DAT"
            if f.is_file():
                out.append(f)
    return sorted(out, key=_mtime, reverse=True)


def _decode(raw: bytes) -> bytes:
    """Strip the header and lift the 0x73 mask off the payload."""
    if len(raw) <= HEADER:
        return b""
    size = struct.unpack("<I", raw[4:8])[0]
    payload = raw[HEADER:HEADER + size] if 0 < size <= len(raw) - HEADER else raw[HEADER:]
    return bytes(c ^ MASK for c in payload)


def _read_u32(buf: bytes, off: int) -> int:
    if off + 4 > len(buf):
        return 0
    return struct.unpack("<I", buf[off:off + 4])[0]


def _parse_record(dec: bytes, off: int) -> Gearset | None:
    """One record at `off`, or None if it doesn't look like a gearset."""
    job_id = dec[off + JOB_AT] if off + JOB_AT < len(dec) else 0
    if job_id not in JOB_BY_ID:
        return None
    raw_name = dec[off + NAME_AT: off + NAME_AT + 47].split(b"\x00")[0]
    if not raw_name or not all(32 <= c < 127 for c in raw_name):
        return None
    items: dict = {}
    for i, slot in enumerate(SLOTS):
        v = _read_u32(dec, off + ITEMS_AT + i * ITEM_STRIDE)
        if v >= HQ_OFFSET:
            v -= HQ_OFFSET
        if 0 < v < 500_000:
            items[slot] = v
    # Require several real slots. One stray plausible id is coincidence; a weapon
    # plus armour plus accessories is a gearset.
    if len(items) < 3:
        return None
    return Gearset(name=raw_name.decode("utf-8", "replace"), job_id=job_id,
                   job=JOB_BY_ID[job_id], items=items)


def parse(path: Path) -> list[Gearset]:
    """Every gearset in one GEARSET.DAT."""
    try:
        dec = _decode(path.read_bytes())
    except OSError:
        return []
    out: list[Gearset] = []
    off, end = 0, len(dec) - (ITEMS_AT + len(SLOTS) * ITEM_STRIDE)
    while off < end:
        rec = _parse_record(dec, off)
        if rec:
            out.append(rec)
            off += ITEMS_AT + len(SLOTS) * ITEM_STRIDE   # skip past this record's body
        else:
            off += 1
    return out


def read_local(path: Path | None = None) -> tuple[list[Gearset], Path | None]:
    """Gearsets from the most recently written character file. ([], None) if absent.

    An empty result is normal, not an error: the game may not be installed here, or
    the player may keep no gearsets. Callers fall back to the Lodestone.
    """
    files = [path] if path else gearset_files()
    for f in files:
        sets = parse(f)
        if sets:
            return sets, f
    return [], (files[0] if files else None)
