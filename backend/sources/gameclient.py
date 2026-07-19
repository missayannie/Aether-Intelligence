"""The installed FFXIV client as a data source.

This is the layer that lets the app answer from the player's OWN game files
instead of Garland Tools: sqpack.py reads the containers, exd_schema.json says
what the columns mean, and this module turns sheets into the record shapes the
rest of the backend already consumes (garland.py stays the fallback, so a
missing client or a failed validation degrades to network — never to an error).

Freshness is automatic by construction: the data IS the installed patch. The
derived indexes (search, reverse joins) are persisted per client version and
rebuilt in the background when ffxivgame.ver changes — see ensure_index().

The one moving part is the SCHEMA: column MEANINGS are community knowledge and
a patch can reorder them. validate() checks ground-truth rows at startup;
a failure flips available() off and every consumer falls back to network
sources until tools/derive_schema.py is re-run. Fail safe, not wrong.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
from pathlib import Path

from paths import DATA_DIR
from sources.sqpack import GameData

SOURCE = "FFXIV game client"

_SCHEMA_PATH = Path(__file__).parent / "exd_schema.json"
_DB_PATH = DATA_DIR / "gameclient.sqlite"

_GAME_DIR: Path | None | bool = False     # False = not probed yet
_GD: GameData | None = None
_SCHEMA: dict | None = None
_VALID: bool | None = None
_LOCK = threading.Lock()


# ---------------------------------------------------------------- discovery

def find_game_dir() -> Path | None:
    """The client's `game` directory, or None. Checks the stock installer
    path, Steam, and the launcher's registry key; a user override via
    FFXIV_GAME_DIR wins over all of them."""
    env = os.environ.get("FFXIV_GAME_DIR")
    candidates = [Path(env)] if env else []
    candidates += [
        Path(r"C:\Program Files (x86)\SquareEnix\FINAL FANTASY XIV - A Realm Reborn\game"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\FINAL FANTASY XIV Online\game"),
        Path(r"C:\Program Files\SquareEnix\FINAL FANTASY XIV - A Realm Reborn\game"),
    ]
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
                r"\{2B41E132-07DF-4925-A3D3-F2D1765CCDFE}") as k:
            loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            candidates.append(Path(loc) / "FINAL FANTASY XIV - A Realm Reborn" / "game")
    except OSError:
        pass
    for c in candidates:
        if (c / "sqpack" / "ffxiv" / "0a0000.win32.index2").exists():
            return c
    return None


def game_dir() -> Path | None:
    global _GAME_DIR
    if _GAME_DIR is False:
        _GAME_DIR = find_game_dir()
    return _GAME_DIR


def _data() -> GameData | None:
    global _GD
    if _GD is None:
        d = game_dir()
        if d is None:
            return None
        _GD = GameData(d)
    return _GD


def _schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        try:
            _SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except Exception:
            _SCHEMA = {}
    return _SCHEMA


def version() -> str:
    gd = _data()
    return gd.version() if gd else ""


# ---------------------------------------------------------------- field access

def _cols(sheet: str, fields: list[str]) -> list[int] | None:
    m = (_schema().get(sheet) or {}).get("fields") or {}
    out = []
    for f in fields:
        if f not in m:
            return None
        out.append(m[f])
    return out


def get(sheet: str, row_id: int, fields: list[str]) -> dict | None:
    """One row's named fields, or None (row or schema missing)."""
    gd = _data()
    cols = _cols(sheet, fields)
    if gd is None or cols is None:
        return None
    try:
        vals = gd.sheet(sheet).row(row_id, cols=cols)
    except KeyError:
        return None
    if vals is None:
        return None
    return dict(zip(fields, vals))


def rows(sheet: str, fields: list[str]):
    """Iterate (row_id, {field: value}) over a whole sheet — index building."""
    gd = _data()
    cols = _cols(sheet, fields)
    if gd is None or cols is None:
        return
    for rid, vals in gd.sheet(sheet).rows(cols=cols):
        yield rid, dict(zip(fields, vals))


def subrows(sheet: str, row_id: int, fields: list[str]) -> list[dict]:
    gd = _data()
    cols = _cols(sheet, fields)
    if gd is None or cols is None:
        return []
    try:
        return [dict(zip(fields, v)) for v in gd.sheet(sheet).subrows(row_id, cols=cols)]
    except KeyError:
        return []


