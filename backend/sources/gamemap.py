"""The in-game map, rebuilt from the game's own data.

Two sources, each for the half it actually has:
  - Garland Tools: the map TEXTURE (the parchment image). Covers every zone through
    Dawntrail, which is why it and not A Realm Remapped is the base here.
  - XIVAPI `MapMarker`: the game's own marker table — the place labels, aetherytes,
    settlements and dungeon entrances the in-game map draws, at real coordinates.

MapMarker is a SUBROW sheet: a Map's `MapMarkerRange` is the row id, and every marker
in that zone is a subrow of it. Fetching the row alone returns only subrow 0 — the
markers come from listing the row's subrows. That is the whole trick to this module.

Marker X/Y are pixels in the map's 2048-space, independent of the texture's real size,
so the UI positions them as a fraction of 2048 and they scale with any zoom.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

from sources import cache, garland

V2 = "https://v2.xivapi.com/api"
SOURCE = "XIVAPI (game data)"
TEX = 2048          # marker coordinate space

_DAY = 24 * 60 * 60
TTL_MAP = 7 * _DAY  # game data; the patch purge is the real invalidator

# icon id -> layer. Read off the live data rather than assumed; icon 0 means the game
# draws a label with no pin (the italic area names like "The Xobr'it Cinderfield").
_KIND = {
    0: "area",
    60453: "aetheryte",
    60352: "city",
    # 60448 is the settlement symbol — Aleport, every "Camp …". Named places
    # players actually look up, so they get their own layer (and turn up in the
    # map search) instead of drowning in "Other markers".
    60448: "settlement",
    60414: "dungeon", 60441: "dungeon",
    60442: "landmark",
}
# Layers the UI offers, in display order. `icon` is a fallback glyph for the legend.
LAYERS = [
    {"id": "area", "label": "Area names", "glyph": "𝘈"},
    {"id": "aetheryte", "label": "Aetherytes", "glyph": "💠"},
    {"id": "city", "label": "Cities", "glyph": "🏛"},
    {"id": "settlement", "label": "Settlements", "glyph": "🏕"},
    {"id": "dungeon", "label": "Dungeons", "glyph": "🌀"},
    {"id": "landmark", "label": "Landmarks", "glyph": "📍"},
    {"id": "marker", "label": "Other markers", "glyph": "•"},
    {"id": "node", "label": "Gathering", "glyph": "⛏"},
]


def _get(url: str, params: dict | None = None) -> dict | list:
    def _load() -> str:
        # garland's curl_cffi session (Chrome impersonation), not plain httpx: a
        # bot User-Agent is exactly what a WAF blocks intermittently, and one
        # blocked response used to get CACHED (see below). Two attempts, because
        # WAF blocks and rate limits are usually one-off — a request that fails
        # once and succeeds on retry should never surface as an error.
        last: Exception | None = None
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            s = garland._session()
            try:
                r = s.get(url, params=params, timeout=30)
            except Exception as e:
                last = e
                continue
            finally:
                s.close()
            # Raising on a bad response means NOTHING gets cached — a failure
            # stays a retryable failure instead of becoming a stored error page.
            if r.status_code == 200 and r.text.strip():
                return r.text
            last = RuntimeError(f"xivapi HTTP {r.status_code} for {url}")
        raise last if last else RuntimeError(f"xivapi fetch failed for {url}")

    key = url + (json.dumps(params, sort_keys=True) if params else "")
    # ns "map", NOT "xivapi": only namespaces in gameversion.PATCH_SENSITIVE get
    # purged when the game patches, and "map" is one of them. An unlisted namespace
    # would keep serving pre-patch markers forever.
    raw = cache.fetch_text("map", key, TTL_MAP, _load)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A poisoned entry: an ERROR PAGE cached back when the loader didn't check
        # status codes, which made every retry fail identically for the TTL (the
        # "Retry never works" bug). Self-heal: fetch fresh and overwrite it.
        fresh = _load()
        cache.put("map", key, fresh.encode("utf-8"))
        return json.loads(fresh)


def icon_url(icon_id: int) -> str:
    """Marker icon as a BACKEND path, so it's served from the local disk cache.

    Pointing the UI straight at XIVAPI worked, but re-downloaded every icon on every
    map open. The /map/icon proxy caches the PNG on disk (patch-purged), so repeat
    opens cost no network at all.
    """
    return f"/map/icon?id={icon_id}" if icon_id else ""


def icon_png(icon_id: int) -> bytes:
    """The icon PNG bytes: decoded from the installed client when possible
    (milliseconds, no network), else XIVAPI's render, disk-cached."""
    if not icon_id:
        return b""
    try:
        from sources import gameclient
        png = gameclient.icon_png(icon_id)
        if png:
            return png
    except Exception:
        pass          # undecodable format / no client -> XIVAPI below
    folder = f"{icon_id // 1000 * 1000:06d}"
    url = f"{V2}/asset?path=ui/icon/{folder}/{icon_id:06d}.tex&format=png"

    def _load() -> bytes:
        # Two attempts: a one-off WAF block on an icon painted broken-image squares
        # all over the map. b"" is never cached, so a failure stays retryable.
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            s = garland._session()
            try:
                r = s.get(url, timeout=30)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                pass
            finally:
                s.close()
        return b""

    try:
        return cache.fetch("map", f"icon:{icon_id}", TTL_MAP, _load)
    except Exception:
        return b""


