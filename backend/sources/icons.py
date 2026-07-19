"""The agent's map-icon vocabulary.

A curated dictionary of the game's own UI icons (the set the in-game map and
the community wikis' "Dictionary of Icons" pages catalogue), keyed by short
semantic names the agent can use:

  - on the interactive map: pin_on_map(icon=..., area_radius=...)
  - inline in chat/doc markdown: ![](icon:mining)

Ids are game icon ids; the PNGs come through the existing /map/icon disk-cached
XIVAPI proxy, so nothing here adds a new upstream dependency. Names were
verified against the game's MapSymbol sheet + a visual check of each icon.
"""
from __future__ import annotations

# name -> (icon id, human label)
ICONS: dict[str, tuple[int, str]] = {
    # Travel & town
    "aetheryte":       (60453, "Aetheryte"),
    "aethernet":       (60430, "Aethernet shard"),
    "ferry":           (60456, "Ferry dock"),
    "settlement":      (60448, "Settlement"),
    "inn":             (60436, "Inn"),
    "shop":            (60412, "Shop / vendor"),
    "market_board":    (60570, "Market board"),
    "repairs":         (60434, "Repairs"),
    "retainer_bell":   (60425, "Summoning bell"),
    "delivery_moogle": (60551, "Delivery moogle"),
    "chocobo_porter":  (60311, "Chocobo porter"),
    "hunt_board":      (60571, "Hunt board"),
    "weather":         (60581, "Skywatcher (weather)"),
    # Duties & places
    "dungeon":         (60414, "Dungeon entrance"),
    "raid":            (60428, "Raid entrance"),
    "entrance":        (60441, "Zone exit / adjoining area"),
    "stairs_up":       (60446, "To upper level"),
    "stairs_down":     (60447, "To lower level"),
    # Activities
    "quest":           (71021, "Quest available"),
    "quest_msq":       (71341, "Main Scenario quest"),
    "quest_locked":    (71031, "Quest (requirements not met)"),
    "fate":            (60458, "FATE"),
    "mob":             (60422, "Enemy / mob"),
    "flag":            (60561, "Map flag"),
    # Gathering
    "mining":          (60438, "Mining node (pickaxe)"),
    "quarrying":       (60437, "Quarrying node"),
    "logging":         (60433, "Logging node (hatchet)"),
    "harvesting":      (60432, "Harvesting node"),
    "fishing":         (60445, "Fishing spot"),
    "spearfishing":    (60465, "Spearfishing shadow"),
    # Generic marker for categories the game has no symbol for (aether currents,
    # arbitrary point sets). Drawn locally — id 0 means "not a game icon".
    "star":            (0, "White star (generic marker)"),
}

# Names rendered locally instead of fetched from game data.
LOCAL = {"star"}

_LOCAL_PNG: dict[str, bytes] = {}


def local_png(name: str) -> bytes | None:
    """A locally-drawn icon PNG (crisp 64px, cached). Only for names in LOCAL."""
    if name not in LOCAL:
        return None
    png = _LOCAL_PNG.get(name)
    if png:
        return png
    # White five-point star with a soft dark outline, matching the game's
    # marker weight (drawn 4x and downsampled for clean anti-aliasing).
    import io
    import math
    from PIL import Image, ImageDraw, ImageFilter
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy, r_out, r_in = S / 2, S / 2 + 6, S * 0.44, S * 0.19
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    # outline first (fat dark star underneath), then the white star on top
    d.polygon(pts, fill=(30, 34, 44, 255))
    im = im.filter(ImageFilter.MaxFilter(9))
    d2 = ImageDraw.Draw(im)
    d2.polygon(pts, fill=(250, 250, 248, 255))
    im = im.resize((64, 64), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    _LOCAL_PNG[name] = buf.getvalue()
    return _LOCAL_PNG[name]

# Garland gathering-node `type` -> icon name (matches sources/gamemap + gdb).
NODE_TYPE_ICON = {
    "mining": "mining", "quarrying": "quarrying",
    "logging": "logging", "harvesting": "harvesting",
    0: "mining", 1: "quarrying", 2: "logging", 3: "harvesting",
    4: "spearfishing", 5: "fishing",
}

# Natural-language aliases -> canonical names. The agent reaches for JOB words
# ("botany node") rather than node-type words ("logging"), and an unknown name
# used to become a plain gold dot — meet the model halfway instead. Kept OUT of
# names()/catalog() so the prompt vocabulary stays canonical.
ALIASES: dict[str, str] = {
    "botany": "logging", "botanist": "logging",
    "miner": "mining",
    "fisher": "fishing", "fish": "fishing",
    "gathering": "mining",
}


def canonical(name: str) -> str:
    """The canonical icon name for `name` (resolving aliases), or "" if unknown."""
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in ICONS else ""


def icon_id(name: str) -> int | None:
    got = ICONS.get(canonical(name))
    return got[0] if got else None


def catalog() -> list[dict]:
    return [{"name": n, "id": i, "label": l} for n, (i, l) in ICONS.items()]


def names() -> str:
    """Comma-separated names, for the system prompt."""
    return ", ".join(ICONS.keys())
