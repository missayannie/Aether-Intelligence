"""GarlandDB browse + per-kind detail — the data behind the GarlandDB tab.

Browse mirrors garlandtools.org's own Browse tool: one JSON index per kind
(``/db/doc/browse/en/2/<kind>.json``), grouped here the way Garland's UI groups
them (quests by saga section, instances by content type, mobs/fates/leves by
level bucket, nodes/fishing/NPCs by region). Grouping happens server-side so
the frontend just renders.

Details come from the per-kind doc endpoints. Every doc ships ``partials`` —
the names of each referenced id — so cross-references (rewards, quest chains,
fish lists) resolve to real names without extra requests.
"""
from __future__ import annotations

from . import garland
from .icons import NODE_TYPE_ICON

def _radius_coords(radius, size_factor: int = 100) -> float:
    """A node/fishing `radius` as map COORDINATES — Garland's own drawn size.

    Derived from gt.js, not eyeballed: their map draws
        radius_px = toMapCoordinate(radius) * 2π,  toMapCoordinate(v) = v/40.96
    at 50*(SizeFactor/100) px per coordinate, with a 15px minimum. In
    coordinate units that is radius * 2π / (2048 * sf/100), floored at ~0.3.
    """
    if not radius:
        return 0.0
    c = (size_factor or 100) / 100.0
    return round(max(radius * 6.28318 / (2048.0 * c), 0.3 / c), 2)


def _zone_size_factor(zone: str) -> int:
    try:
        from . import gamemap
        return (gamemap._map_index().get((zone or "").strip().lower()) or {}) \
            .get("size_factor", 100)
    except Exception:
        return 100

BASE = garland.BASE

# Doc schema versions differ by kind; 2 is the default, these are the odd ones.
_DOC_VER = {"item": "3", "leve": "3", "core": "3"}

# The kinds the Browse tool offers — Items first, then Garland's toolbar order.
# (Garland's site has no item browse index at all — it only SEARCHES items — so
# the item catalogue is built from the game's Item sheet instead, see
# _browse_items.)
BROWSE_KINDS = (
    "item", "patch", "action", "status", "achievement", "instance", "quest",
    "fate", "leve", "node", "fishing", "npc", "mob",
)
BROWSE_LABEL = {
    "item": "Items",
    "patch": "Patches", "action": "Actions", "status": "Status Effects",
    "achievement": "Achievements", "instance": "Instances", "quest": "Quests",
    "fate": "FATEs", "leve": "Leves", "node": "Gathering Nodes",
    "fishing": "Fishing Spots", "npc": "NPCs", "mob": "Mobs",
}

# Where each kind's icons live under /files/icons/. Item icons are handled by
# garland.py; these are the extra folders the detail views need.
_ICON_FOLDER = {"achievement": "achievement", "action": "action", "status": "status"}


def _doc(kind: str, ident) -> dict:
    ver = _DOC_VER.get(kind, "2")
    d = garland._get_json(f"{BASE}/db/doc/{kind}/en/{ver}/{ident}.json")
    return d if isinstance(d, dict) else {}


_CORE: dict | None = None


def _core() -> dict:
    """The whole core data doc (location/genre/job/patch indexes), memoised."""
    global _CORE
    if _CORE is None:
        try:
            ver = _DOC_VER.get("core", "3")
            d = garland._get_json(f"{BASE}/db/doc/core/en/{ver}/data.json")
            _CORE = d if isinstance(d, dict) else {}
        except Exception:
            return {}   # transient failure: don't memoise emptiness
    return _CORE


def _zone_name(zid) -> str:
    loc = (_core().get("locationIndex") or {}).get(str(zid)) or {}
    return loc.get("name", "")


def _region_name(zid) -> str:
    """The region a zone belongs to ("Yak T'el" -> "Yok Tural")."""
    idx = _core().get("locationIndex") or {}
    loc = idx.get(str(zid)) or {}
    parent = idx.get(str(loc.get("parentId"))) or {}
    name = parent.get("name") or ""
    return name if name and parent.get("id") != loc.get("id") else (loc.get("name") or "")


def _job_category(jid) -> str:
    jc = (_core().get("jobCategories") or {}).get(str(jid)) or {}
    return jc.get("name", "")