def array(rec: dict | None, name: str) -> list:
    """The Field[0..n] entries of a fetched record, in order."""
    if not rec:
        return []
    out = []
    i = 0
    while f"{name}[{i}]" in rec:
        out.append(rec[f"{name}[{i}]"])
        i += 1
    return out


def _array_fields(sheet: str, name: str) -> list[str]:
    m = (_schema().get(sheet) or {}).get("fields") or {}
    out = []
    i = 0
    while f"{name}[{i}]" in m:
        out.append(f"{name}[{i}]")
        i += 1
    return out


# ---------------------------------------------------------------- validation

def validate() -> bool:
    """Ground-truth check: a few rows whose values we KNOW. A patch that
    reorders columns fails here, and the app falls back to network sources
    instead of serving garbage."""
    gd = _data()
    checks = _schema().get("_validate") or []
    if gd is None or not checks:
        return False
    try:
        for sheet, rid, fld, want in checks:
            rec = get(sheet, rid, [fld])
            if rec is None:
                return False
            got = rec[fld]
            if isinstance(want, str):
                if " ".join(str(got).split()).lower() != " ".join(want.split()).lower():
                    return False
            elif got != want:
                return False
    except Exception:
        return False
    return True


def available() -> bool:
    """True when a client is installed AND the schema validates against it.
    Memoised per process — a patch mid-session flips it on next restart (the
    version watcher triggers the rebuild path, which re-checks)."""
    global _VALID
    if _VALID is None:
        with _LOCK:
            if _VALID is None:
                _VALID = validate()
    return _VALID


def _reset_validation() -> None:
    global _VALID
    _VALID = None


def status() -> dict:
    d = game_dir()
    return {"game_dir": str(d) if d else None, "version": version(),
            "schema_ok": available(), "index_ready": index_ready()}


# ---------------------------------------------------------------- index

# The derived database: a cross-type name index (Garland's search.php
# replacement) plus the reverse joins Garland's pipeline precomputes (what
# nodes drop an item, what recipes use it, who sells it). Rebuilt whenever the
# CLIENT version changes — the build reads only local files, so "content
# updates itself the moment the launcher finishes patching".

_GIL_SHOP_RANGE = range(262144, 262144 + 8192)   # GilShop row-id block

# What each search kind indexes: (sheet, name field, level field, icon field).
_HIT_SHEETS = {
    "item": ("Item", "Name", "LevelItem", "Icon"),
    "instance": ("ContentFinderCondition", "Name", "ClassJobLevelRequired", None),
    "npc": ("ENpcResident", "Singular", None, None),
    "quest": ("Quest", "Name", None, None),
    "achievement": ("Achievement", "Name", None, "Icon"),
    "mob": ("BNpcName", "Singular", None, None),
    "fate": ("Fate", "Name", "ClassJobLevel", None),
    "leve": ("Leve", "Name", "ClassJobLevel", None),
}


