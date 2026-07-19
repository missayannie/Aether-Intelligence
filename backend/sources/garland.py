"""Garland Tools — the item/content database.

Replaces the Lodestone's Eorzea Database as this app's database. The reason is
plumbing, not taste: Garland is a plain JSON API, so there is no bot-challenge to
retry through and no CSS selectors to drift out from under us. Every field below is
read from a documented key rather than regexed out of a rendered page.

    search:   /api/search.php?text=<q>&lang=en   -> every type at once
    item:     /db/doc/item/en/3/<id>.json        -> {item:{...}, partials:[...]}
    other:    /db/doc/<type>/en/2/<id>.json      -> instance / npc / quest / ...
    icons:    /files/icons/item/<icon>.png

What it gives us that the Eorzea Database could not:
  - stats as DATA ({"Dexterity": 146, ...}) rather than prose to parse
  - jobCategories ("NIN") — who can actually equip it
  - upgrades/downgrades — the gear progression chain, which answers "what replaces
    this?" directly; the official DB has no such field
  - NPCs, which the official database simply does not have pages for (it 404s)

What it does NOT have, and what we therefore gave up in the swap:
  - player comments (there is no comments field at all)
  - official/patch-day authority — Garland is community-maintained and can lag a
    patch, so prefer the patch notes for anything the current patch touched.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from curl_cffi import requests as cffi

from config import USER_AGENT
from sources import cache

BASE = "https://garlandtools.org"
SEARCH_URL = BASE + "/api/search.php"
ICON_URL = BASE + "/files/icons/item/{icon}.png"
SOURCE = "Garland Tools"


def _gc():
    """The local game-client source (lazy import — gameclient pulls sqlite and
    the sqpack reader; a machine without the game never pays for them)."""
    from sources import gameclient
    return gameclient


def _gc_ready() -> bool:
    """True when the installed client can answer instead of the network:
    client found, schema validated against it, derived index built and
    version-fresh. Any False here and every function below quietly uses
    Garland over HTTP exactly as before."""
    try:
        return _gc().ready()
    except Exception:
        return False


def _patch_tag(kind: str, ident) -> str:
    """Per-record patch number for LOCALLY answered records — the one field
    the client files don't carry, restored from Garland's own MIT-licensed
    supplemental data (vendored; see sources/supplemental.py)."""
    try:
        from sources import supplemental
        return supplemental.patch_tag(kind, ident)
    except Exception:
        return ""


def _icon_url(icon) -> str:
    """Icon URL for a locally-answered record: the backend's own /map/icon
    endpoint (which decodes the client's .tex — no CDN round trip). The
    Garland CDN URL remains for network-answered records, keeping every
    response self-consistent about where it came from."""
    import os
    port = os.environ.get("FFXIV_BACKEND_PORT", "8756")
    return f"http://127.0.0.1:{port}/map/icon?id={icon}"

# Item docs are on data version 3; the other types are on 2 (verified live).
_DOC_VERSION = {"item": "3", "core": "3"}

# Zone id -> zone name. A node record names the NODE ("The Xobr'it Cinderfield") and
# carries the zone only as an id (z: 4507 -> "Yak T'el"). The zone is what the player
# actually needs — it's what you type into a map — so resolve it. Garland ships the
# whole table in one core document; cache it for the process.
_LOC_INDEX: dict | None = None


def _location_index() -> dict:
    global _LOC_INDEX
    if _LOC_INDEX is None:
        try:
            ver = _DOC_VERSION.get("core", "3")
            core = _get_json(f"{BASE}/db/doc/core/en/{ver}/data.json")
            _LOC_INDEX = (core or {}).get("locationIndex") or {}
        except Exception:
            _LOC_INDEX = {}
    return _LOC_INDEX


def zone_name(zone_id) -> str:
    """Zone name for a Garland zone id, or "" if unknown."""
    if not zone_id:
        return ""
    rec = _location_index().get(str(zone_id))
    return (rec or {}).get("name", "") if isinstance(rec, dict) else ""


def _zone_by_name(zone: str) -> dict | None:
    """Location record for a zone name — preferring an actual ZONE over a region.

    Some names exist twice: "Mor Dhona" is both a region (its own parent) and the
    zone inside it. Returning the region record made the zone undrawable (a region
    has no parent to build the /files/maps/<Region>/<Zone>.png path from), so a
    record with a real parent wins over a self-parented one.
    """
    want = (zone or "").strip().lower()
    if not want:
        return None
    best = None
    for rec in _location_index().values():
        if not (isinstance(rec, dict) and (rec.get("name") or "").lower() == want):
            continue
        if rec.get("parentId") not in (rec.get("id"), None):
            return rec       # a real zone (has a parent region) — take it immediately
        best = best or rec   # self-parented region: only if nothing better shows up
    return best


def map_image_url(zone: str) -> str:
    """URL of Garland's full map texture for a zone, or "".

    Pattern is /files/maps/<Region>/<Zone>.png, where Region is the zone's parent in
    locationIndex — read off Garland's own node page, not guessed. Unlike A Realm
    Remapped (which stops at Endwalker) this covers Dawntrail.
    """
    z = _zone_by_name(zone)
    if not z:
        return ""
    parent = _location_index().get(str(z.get("parentId"))) or {}
    region = parent.get("name")
    # A region is its own parent; that means the zone had no region above it.
    if not region or parent.get("id") == z.get("id"):
        return ""
    return f"{BASE}/files/maps/{quote(region)}/{quote(z['name'])}.png"


def map_texture(zone: str) -> dict | None:
    """Garland's map image for a zone -> {image, size_factor, region, zone, url}.

    size_factor is the in-game SizeFactor the pin math expects. Garland stores it as
    a scale (`size`: 1.0), so it's ×100 here.
    """
    url = map_image_url(zone)
    if not url:
        return None
    z = _zone_by_name(zone) or {}
    parent = _location_index().get(str(z.get("parentId"))) or {}

    def _load() -> bytes:
        # Two attempts — a one-off WAF block must not surface as a missing map.
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            s = _session()
            try:
                r = s.get(url, timeout=30)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                pass
            finally:
                s.close()
        return b""

    # Disk-cached: a zone texture is ~2.5MB and immutable between patches, so after
    # the first view it comes off local disk (and works offline). The "garland"
    # namespace is patch-purged, which is the only real expiry for map art.
    try:
        img = cache.fetch("garland", f"maptex:{url}", TTL_DB, _load)
    except Exception:
        return None
    if not img:
        return None
    return {"image": img, "size_factor": int(round(float(z.get("size") or 1.0) * 100)),
            "region": parent.get("name", ""), "zone": z.get("name", zone),
            "url": url, "source": SOURCE}


def node(ident) -> dict | None:
    """A gathering node's full record — where it is and when it's up.

    This is what the Database tab's map pin comes from: coords, the zone, the spawn
    window, and the folklore tome that unlocks it. LOCAL-FIRST: node ids are the
    game's own GatheringPointBase ids, so the installed client answers directly
    (verified to coordinate-level parity); Garland remains the network fallback.
    """
    local = _gc().node_record(ident) if _gc_ready() else None
    if local:
        return {**local, "url": web_url("node", ident), "source": _gc().SOURCE}
    try:
        doc = _get_json(f"{BASE}/db/doc/node/en/2/{ident}.json")
    except Exception:
        return None
    n = (doc or {}).get("node") or {}
    if not n.get("name"):
        return None
    coords = n.get("coords") or []
    names = {str((p.get("obj") or {}).get("i", "")): (p.get("obj") or {}).get("n", "")
             for p in doc.get("partials") or []}
    return {
        "id": str(n.get("id", ident)),
        "name": n.get("name", ""),
        "zone": zone_name(n.get("zoneid")),
        "level": n.get("lvl", 0),
        "type": n.get("limitType", ""),
        # The raw gathering-kind int (0=mining … 5=fishing) — "type" above is the
        # LIMIT type ("Unspoiled" etc.), so icon picks need this one (NODE_TYPE_ICON).
        "type_id": n.get("type", -1),
        "stars": n.get("stars", 0),
        "x": float(coords[0]) if len(coords) > 1 else 0.0,
        "y": float(coords[1]) if len(coords) > 1 else 0.0,
        # times are ET spawn hours; uptime is in minutes.
        "spawn_times": n.get("time") or [],
        "uptime_minutes": n.get("uptime", 0),
        "folklore": names.get(str(n.get("unlockId", "")), ""),
        "items": [names.get(str(i.get("id", "")), "") for i in n.get("items") or []],
        "url": web_url("node", n.get("id", ident)),
        "source": SOURCE,
    }

# Types the search returns that we can meaningfully link or open.
LINKABLE = ("item", "instance", "npc", "quest", "achievement", "mob", "fate", "node", "leve")

_DAY = 24 * 60 * 60
TTL_DB = 7 * _DAY          # game data; the patch purge is the real invalidator


@dataclass
class Hit:
    type: str
    id: str
    name: str
    item_level: int = 0
    url: str = ""
    icon: str = ""      # search rows carry the icon id in obj.c


@dataclass
class Item:
    id: str
    name: str
    url: str
    source: str = SOURCE
    item_level: int = 0
    category: str = ""        # slot name where known
    jobs: str = ""            # "NIN"
    description: str = ""
    icon: str = ""
    patch: str = ""
    sockets: int = 0
    attributes: dict = field(default_factory=dict)
    upgrades: list = field(default_factory=list)      # [{id, name, item_level}]
    downgrades: list = field(default_factory=list)
    details: str = ""         # compact text blob for the model to read
    # "Sources & Uses" — where it comes from and what it feeds into. This is the half
    # of a database entry that actually answers "how do I get one?".
    sell_price: int = 0                               # gil, to an NPC vendor
    tradeable: bool = True                            # False -> no market board
    nodes: list = field(default_factory=list)         # [{id, name, level, type}]
    ventures: list = field(default_factory=list)      # retainer venture ids
    ingredient_of: list = field(default_factory=list) # [{id, name, qty}]
    vendors: list = field(default_factory=list)       # [{id, name}]


def _session() -> cffi.Session:
    return cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})


def clean_html(s: str) -> str:
    """Garland doc text -> plain prose. Their descriptions embed the game's own
    markup (<br>, <span class="highlight">…</span>), which the UI renders as
    LITERAL text — "<br>If any other action…" shipped to players once. Line
    breaks survive as newlines (the UI shows them via white-space: pre-line);
    every other tag is dropped, keeping its inner text."""
    import html as _html
    import re as _re
    if not s:
        return ""
    s = _re.sub(r"<br\s*/?>", "\n", s, flags=_re.I)
    s = _re.sub(r"</?p>", "\n", s, flags=_re.I)
    s = _re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def web_url(kind: str, ident) -> str:
    """The human page. Garland's UI is hash-routed, e.g. /db/#item/33992."""
    return f"{BASE}/db/#{kind}/{ident}"


def _get_json(url: str, params: dict | None = None) -> dict | list:
    def _load() -> str:
        # Two attempts: WAF blocks and rate limits are usually one-off. Raise on a
        # bad response so nothing gets cached — caching an error page once made
        # every retry fail identically until the TTL expired.
        last: Exception | None = None
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            s = _session()
            try:
                r = s.get(url, params=params, timeout=20)
            except Exception as e:
                last = e
                continue
            finally:
                s.close()
            if r.status_code == 200 and r.text.strip():
                return r.text
            last = RuntimeError(f"garland HTTP {r.status_code} for {url}")
        raise last if last else RuntimeError(f"garland fetch failed for {url}")

    key = url + (json.dumps(params, sort_keys=True) if params else "")
    raw = cache.fetch_text("garland", key, TTL_DB, _load)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Self-heal a poisoned entry (error page cached before status checking).
        fresh = _load()
        cache.put("garland", key, fresh.encode("utf-8"))
        return json.loads(fresh)


def search(query: str, kind: str = "", limit: int = 20) -> list[Hit]:
    """Search everything, optionally filtered to one type.

    One request covers items, dungeons (type 'instance'), NPCs, quests and more —
    the official database needed a separate section per kind.
    """
    if not query.strip():
        return []
    # Local-first: the derived index covers every LINKABLE kind, so when the
    # client is ready its answer IS the answer (an empty local result is a
    # real "no matches", not a miss to retry over the network).
    if _gc_ready():
        return [Hit(type=h["type"], id=h["id"], name=h["name"],
                    item_level=int(h.get("item_level") or 0),
                    url=web_url(h["type"], h["id"]),
                    icon=_icon_url(h["icon"]) if h.get("icon") else "")
                for h in _gc().search_hits(query, kind=kind, limit=limit)]
    try:
        rows = _get_json(SEARCH_URL, {"text": query, "lang": "en"})
    except Exception:
        return []
    if not isinstance(rows, list):
        return []

    out: list[Hit] = []
    for r in rows:
        t = r.get("type", "")
        if kind and t != kind:
            continue
        if t not in LINKABLE:
            continue
        obj = r.get("obj") or {}
        name = obj.get("n") or ""
        ident = str(r.get("id") or obj.get("i") or "")
        if not (name and ident):
            continue
        # obj.c is only an icon id for SOME types — for fates it's a coords
        # array, which once produced ".../icons/item/[13, 36].png" broken images.
        icon = obj.get("c")
        icon_ok = isinstance(icon, (int, str)) and str(icon).strip().lstrip("-").isdigit()
        out.append(Hit(type=t, id=ident, name=name,
                       item_level=int(obj.get("l") or 0), url=web_url(t, ident),
                       icon=ICON_URL.format(icon=icon) if icon_ok else ""))
        if len(out) >= limit:
            break
    return out


def find(query: str, kind: str = "") -> Hit | None:
    """Best match for `query`. Exact name wins; otherwise the first result.

    The exact-match preference matters: a loose search for "Vanguard" returns a pile
    of unrelated furniture, and linking the wrong page is worse than not linking.
    """
    hits = search(query, kind=kind)
    if not hits:
        return None
    want = query.strip().lower()
    for h in hits:
        if h.name.strip().lower() == want:
            return h
    return hits[0]


def item(ident) -> Item | None:
    """Full item record, with its upgrade chain resolved to real names.

    LOCAL-FIRST: item ids are the game's own, so the installed client supplies
    the whole record (stats, gathering nodes, vendors, recipes) with the
    upgrade chain computed from category+jobs+ilvl. Garland stays the network
    fallback — and still carries the per-item patch tag we can't derive."""
    local = _gc().item_record(ident) if _gc_ready() else None
    if local:
        parts = [local["name"], f"item level {local['item_level']}"]
        if local["jobs"]:
            parts.append(f"jobs: {local['jobs']}")
        if local["attributes"]:
            parts.append(", ".join(f"{k} {v}" for k, v in local["attributes"].items()))
        if local["sockets"]:
            parts.append(f"{local['sockets']} materia slots")
        return Item(
            id=local["id"], name=local["name"], url=web_url("item", local["id"]),
            source=_gc().SOURCE,
            item_level=local["item_level"], category=local["category"],
            jobs=local["jobs"], description=local["description"],
            icon=_icon_url(local["icon"]) if local["icon"] else "",
            patch=_patch_tag("item", local["id"]),
            sockets=local["sockets"], attributes=local["attributes"],
            upgrades=local["upgrades"], downgrades=local["downgrades"],
            details=" · ".join(str(p) for p in parts if p)[:800],
            sell_price=local["sell_price"], tradeable=local["tradeable"],
            nodes=local["nodes"], ventures=local["ventures"],
            ingredient_of=local["ingredient_of"], vendors=local["vendors"],
        )
    ver = _DOC_VERSION.get("item", "3")
    try:
        doc = _get_json(f"{BASE}/db/doc/item/en/{ver}/{ident}.json")
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    it = doc.get("item") or {}
    name = it.get("name")
    if not name:
        return None

    # `partials` resolves every id the record references — upgrade chain, gathering
    # nodes, vendors — so we can report names instead of bare numbers.
    names: dict[str, dict] = {}
    nodes_by_id: dict[str, dict] = {}
    npcs_by_id: dict[str, dict] = {}
    for p in doc.get("partials") or []:
        obj = p.get("obj") or {}
        ident_ = str(obj.get("i", ""))
        if not ident_:
            continue
        if p.get("type") == "item":
            names[ident_] = {"id": ident_, "name": obj.get("n", ""),
                             "item_level": int(obj.get("l") or 0)}
        elif p.get("type") == "node":
            nodes_by_id[ident_] = {"id": ident_, "name": obj.get("n", ""),
                                   "level": int(obj.get("l") or 0),
                                   "type": obj.get("lt", ""),
                                   "zone": zone_name(obj.get("z"))}
        elif p.get("type") == "npc":
            npcs_by_id[ident_] = {"id": ident_, "name": obj.get("n", "")}

    def chain(key: str) -> list:
        out = []
        for i in it.get(key) or []:
            out.append(names.get(str(i), {"id": str(i), "name": "", "item_level": 0}))
        return out

    nodes = [nodes_by_id.get(str(i), {"id": str(i), "name": "", "level": 0, "type": ""})
             for i in (it.get("nodes") or [])]
    vendors = [npcs_by_id.get(str(v), {"id": str(v), "name": ""})
               for v in (it.get("vendors") or []) if not isinstance(v, dict)]
    ingredient_of = [
        {**names.get(str(k), {"id": str(k), "name": "", "item_level": 0}), "qty": q}
        for k, q in (it.get("ingredient_of") or {}).items()
    ]

    attrs = it.get("attr") or {}
    icon = it.get("icon")
    parts = [f"{name}", f"item level {it.get('ilvl', '')}"]
    if it.get("jobCategories"):
        parts.append(f"jobs: {it['jobCategories']}")
    if attrs:
        parts.append(", ".join(f"{k} {v}" for k, v in attrs.items()))
    if it.get("sockets"):
        parts.append(f"{it['sockets']} materia slots")

    return Item(
        id=str(it.get("id", ident)),
        name=name,
        url=web_url("item", it.get("id", ident)),
        item_level=int(it.get("ilvl") or 0),
        category=str(it.get("category", "")),
        jobs=it.get("jobCategories", "") or "",
        description=clean_html(it.get("description") or ""),
        icon=ICON_URL.format(icon=icon) if icon else "",
        patch=str(it.get("patch", "")),
        sockets=int(it.get("sockets") or 0),
        attributes=attrs,
        upgrades=chain("upgrades"),
        downgrades=chain("downgrades"),
        details=" · ".join(str(p) for p in parts if p)[:800],
        sell_price=int(it.get("sell_price") or 0),
        # Garland omits `tradeable` on tradeable items and sets 0 when it's bound.
        tradeable=it.get("tradeable", 1) != 0 and not it.get("unlistable"),
        nodes=nodes,
        ventures=[str(v) for v in (it.get("ventures") or [])],
        ingredient_of=ingredient_of,
        vendors=vendors,
    )


def npc_locations(ident) -> dict | None:
    """An NPC's name and every place it stands, with exact flag coordinates.

    Garland models an NPC that appears in several places as a base record plus
    `alts` — and the LOCATION DATA (zoneid, coords, quests given there) lives on the
    alts, not the base. Kupopo, the Pictomancer quest moogle, is the canonical case:
    the base record carries nothing but the alt list; the Old Gridania spot that
    starts "Paint It Pink" is one of seven alts. So: expand every alt, collect every
    (zone, x, y), and name the quests given at each spot so a caller can pick the
    right one.
    """
    # LOCAL-FIRST — but only when the client actually knows placements: many
    # standing NPCs live in territory layout files rather than the Level sheet,
    # and an empty local location list must fall through to Garland (whose
    # pipeline parses those files) rather than answer "nowhere".
    if _gc_ready():
        local = _gc().npc_record(ident)
        if local and local.get("locations"):
            return {**local, "url": web_url("npc", ident)}

    def _fetch(i) -> dict | None:
        try:
            d = _get_json(f"{BASE}/db/doc/npc/en/2/{i}.json")
        except Exception:
            return None
        return d if isinstance(d, dict) else None

    base = _fetch(ident)
    if not base or not (base.get("npc") or {}).get("name"):
        return None

    locations, seen = [], set()

    def _collect(doc: dict) -> None:
        n = doc.get("npc") or {}
        coords = n.get("coords") or []
        zone = zone_name(n.get("zoneid"))
        if len(coords) > 1 and zone:
            key = (zone, round(float(coords[0]), 1), round(float(coords[1]), 1))
            if key in seen:
                return
            seen.add(key)
            # partials name the quest ids this alt gives
            qnames = {str((p.get("obj") or {}).get("i", "")): (p.get("obj") or {}).get("n", "")
                      for p in doc.get("partials") or [] if p.get("type") == "quest"}
            locations.append({
                "zone": zone,
                "x": float(coords[0]), "y": float(coords[1]),
                "quests": [qnames.get(str(q), "") for q in n.get("quests") or []
                           if qnames.get(str(q))],
            })

    _collect(base)
    for alt in (base.get("npc") or {}).get("alts") or []:
        d = _fetch(alt)
        if d:
            _collect(d)

    return {
        "id": str((base.get("npc") or {}).get("id", ident)),
        "name": (base.get("npc") or {}).get("name", ""),
        "url": web_url("npc", ident),
        "locations": locations,
        "source": SOURCE,
    }


def nodes_detail(nodes: list) -> list:
    """Merge each node's own record (coords, spawn window, folklore) into the item's
    node list. The item document names the node but carries no location — the coords
    live one fetch away in the node doc, and the coords are the entire point of the
    question "where do I gather this?".
    """
    out = []
    for n in nodes or []:
        full = node(n.get("id")) or {}
        out.append({**n,
                    "zone": full.get("zone") or n.get("zone", ""),
                    "x": full.get("x", 0.0), "y": full.get("y", 0.0),
                    "spawn_times": full.get("spawn_times") or [],
                    "uptime_minutes": full.get("uptime_minutes", 0),
                    "stars": full.get("stars", 0),
                    "folklore": full.get("folklore", "")})
    return out


def lookup(query: str) -> Item | None:
    """Search then fetch — the "look up this item by name" path."""
    hit = find(query, kind="item")
    return item(hit.id) if hit else None