def _browse_rows(kind: str) -> list[dict]:
    doc = garland._get_json(f"{BASE}/db/doc/browse/en/2/{kind}.json")
    return (doc or {}).get("browse") or [] if isinstance(doc, dict) else []


def _lvl_bucket(lvl, size: int = 5) -> tuple[int, str]:
    """(sort_key, label) for a level bucket, on GARLAND'S boundaries.

    Their browse groups run 1-4, 5-9, 10-14 … (multiples of `size`, with the
    first bucket starting at 1) — verified against their live UI, not assumed.
    """
    try:
        n = int(str(lvl).split("-")[0].strip().split(" ")[0])
    except (ValueError, AttributeError):
        return (999, "Level ??")
    base = max(n, 0) // size * size
    return (base, f"Level {max(base, 1)}-{base + size - 1}")


def _grouped(pairs) -> list[dict]:
    """[(sort_key, label, row)] -> [{label, count, rows}] sorted by key."""
    groups: dict[str, dict] = {}
    for key, label, row in pairs:
        g = groups.setdefault(label, {"key": key, "label": label, "rows": []})
        g["rows"].append(row)
    out = sorted(groups.values(), key=lambda g: (g["key"], g["label"]))
    for g in out:
        g["count"] = len(g["rows"])
        del g["key"]
    return out


_BROWSE_MEMO: dict[str, dict] = {}


def browse(kind: str) -> dict:
    """{kind, groups:[{label, count, rows:[{id, name, sub, icon?}]}]} for one kind."""
    if kind in _BROWSE_MEMO:
        return _BROWSE_MEMO[kind]
    if kind == "patch":
        out = _browse_patches()
    elif kind == "item":
        out = _browse_items()
    else:
        rows = _browse_rows(kind)
        builder = {
            "quest": _g_quest, "instance": _g_instance, "mob": _g_mob,
            "fate": _g_fate, "leve": _g_leve, "node": _g_zone_kind,
            "fishing": _g_zone_kind, "npc": _g_npc,
            "achievement": _g_achievement, "action": _g_action, "status": _g_status,
        }.get(kind)
        out = {"kind": kind, "groups": builder(rows) if builder else []}
    _enrich_icons(kind, out["groups"])
    if out["groups"] and not out.get("partial"):
        _BROWSE_MEMO[kind] = out
    return out


def _enrich_icons(kind: str, groups: list) -> None:
    """Give every browse row its own icon, read straight from the LOCAL client
    sheets (ids in browse rows are the game's own). Whole-list enrichment is
    cheap — a per-row read is a couple of dict lookups once the sheet page is
    hot — and the memo above keeps the result for the session. Kinds with no
    per-record art (npc, mob, leve, patch) stay icon-less on purpose."""
    from sources import gameclient as gc
    if not gc.available():
        return
    import os
    base = f"http://127.0.0.1:{os.environ.get('FFXIV_BACKEND_PORT', '8756')}"

    def by_id(icon_id) -> str:
        return f"{base}/map/icon?id={int(icon_id)}" if icon_id else ""

    def sheet_icon(sheet: str):
        return lambda r: by_id((gc.get(sheet, int(r["id"]), ["Icon"]) or {}).get("Icon") or 0)

    def action_icon(r):
        # Garland's action browse mixes two sheets: battle actions (Action) and
        # crafting actions, which live at ids 100000+ in CraftAction.
        sheet = "CraftAction" if int(r["id"]) >= 100000 else "Action"
        return by_id((gc.get(sheet, int(r["id"]), ["Icon"]) or {}).get("Icon") or 0)

    def quest_icon(r):
        q = gc.get("Quest", int(r["id"]), ["JournalGenre"]) or {}
        g = gc.get("JournalGenre", int(q.get("JournalGenre") or 0), ["Icon"]) or {}
        return by_id(g.get("Icon") or 0)

    def instance_icon(r):
        c = gc.get("ContentFinderCondition", int(r["id"]), ["ContentType"]) or {}
        t = gc.get("ContentType", int(c.get("ContentType") or 0), ["Icon"]) or {}
        # Several content types (chaotic raids, seasonal) ship no icon —
        # the generic duty symbol keeps the list visually consistent.
        return by_id(t.get("Icon") or 0) or f"{base}/icons/by-name/dungeon"

    def fate_icon(r):
        # Garland's fate ids are their OWN, not Fate sheet rows — resolve the
        # real row through the local search index by name.
        with gc._conn() as c:
            row = c.execute("SELECT id FROM hits WHERE kind='fate' AND norm=? LIMIT 1",
                            (gc._norm_name(r["name"]),)).fetchone()
        if not row:
            return ""
        f = gc.get("Fate", int(row[0]), ["Icon"]) or {}
        return by_id(f.get("Icon") or 0)

    def node_icon(r):
        from sources.icons import NODE_TYPE_ICON
        b = gc.get("GatheringPointBase", int(r["id"]), ["GatheringType"]) or {}
        gtype = b.get("GatheringType")
        name = NODE_TYPE_ICON.get(int(gtype) if gtype is not None else -1)
        return f"{base}/icons/by-name/{name}" if name else ""

    resolver = {
        "item": sheet_icon("Item"), "action": action_icon,
        "status": sheet_icon("Status"), "achievement": sheet_icon("Achievement"),
        "quest": quest_icon, "instance": instance_icon, "fate": fate_icon,
        "node": node_icon,
        "fishing": lambda r: f"{base}/icons/by-name/fishing",
    }.get(kind)
    if not resolver:
        return
    for g in groups:
        for r in g["rows"]:
            try:
                icon = resolver(r)
            except (TypeError, ValueError, KeyError):
                icon = ""
            if icon:
                r["icon"] = icon


