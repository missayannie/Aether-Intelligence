"""Derive EXD column mappings by cross-referencing XIVAPI (dev tool).

The EXH header describes column TYPES and OFFSETS but not names — "which column
of Item is the item level" is community knowledge. Rather than hand-transcribe
it (and typo it), this script asks XIVAPI v2 for sample rows WITH field names,
then finds the local column whose decoded values match across every sample.
The result is written to backend/sources/exd_schema.json, which is committed:
at runtime the app never needs XIVAPI for schema, and gameclient.py validates
ground-truth rows at startup so a patch that reorders columns fails safe
(fall back to network sources) instead of serving garbage.

Run after a patch if validation starts failing:
    backend\\.venv\\Scripts\\python.exe tools\\derive_schema.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sources.sqpack import GameData          # noqa: E402
from sources import garland                  # noqa: E402  (session only)
from sources.gameclient import find_game_dir  # noqa: E402

V2 = "https://v2.xivapi.com/api"

# Sheet -> (fields to map, sample row ids). Arrays map as Field[i].
WANTED: dict[str, tuple[list[str], list[int]]] = {
    "Item": (["Name", "Description", "Icon", "LevelItem", "LevelEquip",
              "ItemUICategory", "ClassJobCategory", "MateriaSlotCount",
              "PriceLow", "PriceMid", "IsUntradable", "EquipSlotCategory",
              "Rarity", "StackSize", "CanBeHq",
              "DamagePhys", "DamageMag", "DefensePhys", "DefenseMag", "DelayMs",
              "BaseParam", "BaseParamValue"],
             [5111, 5057, 4551, 44162, 23767]),
    "BaseParam": (["Name"], [1, 3, 45]),
    "GatheringType": (["Name"], [0, 2, 4]),
    "GatheringSubCategory": (["Item", "FolkloreBook"], [1, 5, 9]),
    # 427 ("The Goblet") separates Name from NameNoArticle ("Goblet").
    "PlaceName": (["Name"], [40, 271, 223, 427]),
    "Map": (["Id", "SizeFactor", "MapMarkerRange", "PlaceName",
             "PlaceNameRegion", "PlaceNameSub", "TerritoryType",
             "OffsetX", "OffsetY"],
            [13, 20, 21, 83]),
    "TerritoryType": (["Name", "PlaceName", "PlaceNameRegion", "Map", "Aetheryte"],
                      [130, 140, 141, 341]),
    "Aetheryte": (["PlaceName", "Territory", "Map", "IsAetheryte", "AethernetName"],
                  [17, 9, 53, 8]),
    "ClassJob": (["Name", "Abbreviation"], [1, 19, 22]),
    "ClassJobCategory": (["Name"], [1, 30, 32]),
    "ItemUICategory": (["Name"], [15, 47, 58]),
    "EquipSlotCategory": (["MainHand", "OffHand", "Head", "Body", "Gloves",
                           "Waist", "Legs", "Feet", "Ears", "Neck", "Wrists",
                           "FingerL", "FingerR"], [1, 3, 5]),
    "GatheringItem": (["Item", "GatheringItemLevel", "IsHidden"],
                      [10001, 10100, 10300]),
    "GatheringPointBase": (["GatheringType", "GatheringLevel", "Item"],
                           [30, 100, 400]),
    "GatheringPoint": (["GatheringPointBase", "TerritoryType", "PlaceName"],
                       [30, 100, 400]),
    "ExportedGatheringPoint": (["X", "Y", "Radius", "GatheringType"],
                               [30, 100, 400]),
    "GatheringPointTransient": (["GatheringRarePopTimeTable",
                                 "EphemeralStartTime", "EphemeralEndTime"],
                                [30, 100, 400]),
    "GatheringRarePopTimeTable": (["StartTime", "Duration"], []),
    "GilShop": (["Name"], [262144, 262145, 262200]),
    "Recipe": (["ItemResult", "AmountResult", "CraftType", "RecipeLevelTable",
                "Ingredient", "AmountIngredient"],
               [1, 100, 3000, 33000]),
    "ENpcResident": (["Singular", "Title"], [1000236, 1001637, 1005422]),
    "Level": (["X", "Y", "Z", "Territory", "Object", "Map"],
              [5659015, 6913217, 197145]),
    "ContentFinderCondition": (["Name", "ContentType", "ClassJobLevelRequired",
                                "ClassJobLevelSync", "Image"], [1, 30, 200]),
    "Quest": (["Name", "JournalGenre", "PlaceName", "IssuerStart", "GilReward"],
              [65564, 66043, 69414]),
    "Achievement": (["Name", "Description", "Icon", "Points",
                     "AchievementCategory"], [1, 500, 2000]),
    "BNpcName": (["Singular"], [2, 442, 4000]),
    "Fate": (["Name", "Description", "ClassJobLevel", "ClassJobLevelMax",
              "Location", "Icon"], [120, 500, 1600]),
    "Leve": (["Name", "Description", "ClassJobLevel", "GilReward",
              "ClassJobCategory", "PlaceNameIssued", "LevelLevemete"],
             [21, 200, 800]),
    "FishingSpot": (["GatheringLevel", "X", "Z", "Radius", "TerritoryType",
                     "PlaceName", "Item"], [1, 100, 250]),
    "JournalGenre": (["Name", "JournalCategory", "Icon"], [1, 30, 80]),
    "Action": (["Name", "Icon"], [2260, 3577, 7395]),
    "CraftAction": (["Name", "Icon"], [100090, 100128, 100315]),
    "Status": (["Name", "Icon"], [1, 50, 1000]),
    "ContentType": (["Name", "Icon"], [2, 4, 5]),
    "RetainerTaskNormal": (["Item"], [1, 100, 400]),
}


def _flatten(fields: dict, prefix: str = "") -> dict:
    """XIVAPI field payload -> {name: scalar}. Row links become their id
    (matches the local int column); icons their id; arrays index as Name[i]."""
    out = {}
    for k, v in fields.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            if "value" in v:
                out[name] = v["value"]
            elif "id" in v:
                out[name] = v["id"]
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    if "value" in x:
                        out[f"{name}[{i}]"] = x["value"]
                    elif "id" in x:
                        out[f"{name}[{i}]"] = x["id"]
                else:
                    out[f"{name}[{i}]"] = x
        else:
            out[name] = v
    return out


def _norm(v):
    if isinstance(v, str):
        return " ".join(v.split()).lower()
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, bool):
        return v
    return v


def _matches(local, remote) -> bool:
    a, b = _norm(local), _norm(remote)
    # Bools compare only against 0/1-ish values — "any nonzero int matches a
    # True column" once made one packed-bool column tie with EVERY numeric
    # field in the Map sheet.
    if isinstance(a, bool) or isinstance(b, bool):
        aa = a if isinstance(a, bool) else (a in (0, 1) and bool(a))
        bb = b if isinstance(b, bool) else (b in (0, 1) and bool(b))
        if not isinstance(a, bool) and a not in (0, 1):
            return False
        if not isinstance(b, bool) and b not in (0, 1):
            return False
        return bool(aa) == bool(bb)
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 0.05
        except (TypeError, ValueError):
            return False
    return a == b


def _auto_ids(sheet) -> list[int]:
    """Row ids spread across the sheet — explicit ids miss sheets whose ids
    are sparse or offset (Level starts in the millions, GatheringPoint at
    tens of thousands)."""
    ids = []
    for start, _count in sheet.pages:
        _, offsets = sheet._page(start)
        ids.extend(offsets)
    if not ids:
        return []
    ids.sort()
    picks = [ids[int(len(ids) * f)] for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
    return sorted(set(picks))


def _disambiguate(sheet, sheet_name: str, key: str, cands: set, s) -> set:
    """Shrink a candidate set using rows where the tied columns disagree."""
    field = key.split("[")[0]
    tried = 0
    for start, _count in sheet.pages:
        if tried >= 3 or len(cands) <= 1:
            break
        _, offsets = sheet._page(start)
        for rid in offsets:
            vals = (sheet.subrows(rid)[0] if sheet.variant == 2 else sheet.row(rid))
            if vals is None:
                continue
            picked = {vals[i] for i in cands if isinstance(vals[i], (int, float, str, bool))}
            if len(picked) <= 1:
                continue        # columns still identical on this row
            r = s.get(f"{V2}/sheet/{sheet_name}/{rid}",
                      params={"fields": field}, timeout=30)
            if r.status_code != 200:
                continue
            remote = _flatten(r.json().get("fields") or {})
            if key not in remote:
                continue
            cands = {i for i in cands if _matches(vals[i], remote[key])} or cands
            tried += 1
            if len(cands) <= 1:
                break
    return cands


def derive(gd: GameData) -> dict:
    s = garland._session()
    schema: dict = {}
    problems = []
    try:
        for sheet_name, (fields, sample_ids) in WANTED.items():
            sheet = gd.sheet(sheet_name)
            samples = []
            for rid in list(dict.fromkeys(sample_ids + _auto_ids(sheet))):
                local = (sheet.subrows(rid)[0] if sheet.variant == 2
                         else sheet.row(rid))
                if local is None:
                    continue
                r = s.get(f"{V2}/sheet/{sheet_name}/{rid}",
                          params={"fields": ",".join(fields)}, timeout=30)
                if r.status_code != 200:
                    continue
                samples.append((local, _flatten(r.json().get("fields") or {})))
            if not samples:
                problems.append(f"{sheet_name}: no usable samples")
                continue
            wanted_keys = sorted({k for _, remote in samples for k in remote})
            mapping = {}
            for key in wanted_keys:
                cands = None
                counts: dict[int, int] = {}
                n_samples = 0
                for local, remote in samples:
                    if key not in remote:
                        continue
                    n_samples += 1
                    here = {i for i, v in enumerate(local)
                            if _matches(v, remote[key])}
                    for i in here:
                        counts[i] = counts.get(i, 0) + 1
                    cands = here if cands is None else (cands & here)
                if not cands and counts:
                    # One odd row (localized casing, SeString variance) can kill
                    # a strict intersection — fall back to majority vote.
                    best = max(counts.values())
                    if best >= max(2, n_samples - 2):
                        cands = {i for i, c in counts.items() if c == best}
                if cands and len(cands) > 1:
                    # Twin-column tie: the samples never told the columns apart.
                    # Hunt the sheet for a row where the tied columns DIFFER,
                    # fetch that one row remotely, and re-intersect.
                    cands = _disambiguate(sheet, sheet_name, key, cands, s)
                if not cands:
                    problems.append(f"{sheet_name}.{key}: no matching column")
                elif len(cands) == 1:
                    mapping[key] = cands.pop()
                else:
                    mapping[key] = min(cands)
                    problems.append(
                        f"{sheet_name}.{key}: ambiguous {sorted(cands)[:6]} -> {mapping[key]}")
            schema[sheet_name] = {"fields": mapping, "variant": sheet.variant}
            print(f"{sheet_name}: mapped {len(mapping)}/{len(wanted_keys)}")
    finally:
        s.close()

    # Subrow (variant 2) sheets can't be sampled through XIVAPI's row API the
    # same way — these mappings are hand-verified against live data instead
    # (GilShopItem 262144 subrow 0 col 0 == item 4594, checked in tests).
    schema["GilShopItem"] = {"fields": {"Item": 0}, "variant": 2}

    # Ground-truth rows for the runtime validation gate.
    schema["_validate"] = [
        ["PlaceName", 40, "Name", "Ul'dah - Steps of Nald"],
        ["PlaceName", 271, "Name", "Horizon"],
        ["Item", 5111, "Name", "Iron Ore"],
        ["Item", 5111, "PriceLow", 1],
        ["Map", 13, "Id", "w1t1/01"],
        ["Aetheryte", 17, "PlaceName", 271],
    ]
    schema["_problems"] = problems
    return schema


if __name__ == "__main__":
    game = find_game_dir()
    if not game:
        sys.exit("no game client found")
    gd = GameData(game)
    schema = derive(gd)
    out = ROOT / "backend" / "sources" / "exd_schema.json"
    out.write_text(json.dumps(schema, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    for p in schema["_problems"]:
        print("  ⚠", p)