def _norm_name(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def index_ready() -> bool:
    if not _DB_PATH.exists():
        return False
    try:
        with _conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        return bool(row and row[0] == version())
    except sqlite3.Error:
        return False


def build_index(progress=None) -> bool:
    """(Re)build the derived database from the client's sheets. Idempotent and
    resumable: writes to a temp file, swaps in atomically, stamps the client
    version — a killed build just runs again from scratch (it's ~a minute of
    local reads, there is nothing worth checkpointing)."""
    if not available():
        return False
    tmp = _DB_PATH.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE hits(kind TEXT, id INT, name TEXT, norm TEXT,
                              ilvl INT DEFAULT 0, icon INT DEFAULT 0);
            CREATE TABLE items(id INT PRIMARY KEY, name TEXT, ilvl INT,
                               uicat INT, jobcat INT, equipslot INT);
            CREATE TABLE item_nodes(item INT, gpbase INT);
            CREATE TABLE recipe_uses(ingredient INT, result INT, qty INT);
            CREATE TABLE item_shops(item INT, npc INT);
            CREATE TABLE item_ventures(item INT, task INT);
            CREATE TABLE gp_points(gpbase INT, gp INT, territory INT, place INT);
            CREATE TABLE npc_levels(obj INT, x REAL, z REAL, territory INT);
            CREATE TABLE npc_quests(npc INT, quest INT);
        """)
        cur = conn.cursor()

        def note(msg):
            if progress:
                progress(msg)

        # -- cross-type name index --------------------------------------
        for kind, (sheet, name_f, lvl_f, icon_f) in _HIT_SHEETS.items():
            note(f"indexing {kind}s")
            fields = [name_f] + ([lvl_f] if lvl_f else []) + ([icon_f] if icon_f else [])
            for rid, rec in rows(sheet, fields):
                name = (rec.get(name_f) or "").strip()
                if not name:
                    continue
                cur.execute(
                    "INSERT INTO hits VALUES (?,?,?,?,?,?)",
                    (kind, rid, name, _norm_name(name),
                     int(rec.get(lvl_f) or 0) if lvl_f else 0,
                     int(rec.get(icon_f) or 0) if icon_f else 0))

        # -- items core table (upgrade-chain queries need these columns) --
        note("indexing item details")
        for rid, rec in rows("Item", ["Name", "LevelItem", "ItemUICategory",
                                      "ClassJobCategory", "EquipSlotCategory"]):
            if rec.get("Name"):
                cur.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)",
                            (rid, rec["Name"], int(rec.get("LevelItem") or 0),
                             int(rec.get("ItemUICategory") or 0),
                             int(rec.get("ClassJobCategory") or 0),
                             int(rec.get("EquipSlotCategory") or 0)))

        # -- gathering: node names + which nodes yield which items --------
        note("indexing gathering")
        gi_fields = ["Item"]
        gi_item = {rid: int(rec.get("Item") or 0)
                   for rid, rec in rows("GatheringItem", gi_fields)}
        item_fields = _array_fields("GatheringPointBase", "Item")
        gpb_lvl: dict[int, int] = {}
        for rid, rec in rows("GatheringPointBase", ["GatheringLevel"] + item_fields):
            gpb_lvl[rid] = int(rec.get("GatheringLevel") or 0)
            for f in item_fields:
                gi = int(rec.get(f) or 0)
                # Item[] slots hold GatheringItem row ids (small) — resolve to
                # the real item.
                item_id = gi_item.get(gi, 0)
                if item_id:
                    cur.execute("INSERT INTO item_nodes VALUES (?,?)", (item_id, rid))
        for rid, rec in rows("GatheringPoint",
                             ["GatheringPointBase", "TerritoryType", "PlaceName"]):
            base = int(rec.get("GatheringPointBase") or 0)
            if base:
                cur.execute("INSERT INTO gp_points VALUES (?,?,?,?)",
                            (base, rid, int(rec.get("TerritoryType") or 0),
                             int(rec.get("PlaceName") or 0)))
        # nodes join the search index under their in-world place name
        note("indexing nodes")
        seen_nodes = set()
        for base, gp, terr, place in cur.execute(
                "SELECT gpbase, gp, territory, place FROM gp_points").fetchall():
            if base in seen_nodes or not place:
                continue
            seen_nodes.add(base)
            pn = get("PlaceName", place, ["Name"]) or {}
            name = (pn.get("Name") or "").strip()
            if name:
                cur.execute("INSERT INTO hits VALUES (?,?,?,?,?,?)",
                            ("node", base, name, _norm_name(name),
                             gpb_lvl.get(base, 0), 0))

        # -- recipes: what each item crafts into --------------------------
        note("indexing recipes")
        ing_fields = _array_fields("Recipe", "Ingredient")
        amt_fields = _array_fields("Recipe", "AmountIngredient")
        for rid, rec in rows("Recipe", ["ItemResult"] + ing_fields + amt_fields):
            result = int(rec.get("ItemResult") or 0)
            if result <= 0:
                continue
            for f, a in zip(ing_fields, amt_fields):
                ing = int(rec.get(f) or 0)
                if ing > 0:
                    cur.execute("INSERT INTO recipe_uses VALUES (?,?,?)",
                                (ing, result, int(rec.get(a) or 0)))

        # -- ventures ------------------------------------------------------
        note("indexing ventures")
        for rid, rec in rows("RetainerTaskNormal", ["Item"]):
            item_id = int(rec.get("Item") or 0)
            if item_id:
                cur.execute("INSERT INTO item_ventures VALUES (?,?)", (item_id, rid))

        # -- vendors: ENpcBase data slots that hold GilShop ids ------------
        # No schema needed: scan every integer column for values in the
        # GilShop id block — the data slots are the only place those appear.
        note("indexing vendors")
        gd = _data()
        shop_items: dict[int, list[int]] = {}
        for rid, vals in gd.sheet("ENpcBase").rows():
            shops = [v for v in vals
                     if isinstance(v, int) and v in _GIL_SHOP_RANGE]
            for shop in shops:
                if shop not in shop_items:
                    shop_items[shop] = [
                        int(s.get("Item") or 0)
                        for s in subrows("GilShopItem", shop, ["Item"])]
                for item_id in shop_items[shop]:
                    if item_id:
                        cur.execute("INSERT INTO item_shops VALUES (?,?)",
                                    (item_id, rid))

        # -- NPC placements (Level rows whose Object is an ENpc) -----------
        note("indexing NPC locations")
        for rid, rec in rows("Level", ["X", "Z", "Object", "Territory"]):
            obj = int(rec.get("Object") or 0)
            if 1000000 <= obj < 1100000:      # ENpc id block
                cur.execute("INSERT INTO npc_levels VALUES (?,?,?,?)",
                            (obj, float(rec.get("X") or 0.0),
                             float(rec.get("Z") or 0.0),
                             int(rec.get("Territory") or 0)))

        # -- quests each NPC gives ------------------------------------------
        note("indexing quest givers")
        for rid, rec in rows("Quest", ["Name", "IssuerStart"]):
            npc = int(rec.get("IssuerStart") or 0)
            if npc and rec.get("Name"):
                cur.execute("INSERT INTO npc_quests VALUES (?,?)", (npc, rid))

        conn.executescript("""
            CREATE INDEX ix_hits_norm ON hits(norm);
            CREATE INDEX ix_hits_kind ON hits(kind, norm);
            CREATE INDEX ix_nodes_item ON item_nodes(item);
            CREATE INDEX ix_uses_ing ON recipe_uses(ingredient);
            CREATE INDEX ix_shops_item ON item_shops(item);
            CREATE INDEX ix_vent_item ON item_ventures(item);
            CREATE INDEX ix_gpp_base ON gp_points(gpbase);
            CREATE INDEX ix_lvl_obj ON npc_levels(obj);
            CREATE INDEX ix_nq_npc ON npc_quests(npc);
            CREATE INDEX ix_items_cat ON items(uicat, jobcat, ilvl);
        """)
        cur.execute("INSERT INTO meta VALUES ('version', ?)", (version(),))
        conn.commit()
    finally:
        conn.close()
    # Atomic swap: readers either see the old complete index or the new one.
    import os as _os
    _os.replace(tmp, _DB_PATH)
    return True


def ensure_index(background: bool = True) -> None:
    """Build (or rebuild after a patch) if needed. Called at startup — with
    background=True the app serves from network fallbacks until it lands."""
    if not available() or index_ready():
        return
    if background:
        threading.Thread(target=build_index, daemon=True,
                         name="gameclient-index").start()
    else:
        build_index()


def ready() -> bool:
    """Local answers possible right now: client + valid schema + fresh index."""
    return available() and index_ready()


# ---------------------------------------------------------------- providers
# Everything below returns GARLAND-SHAPED data (see garland.py's dataclasses)
# so garland.py can serve local-first without its callers changing.

def search_hits(query: str, kind: str = "", limit: int = 20) -> list[dict]:
    """[{type, id, name, item_level, icon}] — icon is the GAME icon id.
    Exact name first, then prefix, then substring — mirrors Garland's feel."""
    q = _norm_name(query)
    if not q or not ready():
        return []
    sql = """SELECT kind, id, name, ilvl, icon FROM hits
             WHERE norm LIKE ?{kind}
             ORDER BY (norm = ?) DESC, (norm LIKE ?) DESC, length(norm), name
             LIMIT ?"""
    args: list = ["%" + q + "%"]
    if kind:
        args.append(kind)
    args += [q, q + "%", limit]
    with _conn() as c:
        rws = c.execute(sql.format(kind=" AND kind = ?" if kind else ""),
                        args).fetchall()
    return [{"type": k, "id": str(i), "name": n, "item_level": lv, "icon": ic}
            for k, i, n, lv, ic in rws]


def _zone_of_territory(terr: int) -> str:
    rec = get("TerritoryType", terr, ["PlaceName"]) or {}
    pn = get("PlaceName", int(rec.get("PlaceName") or 0), ["Name"]) or {}
    return pn.get("Name") or ""


def _map_of_territory(terr: int) -> dict:
    rec = get("TerritoryType", terr, ["Map"]) or {}
    return get("Map", int(rec.get("Map") or 0),
               ["SizeFactor", "OffsetX", "OffsetY"]) or {}


def world_to_game(world: float, size_factor: int, offset: int = 0) -> float:
    """Level-sheet world units -> the in-game flag coordinate players read."""
    c = (size_factor or 100) / 100.0
    return round(41.0 / c * (((world + offset) * c + 1024.0) / 2048.0) + 1.0, 1)


def item_record(ident) -> dict | None:
    """Garland item()-shaped dict, built from local sheets + derived joins."""
    if not ready():
        return None
    try:
        item_id = int(ident)
    except (TypeError, ValueError):
        return None
    rec = get("Item", item_id, ["Name", "Description", "Icon", "LevelItem",
                                "ItemUICategory", "ClassJobCategory",
                                "MateriaSlotCount", "PriceLow", "IsUntradable",
                                "EquipSlotCategory",
                                "DamagePhys", "DamageMag",
                                "DefensePhys", "DefenseMag"])
    if not rec or not rec.get("Name"):
        return None
    full = get("Item", item_id,
               _array_fields("Item", "BaseParam") + _array_fields("Item", "BaseParamValue"))
    attrs: dict[str, int] = {}
    params = array(full, "BaseParam")
    values = array(full, "BaseParamValue")
    for p, v in zip(params, values):
        if p and v:
            name = (get("BaseParam", int(p), ["Name"]) or {}).get("Name")
            if name:
                attrs[name] = int(v)
    for label, key in (("Physical Damage", "DamagePhys"), ("Magic Damage", "DamageMag"),
                       ("Defense", "DefensePhys"), ("Magic Defense", "DefenseMag")):
        if int(rec.get(key) or 0):
            attrs.setdefault(label, int(rec[key]))

    uicat = (get("ItemUICategory", int(rec.get("ItemUICategory") or 0), ["Name"]) or {})
    jobcat = (get("ClassJobCategory", int(rec.get("ClassJobCategory") or 0), ["Name"]) or {})

    with _conn() as c:
        node_ids = [r[0] for r in c.execute(
            "SELECT DISTINCT gpbase FROM item_nodes WHERE item=?", (item_id,))]
        uses = c.execute(
            """SELECT r.result, i.name, i.ilvl, r.qty FROM recipe_uses r
               JOIN items i ON i.id = r.result
               WHERE r.ingredient=? LIMIT 25""", (item_id,)).fetchall()
        vendor_npcs = [r[0] for r in c.execute(
            "SELECT DISTINCT npc FROM item_shops WHERE item=? LIMIT 12", (item_id,))]
        ventures = [r[0] for r in c.execute(
            "SELECT task FROM item_ventures WHERE item=?", (item_id,))]
        # Upgrade chain, computed: gear in the same UI category wearable by the
        # same job category, nearest item levels above/below. Garland curates
        # theirs by hand; this reproduces the useful core of "what replaces it".
        me = c.execute("SELECT ilvl, uicat, jobcat, equipslot FROM items WHERE id=?",
                       (item_id,)).fetchone()
        upgrades, downgrades = [], []
        if me and me[3]:      # equippable only — no upgrade chain for carrots
            ilvl, ucat, jcat, _slot = me
            upgrades = c.execute(
                """SELECT id, name, ilvl FROM items
                   WHERE uicat=? AND jobcat=? AND ilvl>? ORDER BY ilvl LIMIT 3""",
                (ucat, jcat, ilvl)).fetchall()
            downgrades = c.execute(
                """SELECT id, name, ilvl FROM items
                   WHERE uicat=? AND jobcat=? AND ilvl<? AND ilvl>0
                   ORDER BY ilvl DESC LIMIT 3""",
                (ucat, jcat, ilvl)).fetchall()

    nodes = []
    for nid in node_ids[:10]:
        n = node_record(nid, items=False)
        if n:
            nodes.append({"id": str(nid), "name": n["name"], "level": n["level"],
                          "type": n.get("limit_type", ""), "zone": n["zone"]})
    vendors = []
    for npc_id in vendor_npcs:
        n = get("ENpcResident", npc_id, ["Singular"]) or {}
        if n.get("Singular"):
            vendors.append({"id": str(npc_id), "name": n["Singular"]})

    return {
        "id": str(item_id),
        "name": rec["Name"],
        "item_level": int(rec.get("LevelItem") or 0),
        "category": uicat.get("Name") or "",
        "jobs": jobcat.get("Name") or "",
        "description": (rec.get("Description") or "").strip(),
        "icon": int(rec.get("Icon") or 0),
        "sockets": int(rec.get("MateriaSlotCount") or 0),
        "attributes": attrs,
        "upgrades": [{"id": str(i), "name": n, "item_level": lv} for i, n, lv in upgrades],
        "downgrades": [{"id": str(i), "name": n, "item_level": lv} for i, n, lv in downgrades],
        "sell_price": int(rec.get("PriceLow") or 0),
        "tradeable": not rec.get("IsUntradable"),
        "nodes": nodes,
        "ventures": [str(v) for v in ventures],
        "ingredient_of": [{"id": str(i), "name": n, "item_level": lv, "qty": q}
                          for i, n, lv, q in uses],
        "vendors": vendors,
    }


# GatheringType id -> Garland-style node type int (matches icons.NODE_TYPE_ICON
# int keys: 0 mining, 1 quarrying, 2 logging, 3 harvesting, 4 spearfishing,
# 5 fishing). The sheet uses the same order.
def _decode_pop_times(table_id: int) -> tuple[list[int], int]:
    """Unspoiled-node spawn hours (ET) and uptime minutes from the client's
    rare-pop timetable. StartTime is HHMM-packed (800 = 8:00 ET); 65535 marks
    an unused slot."""
    if not table_id:
        return [], 0
    fields = (_array_fields("GatheringRarePopTimeTable", "StartTime")
              + _array_fields("GatheringRarePopTimeTable", "Duration"))
    if not fields:
        return [], 0
    rec = get("GatheringRarePopTimeTable", table_id, fields) or {}
    times: list[int] = []
    uptime = 0
    for k, v in rec.items():
        if not isinstance(v, int) or v in (0, 65535):
            continue
        if k.startswith("StartTime") and v < 2400:
            times.append(v // 100)
        elif k.startswith("Duration") and not uptime:
            # HHMM-packed like StartTime: 160 = 1h60m = 120 minutes.
            uptime = (v // 100) * 60 + (v % 100)
    return sorted(set(times)), uptime


def node_record(ident, items: bool = True) -> dict | None:
    """Garland node()-shaped dict from local sheets."""
    if not ready():
        return None
    try:
        base_id = int(ident)
    except (TypeError, ValueError):
        return None
    base = get("GatheringPointBase", base_id,
               ["GatheringType", "GatheringLevel"] + _array_fields("GatheringPointBase", "Item"))
    if not base:
        return None
    with _conn() as c:
        pts = c.execute(
            "SELECT gp, territory, place FROM gp_points WHERE gpbase=?",
            (base_id,)).fetchall()
    if not pts:
        return None
    gp, terr, place = pts[0]
    name = ((get("PlaceName", place, ["Name"]) or {}).get("Name") or "").strip()
    zone = _zone_of_territory(terr)
    egp = get("ExportedGatheringPoint", base_id, ["X", "Y", "Radius"]) or {}
    # EGP X/Y are WORLD units (same space as Level rows), not the flag
    # coordinates players read — convert with the territory's map scale.
    m = _map_of_territory(terr)
    sf = int(m.get("SizeFactor") or 100)
    gx = world_to_game(float(egp.get("X") or 0.0), sf, int(m.get("OffsetX") or 0))
    gy = world_to_game(float(egp.get("Y") or 0.0), sf, int(m.get("OffsetY") or 0))
    trans = get("GatheringPointTransient", gp,
                ["GatheringRarePopTimeTable", "EphemeralStartTime", "EphemeralEndTime"]) or {}
    times, uptime = _decode_pop_times(int(trans.get("GatheringRarePopTimeTable") or 0))
    eph = int(trans.get("EphemeralStartTime") or 0) not in (0, 65535)
    limit_type = ("Ephemeral" if eph else "Unspoiled" if times else "")

    item_names = []
    if items:
        for f in _array_fields("GatheringPointBase", "Item"):
            gi = int(base.get(f) or 0)
            if not gi:
                continue
            item_id = int((get("GatheringItem", gi, ["Item"]) or {}).get("Item") or 0)
            if item_id:
                nm = (get("Item", item_id, ["Name"]) or {}).get("Name")
                if nm:
                    item_names.append(nm)

    gtype = int(base.get("GatheringType") or 0)
    return {
        "id": str(base_id),
        "name": name,
        "zone": zone,
        "level": int(base.get("GatheringLevel") or 0),
        "type": limit_type,          # garland's `type` is the LIMIT type string
        "limit_type": limit_type,
        "type_id": gtype,
        "stars": 0,
        "x": gx,
        "y": gy,
        "spawn_times": times,
        "uptime_minutes": uptime,
        "folklore": "",
        "items": item_names,
        "radius": float(egp.get("Radius") or 0.0),
    }


def icon_png(icon_id: int) -> bytes | None:
    """One game icon rendered to PNG straight from the client, or None when
    the icon uses a format tex.py doesn't decode (caller falls back to
    XIVAPI's render)."""
    gd = _data()
    if gd is None or not icon_id:
        return None
    from sources import tex
    folder = f"{icon_id // 1000 * 1000:06d}"
    raw = gd.texture(f"ui/icon/{folder}/{icon_id:06d}.tex")
    if not raw:
        return None
    decoded = tex.decode_rgba(raw)
    if not decoded:
        return None
    rgba, w, h = decoded
    return tex.to_png(rgba, w, h)


def map_png(map_id: str) -> bytes | None:
    """A zone map composed exactly the way the game does it: the base texture
    multiplied by its terrain mask when one exists (field zones), the base
    alone otherwise (city maps ship pre-baked). map_id is the Map sheet's Id,
    e.g. "w1t2/02"."""
    gd = _data()
    if gd is None or "/" not in (map_id or ""):
        return None
    from sources import tex
    folder, ver = map_id.split("/", 1)
    stem = f"ui/map/{folder}/{ver}/{folder}{ver}"
    base_raw = gd.texture(f"{stem}_m.tex")
    if not base_raw:
        return None
    base = tex.decode_rgba(base_raw)
    if not base:
        return None
    rgba, w, h = base
    mask_raw = gd.texture(f"{stem}m_m.tex")
    if mask_raw:
        mask = tex.decode_rgba(mask_raw)
        if mask and mask[1] == w and mask[2] == h:
            rgba = tex.multiply(rgba, mask[0])
    return tex.to_png(rgba, w, h)


def npc_record(ident) -> dict | None:
    """Garland npc_locations()-shaped dict: every placement + quests given."""
    if not ready():
        return None
    try:
        npc_id = int(ident)
    except (TypeError, ValueError):
        return None
    res = get("ENpcResident", npc_id, ["Singular"]) or {}
    name = (res.get("Singular") or "").strip()
    if not name:
        return None
    with _conn() as c:
        levels = c.execute(
            "SELECT x, z, territory FROM npc_levels WHERE obj=?", (npc_id,)).fetchall()
        quest_ids = [r[0] for r in c.execute(
            "SELECT quest FROM npc_quests WHERE npc=?", (npc_id,)).fetchall()]
    qnames = []
    for qid in quest_ids[:12]:
        q = get("Quest", qid, ["Name"]) or {}
        # City-start quest variants share a name ("Close to Home" ×3) — one is
        # plenty for "which NPC gives what".
        if q.get("Name") and q["Name"] not in qnames:
            qnames.append(q["Name"])
    locations, seen = [], set()
    for x, z, terr in levels:
        m = _map_of_territory(terr)
        zone = _zone_of_territory(terr)
        if not zone or not m:
            continue
        gx = world_to_game(x, int(m.get("SizeFactor") or 100), int(m.get("OffsetX") or 0))
        gy = world_to_game(z, int(m.get("SizeFactor") or 100), int(m.get("OffsetY") or 0))
        key = (zone, round(gx, 1), round(gy, 1))
        if key in seen:
            continue
        seen.add(key)
        locations.append({"zone": zone, "x": gx, "y": gy, "quests": qnames})
    return {"id": str(npc_id), "name": name, "locations": locations,
            "source": SOURCE}