def _browse_items() -> dict:
    """Every item, grouped by its in-game category (Ninja's Arm, Medicine…).

    Built from the game's own Item sheet via XIVAPI — Garland's site has no
    item browse index to mirror. ~80 pages of 500; each page is disk-cached
    (patch-purged), so the slow build happens once per patch.
    """
    from . import gamemap
    pairs, after, complete = [], "", False
    for _ in range(120):
        url = ("https://v2.xivapi.com/api/sheet/Item"
               f"?limit=500&fields=Name,LevelItem,ItemUICategory.Name{after}")
        try:
            d = gamemap._get(url)
        except Exception:
            break                     # truncated build: usable, but not memoised
        got = (d or {}).get("rows") or []
        for r in got:
            f = r.get("fields") or {}
            name = f.get("Name") or ""
            if not name:
                continue
            cat = (((f.get("ItemUICategory") or {}).get("fields") or {})
                   .get("Name")) or "Other"
            lvl = f.get("LevelItem")
            ilvl = (lvl.get("row_id") if isinstance(lvl, dict) else lvl) or 0
            pairs.append((cat, cat, {"id": str(r.get("row_id")), "name": name,
                                     "sub": f"iLv {ilvl}" if ilvl > 1 else "",
                                     "_ilvl": ilvl}))
        if len(got) < 500:
            complete = True
            break
        after = f"&after={got[-1]['row_id']}"
    groups = sorted(_grouped(pairs), key=lambda g: g["label"])
    for g in groups:
        g["rows"].sort(key=lambda r: (-r.get("_ilvl", 0), r["name"]))
        for r in g["rows"]:
            r.pop("_ilvl", None)
    out = {"kind": "item", "groups": groups}
    if not complete:
        out["partial"] = True         # shown, but never memoised incomplete
    return out


def _browse_patches() -> dict:
    idx = (_core().get("patch") or {}).get("partialIndex") or {}
    series_order: list[str] = []
    pairs = []
    for pid, p in idx.items():
        series = p.get("series") or "Other"
        if series not in series_order:
            series_order.append(series)
        pairs.append((series_order.index(series), series,
                      {"id": str(pid), "name": f"{pid} — {p.get('name', '')}", "sub": ""}))
    groups = _grouped(pairs)
    # Newest expansion first, like Garland.
    groups.reverse()
    for g in groups:
        g["rows"].sort(key=lambda r: [int(x) if x.isdigit() else 0
                                      for x in r["id"].split(".")], reverse=True)
    return {"kind": "patch", "groups": groups}