_MAP_INDEX: dict | None = None


def _map_index() -> dict:
    """zone name (lowercased) -> {size_factor, marker_range}.

    The Map sheet needs paging; do it once and keep it. A zone can appear on several
    rows (per-floor variants, arena copies that reuse the place name), so per name the
    BEST row wins: an overworld-typed map id beats any other, and among those the row
    with the larger marker range — the closest thing the sheet has to "the real map".
    """
    global _MAP_INDEX
    if _MAP_INDEX is not None:
        return _MAP_INDEX
    out: dict = {}
    after, pages = 0, 0
    fields = "Id,SizeFactor,MapMarkerRange,PlaceName.Name"
    failed = False
    while pages < 12:
        try:
            d = _get(f"{V2}/sheet/Map", {"limit": 500, "after": after, "fields": fields})
        except Exception:
            # A failed page TRUNCATES the index rather than failing the map: the
            # texture comes from Garland, so the zone can still draw — just with
            # fewer (or no) markers this session. Blowing up here once turned one
            # flaky XIVAPI response into "Couldn't load the map" with a working
            # texture sitting in cache. The partial index is NOT memoised, so the
            # next request retries the missing pages.
            failed = True
            break
        rows = (d or {}).get("rows") or []
        if not rows:
            break
        for row in rows:
            f = row.get("fields") or {}
            name = ((f.get("PlaceName") or {}).get("fields") or {}).get("Name", "")
            if not name:
                continue
            rec = {
                "name": name,   # display casing, for the zone list
                "map_id": f.get("Id") or "",
                "size_factor": f.get("SizeFactor") or 100,
                "marker_range": f.get("MapMarkerRange") or 0,
            }
            prev = out.get(name.lower())
            if prev is None or _row_rank(rec) > _row_rank(prev):
                out[name.lower()] = rec
        after = rows[-1]["row_id"]
        pages += 1
    if not failed:
        _MAP_INDEX = out   # memoise only a COMPLETE index
    return out


def _is_overworld(map_id: str) -> bool:
    """True for maps the in-game region picker would list.

    The map id encodes the type: <expac letter><digit><TYPE><variant>, e.g. Central
    Shroud 'f1f1', Limsa 's1t2', Mist 's1h1'. TYPE f/t/h = field/town/housing. The
    variant being a DIGIT is what separates real zones from trial arenas, which reuse
    the field type with a letter variant ('w1fa' Bowl of Embers, 'r1fc' Akh Afah) —
    exactly the blank parchment squares that were polluting the zone list.
    """
    return len(map_id) >= 4 and map_id[2] in "fth" and map_id[3].isdigit()


def _row_rank(rec: dict) -> tuple:
    return (_is_overworld(rec.get("map_id", "")), rec.get("marker_range") or 0)


def markers(marker_range: int) -> list:
    """Every marker for a zone, from the subrows of its MapMarkerRange row."""
    if not marker_range:
        return []
    try:
        d = _get(f"{V2}/sheet/MapMarker",
                 {"after": marker_range - 1, "limit": 200,
                  "fields": "X,Y,Icon,PlaceNameSubtext.Name,DataKey.PlaceName.Name"})
    except Exception:
        return []
    out = []
    for row in (d or {}).get("rows") or []:
        if row.get("row_id") != marker_range:
            continue
        f = row.get("fields") or {}
        icon = (f.get("Icon") or {}).get("id", 0) or 0
        label = ((f.get("PlaceNameSubtext") or {}).get("fields") or {}).get("Name", "")
        if not label:
            # Aetherytes (and other data-linked markers) carry NO subtext — their
            # name sits on the row the marker links to (DataType 3 -> Aetheryte,
            # whose PlaceName is "Horizon" etc.). The in-game map labels them;
            # without this fallback every aetheryte drew as a nameless icon.
            dk = (f.get("DataKey") or {}).get("fields") or {}
            label = ((dk.get("PlaceName") or {}).get("fields") or {}).get("Name") or ""
        # A marker with no icon AND no label draws nothing — skip the noise.
        if not icon and not label:
            continue
        out.append({
            "x": f.get("X") or 0, "y": f.get("Y") or 0,
            "label": label, "kind": _KIND.get(icon, "marker"),
            "icon": icon_url(icon), "icon_id": icon,
        })
    return out


