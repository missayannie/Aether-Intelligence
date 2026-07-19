"""Field aether-current locations, read straight from the installed client.

The game files carry the whole chain: EObjName rows literally named "Aether
Current", their world placements in the Level sheet, and the zone's map
geometry to convert world units into the flag coordinates players read. That
makes this source EXACT (it's what the wikis are derived from) and always
current with the installed patch — no network, nothing to invent.

Only the 10 field-interaction currents per zone live here; the 5 quest-granted
ones aren't world points (their "location" is a quest giver). No client on the
machine → callers fall back to wiki research, same as everything else.
"""
from __future__ import annotations

import threading

from . import gameclient as gc

_LOCK = threading.Lock()
# zone name (lower) -> list of (x, y) game coords, sorted by EObj id — which
# tracks the in-game attunement numbering closely enough to label 1..N.
_BY_ZONE: dict[str, list[tuple[float, float]]] | None = None


def _build() -> dict[str, list[tuple[float, float]]]:
    gd = gc._data()
    if gd is None:
        return {}

    current_ids: set[int] = set()
    for rid, row in gd.sheet("EObjName").rows(cols=[0]):
        if isinstance(row[0], str) and row[0].strip().lower() == "aether current":
            current_ids.add(rid)
    if not current_ids:
        return {}

    # One pass over Level: territory -> [(eobj, world_x, world_z)].
    # Level columns (community layout, stable for years): 0 X, 2 Z, 6 Object,
    # 9 Territory.
    by_terr: dict[int, list[tuple[int, float, float]]] = {}
    for _rid, row in gd.sheet("Level").rows(cols=[0, 2, 6, 9]):
        x, z, obj, terr = row
        if obj in current_ids:
            by_terr.setdefault(terr, []).append((obj, float(x), float(z)))

    out: dict[str, list[tuple[float, float]]] = {}
    for terr_id, pts in by_terr.items():
        t = gc.get("TerritoryType", terr_id, ["PlaceName", "Map"]) or {}
        place = gc.get("PlaceName", int(t.get("PlaceName") or 0), ["Name"]) or {}
        zone = (place.get("Name") or "").strip()
        m = gc.get("Map", int(t.get("Map") or 0),
                   ["SizeFactor", "OffsetX", "OffsetY"]) or {}
        sf = int(m.get("SizeFactor") or 100)
        ox = int(m.get("OffsetX") or 0)
        oy = int(m.get("OffsetY") or 0)
        if not zone:
            continue
        pts.sort(key=lambda p: p[0])
        coords = [(gc.world_to_game(x, sf, ox), gc.world_to_game(z, sf, oy))
                  for _obj, x, z in pts]
        # Duplicate/instanced territories reference the same zone name — keep
        # the fullest set (the real overworld one). Value carries the DISPLAY
        # name so callers can canonicalise fuzzy requests.
        prev = out.get(zone.lower())
        if prev is None or len(coords) > len(prev[1]):
            out[zone.lower()] = (zone, coords)
    return out


def find(zone: str) -> tuple[str, list[dict]] | None:
    """(canonical zone name, [{x, y, label}]) for a zone, or None.

    Tolerates region-prefixed phrasings ("Ilsabard Garlemald"): any known zone
    name contained in the request wins — longest match first, so "south
    thanalan" beats "thanalan". The canonical name matters: the map lookup that
    follows needs the real zone, not the player's phrasing.
    """
    global _BY_ZONE
    if not gc.available():
        return None
    with _LOCK:
        if _BY_ZONE is None:
            try:
                _BY_ZONE = _build()
            except Exception:
                return None      # transient read issue: don't cache emptiness
    want = (zone or "").strip().lower()
    hit = _BY_ZONE.get(want)
    if hit is None and want:
        for name in sorted(_BY_ZONE, key=len, reverse=True):
            if name in want:
                hit = _BY_ZONE[name]
                break
    if hit is None:
        return None
    display, pts = hit
    return display, [{"x": round(x, 1), "y": round(y, 1), "label": str(i + 1)}
                     for i, (x, y) in enumerate(pts)]


def field_currents(zone: str) -> list[dict] | None:
    """[{x, y, label}] in game flag coords for a zone, or None."""
    got = find(zone)
    return got[1] if got else None