def _g_quest(rows):
    genres = _core().get("questGenreIndex") or {}
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        g = genres.get(str(r.get("g"))) or {}
        section = g.get("section") or "Other Quests"
        sub = ", ".join(x for x in (g.get("category"), r.get("l")) if x)
        pairs.append((section, section, {"id": str(r["i"]), "name": r["n"], "sub": sub}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_instance(rows):
    pairs = [(r.get("t") or "Other", r.get("t") or "Other",
              {"id": str(r["i"]), "name": r["n"],
               "sub": f"Lv {r.get('min_lvl', '?')}–{r.get('max_lvl', '?')}"})
             for r in rows if r.get("n")]
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_mob(rows):
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        key, label = _lvl_bucket(r.get("l"))
        pairs.append((key, label, {"id": str(r["i"]), "name": r["n"],
                                   "sub": _zone_name(r.get("z")) or f"Lv {r.get('l', '?')}"}))
    return _grouped(pairs)


def _g_fate(rows):
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        key, label = _lvl_bucket(r.get("l"))
        pairs.append((key, label, {"id": str(r["i"]), "name": r["n"],
                                   "sub": r.get("t") or ""}))
    return _grouped(pairs)


def _g_leve(rows):
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        key, label = _lvl_bucket(r.get("l"), 10)
        pairs.append((key, label, {"id": str(r["i"]), "name": r["n"],
                                   "sub": f"Lv {r.get('l', '?')} · {_job_category(r.get('j'))}"}))
    return _grouped(pairs)


def _g_zone_kind(rows):
    """Nodes and fishing spots: grouped by region, sub = the zone."""
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        region = _region_name(r.get("z")) or "Unknown"
        zone = _zone_name(r.get("z"))
        pairs.append((region, region, {"id": str(r["i"]), "name": r["n"],
                                       "sub": f"{zone} · Lv {r.get('l', '?')}"}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_npc(rows):
    pairs = []
    for r in rows:
        n = r.get("n") or ""
        if not n or n.startswith("Nameless"):
            continue
        region = _region_name(r.get("l")) or "Other"
        zone = _zone_name(r.get("l"))
        sub = " · ".join(x for x in (zone, r.get("t")) if x)
        pairs.append((region, region, {"id": str(r["i"]), "name": n, "sub": sub}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_achievement(rows):
    cats = _core().get("achievementCategoryIndex") or {}
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        cat = cats.get(str(r.get("t"))) or {}
        kind = cat.get("kind") or "Other"
        pairs.append((kind, kind, {"id": str(r["i"]), "name": r["n"],
                                   "sub": cat.get("name") or ""}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_action(rows):
    jobs = {str(j.get("id")): j for j in (_core().get("jobs") or [])
            if isinstance(j, dict)} if isinstance(_core().get("jobs"), list) else {}
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        j = jobs.get(str(r.get("j"))) or {}
        label = j.get("name") or "Other"
        pairs.append((label, label, {"id": str(r["i"]), "name": r["n"],
                                     "sub": f"Lv {r.get('l', '?')}"}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


def _g_status(rows):
    kinds = {1: "Beneficial", 2: "Detrimental"}
    pairs = []
    for r in rows:
        if not r.get("n"):
            continue
        label = kinds.get(r.get("t"), "Other")
        pairs.append((label, label, {"id": str(r["i"]), "name": r["n"], "sub": ""}))
    return sorted(_grouped(pairs), key=lambda x: x["label"])


# ---------------------------------------------------------------------------
# Per-kind detail
# ---------------------------------------------------------------------------

def _partials(doc: dict) -> dict:
    """(type, id) -> {name, ...} from the doc's partials block."""
    out = {}
    for p in doc.get("partials") or []:
        obj = p.get("obj") or {}
        out[(p.get("type"), str(p.get("id")))] = obj
    return out


def _refs(parts: dict, kind: str, ids, lvl: bool = False) -> list[dict]:
    """Resolve ids of one kind into clickable refs, dropping unknowns.

    Item refs carry the item's ICON (partials ship `c`, the icon id) — that's
    what makes a drops/fish/rewards list look like Garland's own, not a wall of
    text. lvl=True also surfaces the partial's level (fish lists show it).
    """
    seen, out = set(), []
    for i in ids or []:
        i = str(i.get("id") if isinstance(i, dict) else i)
        if not i or i in seen:
            continue
        seen.add(i)
        obj = parts.get((kind, i)) or {}
        name = obj.get("n")
        if not name:
            continue
        ref = {"kind": kind, "id": i, "name": name}
        c = obj.get("c")
        if kind == "item" and isinstance(c, (int, str)) and str(c).isdigit():
            ref["icon"] = garland.ICON_URL.format(icon=c)
        if lvl and obj.get("l"):
            ref["sub"] = f"Lv {obj['l']}"
        out.append(ref)
    return out


def _icon(kind: str, icon_id) -> str:
    folder = _ICON_FOLDER.get(kind)
    return f"{BASE}/files/icons/{folder}/{icon_id}.png" if folder and icon_id else ""


def detail(kind: str, ident: str) -> dict:
    """One record of any non-item kind, in a uniform render-ready shape."""
    builder = {
        "instance": _d_instance, "quest": _d_quest, "npc": _d_npc, "mob": _d_mob,
        "achievement": _d_achievement, "fate": _d_fate, "leve": _d_leve,
        "node": _d_node, "fishing": _d_fishing, "action": _d_action,
        "status": _d_status, "patch": _d_patch,
    }.get(kind)
    base = {"found": False, "kind": kind, "id": str(ident),
            "url": garland.web_url(kind, ident)}
    if not builder:
        return base
    try:
        got = builder(str(ident))
    except Exception:
        got = None
    if not got:
        return base
    got.update({"kind": kind, "id": str(ident), "found": True,
                "url": garland.web_url(kind, ident)})
    return got


def _d_instance(ident):
    doc = _doc("instance", ident)
    it = doc.get("instance") or {}
    if not it.get("name"):
        return None
    parts = _partials(doc)
    roles = [f"{it[k]} {k}" for k in ("tank", "healer", "melee", "ranged") if it.get(k)]
    fields = [
        {"label": "Time limit", "value": f"{it['time']} min"} if it.get("time") else None,
        {"label": "Party", "value": ", ".join(roles)} if roles else None,
        {"label": "Patch", "value": str(it.get("patch") or "")},
    ]
    links = []
    if it.get("unlockedByQuest"):
        links.append({"group": "Unlocked by", "refs": _refs(parts, "quest", [it["unlockedByQuest"]])})
    drops = _refs(parts, "item", it.get("rewards"))
    for f in it.get("fights") or []:
        drops += _refs(parts, "item", (f.get("coffer") or {}).get("items"))
    for c in it.get("coffers") or []:
        drops += _refs(parts, "item", c.get("items"))
    seen = set()
    drops = [d for d in drops if not (d["id"] in seen or seen.add(d["id"]))]
    if drops:
        links.append({"group": f"Rewards & drops ({len(drops)})", "refs": drops})
    return {
        "name": it["name"], "icon": "",
        # The duty's splash banner, like Garland's own header.
        "image": (f"{BASE}/files/icons/instance/{it['fullIcon']}.png"
                  if it.get("fullIcon") else ""),
        "sub": " · ".join(x for x in (it.get("category"),
                                      f"Lv {it.get('min_lvl', '?')}–{it.get('max_lvl', '?')}") if x),
        "description": garland.clean_html(it.get("description") or ""),
        "fields": [f for f in fields if f and f["value"]],
        "location": None, "links": links,
    }


def _d_quest(ident):
    doc = _doc("quest", ident)
    q = doc.get("quest") or {}
    if not q.get("name"):
        return None
    parts = _partials(doc)
    genre = (_core().get("questGenreIndex") or {}).get(str(q.get("genre"))) or {}
    reward = q.get("reward") or {}
    fields = [
        {"label": "XP", "value": f"{reward['xp']:,}"} if reward.get("xp") else None,
        {"label": "Gil", "value": f"{reward['gil']:,}"} if reward.get("gil") else None,
        {"label": "Patch", "value": str(q.get("patch") or "")},
    ]
    links = []
    for group, kind, ids in [
        ("Given by", "npc", [q.get("issuer")] if q.get("issuer") else []),
        ("Rewards", "item", reward.get("items")),
        ("Requires", "quest", (q.get("reqs") or {}).get("quests")),
        ("Leads to", "quest", q.get("next")),
    ]:
        refs = _refs(parts, kind, ids)
        if refs:
            links.append({"group": group, "refs": refs})
    # Pin the quest giver if Garland knows where they stand.
    location = None
    if q.get("issuer"):
        locs = (garland.npc_locations(str(q["issuer"])) or {}).get("locations") or []
        with_xy = next((l for l in locs if l.get("x")), None)
        if with_xy:
            location = {"zone": with_xy["zone"], "x": with_xy["x"], "y": with_xy["y"],
                        "icon": "quest",
                        "label": (parts.get(("npc", str(q["issuer"]))) or {}).get("n", "Quest giver")}
    return {
        "name": q["name"],
        "icon": (f"{BASE}/files/icons/event/{q['eventIcon']}.png"
                 if q.get("eventIcon") else ""),
        "sub": " · ".join(x for x in (genre.get("category"), q.get("location")) if x),
        "description": "",
        "fields": [f for f in fields if f and f["value"]],
        "location": location, "links": links,
    }


def _d_npc(ident):
    got = garland.npc_locations(ident)
    if not got or not got.get("name"):
        return None
    doc = _doc("npc", ident)
    npc = doc.get("npc") or {}
    parts = _partials(doc)
    locs = got.get("locations") or []
    quest_ids = [k[1] for k in parts if k[0] == "quest"]
    links = []
    refs = _refs(parts, "quest", quest_ids)
    if refs:
        links.append({"group": "Quests", "refs": refs})
    # What they wear — Garland's Equipment section, as clickable item refs.
    gear = _refs(parts, "item", npc.get("equipment"))
    if gear:
        links.append({"group": "Equipment", "refs": gear})
    with_xy = next((l for l in locs if l.get("x")), None)
    location = ({"zone": with_xy["zone"], "x": with_xy["x"], "y": with_xy["y"],
                 "label": got["name"]} if with_xy else None)
    fields = [{"label": "Also at",
               "value": "; ".join(f"{l['zone']} ({l['x']:.1f}, {l['y']:.1f})" if l.get("x")
                                  else l["zone"] for l in locs[1:6])}] if len(locs) > 1 else []
    # Appearance summary, like Garland's Info tab header line.
    app = npc.get("appearance") or {}
    sub_bits = [locs[0]["zone"] if locs else ""]
    if app.get("gender") or app.get("race"):
        sub_bits.append(f"{app.get('gender', '')} {app.get('race', '')}".strip())
    # Portrait preference: the community wiki's infobox screenshot (the REAL
    # in-game look, and no Garland dependency) via our cached /npc/photo proxy;
    # Garland's model render only when the wiki has none. The lookup is cheap
    # after the first view — the wiki client caches page data on disk.
    image = ""
    try:
        from llm.tools import _wiki
        hit = _wiki.lookup("consolegames", got["name"])
        if hit and hit.image_url:
            import os
            from urllib.parse import quote as _q
            port = os.environ.get("FFXIV_BACKEND_PORT", "8756")
            image = f"http://127.0.0.1:{port}/npc/photo?name={_q(got['name'])}"
    except Exception:
        pass
    if not image and npc.get("photo"):
        image = f"{BASE}/files/photos/npc/{npc['photo']}"
    return {"name": got["name"], "icon": "",
            "image": image,
            "sub": " · ".join(x for x in sub_bits if x),
            "description": "", "fields": fields, "location": location, "links": links}


def _d_mob(ident):
    doc = _doc("mob", ident)
    m = doc.get("mob") or {}
    if not m.get("name"):
        return None
    parts = _partials(doc)
    zone = _zone_name(m.get("zoneid"))
    links = []
    drops = _refs(parts, "item", m.get("drops"))
    if drops:
        links.append({"group": f"Drops ({len(drops)})", "refs": drops})
    return {
        "name": m["name"], "icon": "", "icon_name": "mob",
        "sub": f"Lv {m.get('lvl', '?')}" + (f" · {zone}" if zone else ""),
        "description": "", "fields": [],
        "location": ({"zone": zone, "x": 0, "y": 0, "label": m["name"], "icon": "mob"}
                     if zone else None),
        "links": links,
    }


def _d_achievement(ident):
    doc = _doc("achievement", ident)
    a = doc.get("achievement") or {}
    if not a.get("name"):
        return None
    cat = (_core().get("achievementCategoryIndex") or {}).get(str(a.get("category"))) or {}
    fields = [
        {"label": "Points", "value": str(a.get("points") or "")},
        {"label": "Title reward", "value": a.get("title") or ""},
        {"label": "Patch", "value": str(a.get("patch") or "")},
    ]
    return {
        "name": a["name"], "icon": _icon("achievement", a.get("icon")),
        "sub": " · ".join(x for x in (cat.get("kind"), cat.get("name")) if x),
        "description": garland.clean_html(a.get("description") or ""),
        "fields": [f for f in fields if f["value"]], "location": None, "links": [],
    }


def _d_fate(ident):
    doc = _doc("fate", ident)
    f = doc.get("fate") or {}
    if not f.get("name"):
        return None
    parts = _partials(doc)
    zone = _zone_name(f.get("zoneid"))
    coords = f.get("coords") or []
    links = []
    refs = _refs(parts, "item", f.get("items"))
    if refs:
        links.append({"group": "Rewards", "refs": refs})
    lvl = f"Lv {f.get('lvl', '?')}" + (f"–{f['maxlvl']}" if f.get("maxlvl") else "")
    return {
        "name": f["name"], "icon": "", "icon_name": "fate",
        "sub": " · ".join(x for x in (f.get("type"), lvl, zone) if x),
        "description": garland.clean_html(f.get("description") or ""), "fields": [],
        "location": ({"zone": zone, "x": coords[0], "y": coords[1], "label": f["name"],
                      "icon": "fate"}
                     if zone and len(coords) >= 2 else None),
        "links": links,
    }


def _d_leve(ident):
    doc = _doc("leve", ident)
    l = doc.get("leve") or {}
    if not l.get("name"):
        return None
    parts = _partials(doc)
    fields = [
        {"label": "Client", "value": l.get("client") or ""},
        {"label": "XP", "value": f"{l['xp']:,}"} if l.get("xp") else None,
        {"label": "Gil", "value": f"{l['gil']:,}"} if l.get("gil") else None,
        {"label": "Patch", "value": str(l.get("patch") or "")},
    ]
    links = []
    if l.get("levemete"):
        refs = _refs(parts, "npc", [l["levemete"]])
        if refs:
            links.append({"group": "Levemete", "refs": refs})
    location = None
    if l.get("levemete"):
        locs = (garland.npc_locations(str(l["levemete"])) or {}).get("locations") or []
        with_xy = next((x for x in locs if x.get("x")), None)
        if with_xy:
            location = {"zone": with_xy["zone"], "x": with_xy["x"], "y": with_xy["y"],
                        "label": "Levemete", "icon": "quest"}
    return {
        "name": l["name"], "icon": "",
        "sub": f"Lv {l.get('lvl', '?')} · {_job_category(l.get('jobCategory'))}",
        "description": garland.clean_html(l.get("description") or ""),
        "fields": [f for f in fields if f and f["value"]],
        "location": location, "links": links,
    }


def _d_node(ident):
    n = garland.node(ident)
    doc = _doc("node", ident)
    raw = doc.get("node") or {}
    if not (n and n.get("name")):
        return None
    parts = _partials(doc)
    fields = []
    if n.get("spawn_times"):
        hrs = ", ".join(f"{h:02d}:00" for h in n["spawn_times"])
        fields.append({"label": "Spawns", "value": f"{hrs} ET, up {n.get('uptime_minutes', 0)}m"})
    if n.get("folklore"):
        fields.append({"label": "Requires", "value": n["folklore"]})
    links = []
    refs = _refs(parts, "item", raw.get("items"))
    if refs:
        links.append({"group": "Items", "refs": refs})
    star = " " + "★" * n["stars"] if n.get("stars") else ""
    # Type-matched marker: the node's own gathering icon + its cluster radius,
    # so the map shows the AREA the points spawn in, like Garland's map does.
    node_icon = (NODE_TYPE_ICON.get(str(n.get("type") or "").strip().lower())
                 or NODE_TYPE_ICON.get(raw.get("type")) or "mining")
    return {
        "name": n["name"], "icon": "", "icon_name": node_icon,
        "sub": f"Lv {n.get('level', '?')}{star} {n.get('type') or ''} · {n.get('zone') or ''}".strip(),
        "description": "", "fields": fields,
        "location": ({"zone": n["zone"], "x": n["x"], "y": n["y"], "label": n["name"],
                      "icon": node_icon,
                      "radius": _radius_coords(raw.get("radius") or 0,
                                               _zone_size_factor(n["zone"]))}
                     if n.get("zone") and n.get("x") else None),
        "links": links,
    }


def _d_fishing(ident):
    doc = _doc("fishing", ident)
    f = doc.get("fishing") or {}
    if not f.get("name"):
        return None
    parts = _partials(doc)
    zone = _zone_name(f.get("zoneid"))
    links = []
    refs = _refs(parts, "item", f.get("items"), lvl=True)
    if refs:
        links.append({"group": f"Fish ({len(refs)})", "refs": refs})
    return {
        "name": f["name"], "icon": "", "icon_name": "fishing",
        "sub": f"Lv {f.get('lvl', '?')} fishing spot · {zone}",
        "description": "", "fields": [],
        "location": ({"zone": zone, "x": f["x"], "y": f["y"], "label": f["name"],
                      "icon": "fishing",
                      "radius": _radius_coords(f.get("radius") or 0,
                                               _zone_size_factor(zone))}
                     if zone and f.get("x") else None),
        "links": links,
    }


def _d_action(ident):
    doc = _doc("action", ident)
    a = doc.get("action") or {}
    if not a.get("name"):
        return None
    jobs = _core().get("jobs")
    job = ""
    if isinstance(jobs, list):
        job = next((j.get("name", "") for j in jobs
                    if isinstance(j, dict) and j.get("id") == a.get("job")), "")
    # Cost can be a NUMBER with a resource ("50 MP") or a STATUS REFERENCE
    # ({"id": 496, "name": "Mudra", …}) — printing the raw dict once put
    # "{'id': 496, 'name': 'Mudra'…}" in a player-facing chip.
    cost = a.get("cost")
    if isinstance(cost, dict):
        cost_text = cost.get("name", "")
    else:
        cost_text = f"{cost} {a.get('resource', '')}".strip() if cost else ""
    fields = [
        {"label": "Cast", "value": f"{a['cast']}s"} if a.get("cast") else None,
        {"label": "Recast", "value": f"{a['recast'] / 1000:g}s"} if a.get("recast") else None,
        {"label": "Cost", "value": cost_text} if cost_text else None,
        {"label": "Range", "value": f"{a['range']}y"} if a.get("range") else None,
        {"label": "Patch", "value": str(a.get("patch") or "")},
    ]
    return {
        "name": a["name"], "icon": _icon("action", a.get("icon")),
        "sub": " · ".join(x for x in (job, f"Lv {a.get('lvl', '?')}") if x),
        "description": garland.clean_html(a.get("description") or ""),
        "fields": [f for f in fields if f and f["value"]], "location": None, "links": [],
    }


def _d_status(ident):
    doc = _doc("status", ident)
    s = doc.get("status") or {}
    if not s.get("name"):
        return None
    kind = {1: "Beneficial", 2: "Detrimental"}.get(s.get("category"), "")
    fields = [{"label": "Dispellable", "value": "yes" if s.get("canDispel") else "no"},
              {"label": "Patch", "value": str(s.get("patch") or "")}]
    return {
        "name": s["name"], "icon": _icon("status", s.get("icon")), "sub": kind,
        "description": garland.clean_html(s.get("description") or ""),
        "fields": [f for f in fields if f["value"]], "location": None, "links": [],
    }


def _d_patch(ident):
    p = ((_core().get("patch") or {}).get("partialIndex") or {}).get(str(ident)) or {}
    if not p.get("name"):
        return None
    return {"name": f"Patch {ident} — {p['name']}", "icon": "",
            "sub": p.get("series") or "", "description": "",
            "fields": [], "location": None, "links": []}