def map_texture(name: str) -> dict | None:
    """A zone's map texture: Garland's file when it exists, else the game's own
    composed map straight from XIVAPI's asset endpoint.

    Garland covers almost everything, but is MISSING a handful of city maps
    (Ul'dah - Steps of Thal 404s) — those zones sat in the picker but bounced
    back to it when opened. The XIVAPI asset is keyed by the index's map_id
    (`w1t2/02`) and is the game's real 2048-space render, so markers line up.
    Same result shape as garland.map_texture; disk-cached the same way.
    """
    rec = _map_index().get((name or "").strip().lower()) or {}
    map_id = rec.get("map_id")
    # The installed client first: the SAME art Garland serves, composed locally
    # (base × terrain mask) — zero network and current the moment a patch lands.
    # Compose costs a few seconds once; the disk cache absorbs repeats.
    if map_id:
        try:
            from sources import gameclient

            def _compose() -> bytes:
                return gameclient.map_png(map_id) or b""

            img = cache.fetch("map", f"localtex:{map_id}:{gameclient.version()}",
                              TTL_MAP, _compose)
            if img:
                return {"image": img, "size_factor": rec.get("size_factor", 100),
                        "region": "", "zone": rec.get("name", name),
                        "url": f"sqpack:ui/map/{map_id}"}
        except Exception:
            pass
    t = garland.map_texture(name)
    if t:
        return t
    if not map_id:
        return None

    def _load() -> bytes:
        # Two attempts, and b"" is never cached — a one-off failure must not
        # become a cached "this zone has no map".
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            s = garland._session()
            try:
                r = s.get(f"{V2}/asset/map/{map_id}", timeout=30)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                pass
            finally:
                s.close()
        return b""

    try:
        img = cache.fetch("map", f"tex:{map_id}", TTL_MAP, _load)
    except Exception:
        return None
    if not img:
        return None
    return {"image": img, "size_factor": rec.get("size_factor", 100),
            "region": "", "zone": rec.get("name", name),
            "url": f"{V2}/asset/map/{map_id}"}


def search_markers(query: str, limit: int = 8) -> list:
    """Named markers matching `query`, across EVERY drawable zone — one XIVAPI
    sheet-search call, not a hundred per-zone fetches. Feeds the map bar's
    search. Only the named layers (area names, aetherytes, cities, dungeons,
    landmarks) are returned: the "Other markers" service icons are skipped —
    they carry no text worth matching (NPC names aren't in this sheet at all;
    that's Garland's NPC data)."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    # Reverse the map index: a hit's row_id IS the zone's MapMarkerRange.
    by_range = {v["marker_range"]: v["name"]
                for v in _map_index().values() if v.get("marker_range")}
    try:
        d = _get(f"{V2}/search", {
            "sheets": "MapMarker",
            # ~ is XIVAPI's partial string match; strip quotes so a typed " can't
            # break out of the term.
            "query": f'PlaceNameSubtext.Name~"{q.replace(chr(34), "")}"',
            "fields": "X,Y,Icon,PlaceNameSubtext.Name",
            "limit": 40,
        })
    except Exception:
        return []
    out = []
    for hit in (d or {}).get("results") or []:
        zone = by_range.get(hit.get("row_id"))
        # No drawable texture = a map the picker doesn't offer (dungeon floors);
        # jumping there would land on an error card.
        if not zone or not garland.map_image_url(zone):
            continue
        f = hit.get("fields") or {}
        icon = (f.get("Icon") or {}).get("id", 0) or 0
        kind = _KIND.get(icon, "marker")
        if kind == "marker":
            continue
        label = ((f.get("PlaceNameSubtext") or {}).get("fields") or {}).get("Name", "")
        if not label:
            continue
        out.append({"zone": zone, "x": f.get("X") or 0, "y": f.get("Y") or 0,
                    "label": label, "kind": kind, "icon": icon_url(icon)})
        if len(out) >= limit:
            break

    # Aetherytes match by their OWN name (Horizon, Camp Drybone…), which lives on
    # the Aetheryte sheet — their MapMarker rows have no subtext for the pass
    # above to hit. The sheet's AetherstreamX/Y are teleport-LINE endpoints, not
    # the pin position, so coordinates come from the zone's (disk-cached) marker
    # list, matched by the label fallback in markers().
    try:
        a = _get(f"{V2}/search", {
            "sheets": "Aetheryte",
            "query": f'PlaceName.Name~"{q.replace(chr(34), "")}"',
            "fields": "PlaceName.Name,IsAetheryte,Invisible,Map.PlaceName.Name",
            "limit": 12,
        })
    except Exception:
        a = {}
    aeth = []
    for hit in (a or {}).get("results") or []:
        f = hit.get("fields") or {}
        if not f.get("IsAetheryte") or f.get("Invisible"):
            continue
        name = ((f.get("PlaceName") or {}).get("fields") or {}).get("Name", "")
        zone = ((((f.get("Map") or {}).get("fields") or {})
                 .get("PlaceName") or {}).get("fields") or {}).get("Name", "")
        rec = _map_index().get(zone.lower()) if zone else None
        if not name or not rec or not garland.map_image_url(rec["name"]):
            continue
        m = next((m for m in markers(rec["marker_range"])
                  if m["kind"] == "aetheryte" and m["label"] == name), None)
        if m:
            aeth.append({"zone": rec["name"], "x": m["x"], "y": m["y"],
                         "label": name, "kind": "aetheryte", "icon": m["icon"]})
    # Aetherytes lead: an exact teleport destination beats a same-named area text.
    return (aeth + out)[:limit]


# The in-game map's top-level region order (Dawntrail regions follow the base list;
# anything the data adds later lands after these, alphabetically).
REGION_ORDER = [
    "La Noscea", "The Black Shroud", "Thanalan", "Coerthas", "Mor Dhona",
    "Abalathia's Spine", "Dravania", "Gyr Abania", "Hingashi", "Othard",
    "Norvrandt", "The Northern Empty", "Ilsabard", "The Sea of Stars",
    "The World Unsundered", "Yok Tural", "Xak Tural", "Unlost World",
]


def index_complete() -> bool:
    """True when the FULL Map-sheet index is memoised.

    A flaky page truncates the index instead of failing (so the map still draws),
    but a truncated index also silently truncates the zone LIST — callers that
    cache the list must know whether it's worth caching.
    """
    return _MAP_INDEX is not None


def regions() -> list[dict]:
    """Zones the rebuilt map can draw, grouped by region like the in-game picker.

    A zone qualifies only if it's an overworld map (see _is_overworld — this is what
    keeps blank trial arenas and textureless dungeon interiors out), its Garland
    parent is a real region, and a Garland texture URL exists. Both checks are
    in-memory index lookups, no HTTP.
    """
    grouped: dict[str, set] = {}
    for rec in _map_index().values():
        name = rec.get("name", "")
        if not name or not _is_overworld(rec.get("map_id", "")):
            continue
        z = garland._zone_by_name(name)
        if not z:
            continue
        parent = garland._location_index().get(str(z.get("parentId"))) or {}
        region = parent.get("name", "")
        if not region or parent.get("id") == z.get("id"):
            continue
        if not garland.map_image_url(name):
            continue
        grouped.setdefault(region, set()).add(name)

    order = {r: i for i, r in enumerate(REGION_ORDER)}
    out = []
    for region in sorted(grouped, key=lambda r: (order.get(r, len(order)), r.lower())):
        out.append({"region": region, "zones": sorted(grouped[region], key=str.lower)})
    return out


def zone(name: str) -> dict | None:
    """Everything needed to draw a zone: texture, markers, and gathering nodes."""
    idx = _map_index().get((name or "").strip().lower()) or {}
    # No texture = not drawable, full stop. And a constructible URL is NOT a texture:
    # Garland has no file for most dungeon interiors, so the bytes must actually
    # exist. map_texture is disk-cached, so for a real zone this is the same fetch
    # the UI is about to make anyway; for a fake one it's a cheap 404. Skipping this
    # check once produced "labels floating on black" — and a stale lastMapZone
    # pointing at such a zone dumped the user on the ARR homepage at startup.
    # (This module's map_texture, not garland's — it adds the XIVAPI fallback.)
    if not map_texture(name):
        return None
    ms = markers(idx.get("marker_range", 0))
    return {
        "found": True,
        # The CANONICAL display name, not the request echo — links arrive with
        # whatever casing/spelling the model wrote, and the UI matches this
        # string against the zone picker to keep the dropdowns in sync.
        "zone": idx.get("name") or name,
        # A backend path, not the Garland URL: /map/texture serves the image from
        # the local disk cache, so a revisited zone loads with zero network.
        "texture": f"/map/texture?zone={quote(name)}",
        "size_factor": idx.get("size_factor", 100),
        "coord_space": TEX,
        "markers": ms,
        "layers": [l for l in LAYERS if any(m["kind"] == l["id"] for m in ms)],
        "sources": [garland.SOURCE, SOURCE],
    }
