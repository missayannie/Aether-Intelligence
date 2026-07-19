"""Profile workspaces — the character profiles the app switches between.

Every workspace is one FFXIV character, and every chat belongs to one via its
`owner` field (a workspace slug). There is no "global" workspace: context that
applies to all characters lives in ONE shared file (paths.SHARED_PROFILE_PATH),
read into every workspace's prompt, and individual docs/notes/assets can be marked
`shared` to be visible from other profiles.

Storage (under DATA_DIR/profile/):
    _index.json                 [{slug, display_name, character_id, kind}]
    _shared.md                  cross-profile context (was the global workspace)
    <slug>/profile.md, character.json, settings.json
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone

from paths import (
    workspace_index_path, profile_dir, profile_md, character_json,
    LEGACY_PROFILE_PATH, SHARED_PROFILE_PATH, GLOBAL_SLUG, CHATS_DIR,
)

# Used when a fresh install has no profiles yet, so the app is usable immediately;
# the player can bind a real Lodestone character to it later.
DEFAULT_DISPLAY = "My Character"
SHARED_PROFILE_DEFAULT = (
    "# Shared context\n\n"
    "Context that applies across all your characters — server region, preferred "
    "answer style, content you care about. The assistant reads this in every "
    "profile, alongside that character's own profile.\n"
)
PROFILE_TEMPLATE = (
    "# Player profile\n\n"
    "## Goals\n(What are you working toward?)\n\n"
    "## Playstyle\n(Casual, savage raider, crafter, roleplayer…?)\n\n"
    "## Preferences\n(How much detail do you want in answers?)\n"
)
IDENTITY_HEADING = "## Identity (imported from the Lodestone)"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "profile"


def _read_index() -> list[dict]:
    p = workspace_index_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _write_index(items: list[dict]) -> None:
    workspace_index_path().write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _iter_chats():
    if not CHATS_DIR.exists():
        return
    for d in CHATS_DIR.iterdir():
        f = d / "chat.json"
        if not f.exists():
            continue
        try:
            yield f, json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue


def _reassign_chats(from_slug: str, to_slug: str) -> int:
    """Re-owner every chat belonging to `from_slug`. Chats are never deleted."""
    n = 0
    for f, data in _iter_chats():
        if data.get("owner") == from_slug:
            data["owner"] = to_slug
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            n += 1
    return n


def ensure_migration() -> None:
    """Idempotent upgrade to the no-global-workspace model.

    Every chat now belongs to a character profile, and cross-character context lives
    in _shared.md rather than a "global" workspace. This:
      1. seeds _shared.md from the old global workspace's profile (or legacy
         player.md), so that shared context is preserved rather than dropped;
      2. guarantees at least one real profile exists (fresh installs get a default,
         so the app is usable before any Lodestone character is bound);
      3. re-owners the old global workspace's chats onto a real profile and retires
         the global entry;
      4. tags any untagged chat with an owner.
    Safe to run repeatedly — every step is conditional.
    """
    idx = _read_index()

    # 1. Preserve cross-profile context: prefer the old global profile, then legacy.
    if not SHARED_PROFILE_PATH.exists():
        old_global = profile_md(GLOBAL_SLUG)
        seed = SHARED_PROFILE_DEFAULT
        for src in (old_global, LEGACY_PROFILE_PATH):
            if src.exists():
                try:
                    text = src.read_text(encoding="utf-8").strip()
                    if text:
                        seed = text + "\n"
                        break
                except OSError:
                    pass
        SHARED_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SHARED_PROFILE_PATH.write_text(seed, encoding="utf-8")

    # 2. There must always be at least one profile to own chats.
    profiles = [w for w in idx if w.get("slug") != GLOBAL_SLUG]
    if not profiles:
        entry = create_workspace(DEFAULT_DISPLAY)
        idx = _read_index()
        profiles = [entry]

    home = profiles[0]["slug"]  # where orphaned/global chats land

    # 3. Retire the global workspace: move its chats to a real profile, drop the entry.
    if any(w.get("slug") == GLOBAL_SLUG for w in idx):
        _reassign_chats(GLOBAL_SLUG, home)
        _write_index([w for w in idx if w.get("slug") != GLOBAL_SLUG])
        shutil.rmtree(profile_dir(GLOBAL_SLUG), ignore_errors=True)

    # 4. Any chat with a missing/unknown owner joins the home profile.
    known = {w["slug"] for w in _read_index()}
    for f, data in _iter_chats():
        if data.get("owner") not in known:
            data["owner"] = home
            f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_workspaces() -> list[dict]:
    ensure_migration()
    return _read_index()


def valid_slug(slug: str) -> bool:
    return any(w.get("slug") == slug for w in _read_index())


def default_slug() -> str:
    """The workspace a chat belongs to when none is specified — the first profile,
    creating one if this is a fresh install."""
    idx = _read_index()
    if not idx:
        return create_workspace(DEFAULT_DISPLAY)["slug"]
    return idx[0]["slug"]


def resolve_owner(owner: str | None) -> str:
    """Coerce an owner tag to a real workspace slug (unknown/absent -> first profile)."""
    return owner if owner and valid_slug(owner) else default_slug()


def get_shared_profile() -> str:
    """Cross-profile context, read into every workspace's prompt."""
    try:
        return SHARED_PROFILE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def set_shared_profile(content: str) -> None:
    SHARED_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARED_PROFILE_PATH.write_text(content, encoding="utf-8")


PREFERENCES_DEFAULT = (
    "# Agent preferences\n\n"
    "Standing instructions for how the assistant behaves, in every chat. The\n"
    "assistant adds a line here when you ask it to remember something 'from now\n"
    "on' — and you can edit or delete anything below.\n"
)


def get_preferences() -> str:
    """Standing behaviour preferences, read into EVERY chat's prompt."""
    from paths import PREFERENCES_PATH
    try:
        return PREFERENCES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def set_preferences(content: str) -> None:
    from paths import PREFERENCES_PATH
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFERENCES_PATH.write_text(content, encoding="utf-8")


def append_preference(preference: str, reason: str = "") -> str:
    """Add one standing preference line; returns the line as written.

    Append-only on the agent's side: the agent records, the PLAYER curates (the
    file is theirs to edit in the app). A rewrite tool would let one bad call
    wipe every preference they've accumulated.
    """
    line = f"- {preference.strip()}"
    if reason.strip():
        line += f" _(asked for: {reason.strip()})_"
    cur = get_preferences() or PREFERENCES_DEFAULT
    if not cur.endswith("\n"):
        cur += "\n"
    set_preferences(cur + line + "\n")
    return line


def create_workspace(display_name: str) -> dict:
    idx = _read_index()
    base = slugify(display_name)
    existing = {w["slug"] for w in idx}
    slug, n = base, 2
    while slug in existing:
        slug, n = f"{base}-{n}", n + 1
    profile_md(slug).write_text(PROFILE_TEMPLATE, encoding="utf-8")
    entry = {"slug": slug, "display_name": (display_name.strip() or slug),
             "character_id": "", "kind": "profile"}
    idx.append(entry)
    _write_index(idx)
    return entry


def delete_workspace(slug: str) -> None:
    """Delete a profile, moving its chats to another profile (never deleting them).
    The last remaining profile can't be deleted — chats must have an owner."""
    remaining = [w for w in _read_index() if w.get("slug") != slug]
    if not remaining:
        raise ValueError("Can't delete your only profile — chats need a profile to live in.")
    _reassign_chats(slug, remaining[0]["slug"])
    shutil.rmtree(profile_dir(slug), ignore_errors=True)
    _write_index(remaining)


def get_profile(slug: str) -> str:
    try:
        return profile_md(resolve_owner(slug)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def set_profile(slug: str, content: str) -> None:
    profile_md(resolve_owner(slug)).write_text(content, encoding="utf-8")


def get_character(slug: str) -> dict | None:
    try:
        return json.loads(character_json(slug).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --- Gear archive: one last-known set PER JOB ---------------------------------
# The Lodestone only ever shows the gear you logged out wearing. Log out as Botanist
# and your Ninja set is not stale — it's absent. Overwriting `gear` on every refresh
# therefore threw away the only Ninja data we'd ever had.
#
# So each refresh FILES that snapshot under the job it belongs to, and nothing is
# discarded. Play a few jobs over a few weeks and the archive fills itself in. Kept in
# its own file because apply_character rewrites character.json wholesale.
GEAR_ARCHIVE = "gear_archive.json"


def gear_archive_path(slug: str):
    return profile_dir(slug) / GEAR_ARCHIVE


def gear_archive(slug: str) -> dict:
    try:
        data = json.loads(gear_archive_path(slug).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# How much we trust each source of a gear set. A WEAKER source never overwrites a
# stronger one for the same job — without this the Lodestone's logout snapshot lands
# after the local import on every launch and silently replaces exact data with a
# guess about one job.
#
# Caveat worth knowing: a gearset file only changes when the player SAVES a gearset.
# If they re-gear without updating the set, the Lodestone's snapshot is the more
# current of the two even though it ranks lower. That's the trade for never letting a
# one-job snapshot clobber the file that covers every job.
_SOURCE_RANK = {"gearset_file": 3, "lodestone": 2, "screenshot": 1, "player": 1}


def record_gear(slug: str, job: str, pieces: list[dict], source: str,
                average: int = 0) -> dict:
    """File one job's gear set in the archive, replacing that job's previous entry.

    `source` is provenance and is shown to the assistant — "lodestone" (scraped from a
    logout snapshot), "screenshot" (read off an in-game screenshot), or "player" (they
    told us). It matters: a screenshot reading can be misread, so the assistant should
    treat it with less certainty than a scrape, and say where a number came from.
    """
    slug = resolve_owner(slug)
    job = (job or "").strip()
    if not job or not pieces:
        return {}
    arch = gear_archive(slug)
    prev = arch.get(job) or {}
    # Don't let a weaker source overwrite a stronger one for the same job.
    if prev and _SOURCE_RANK.get(source, 0) < _SOURCE_RANK.get(prev.get("source", ""), 0):
        return prev
    entry = {
        "job": job,
        "pieces": pieces,
        "average_item_level": average or _avg_ilvl(pieces),
        "source": source,
        "observed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # When the set last actually CHANGED, as best we can tell. The Lodestone
        # publishes no "updated at", so a re-scrape that returns identical gear tells
        # us nothing new — keeping the old changed-date avoids implying it did.
        "changed": prev.get("changed", "") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if prev.get("pieces") != pieces:
        entry["changed"] = entry["observed"]
    arch[job] = entry
    try:
        gear_archive_path(slug).write_text(
            json.dumps(arch, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return entry


def _avg_ilvl(pieces: list[dict]) -> int:
    lv = [p.get("item_level", 0) for p in pieces if p.get("item_level")]
    return round(sum(lv) / len(lv)) if lv else 0


def record_gear_from_char(slug: str, char) -> None:
    """File the scraped snapshot under whichever job it was worn on."""
    if not getattr(char, "equipment", None) or not char.active_job:
        return
    record_gear(
        slug, char.active_job,
        [{"slot": g.slot, "name": g.name, "item_level": g.item_level}
         for g in char.equipment],
        source="lodestone",
        average=char.average_item_level,
    )


def _merge_identity(existing_md: str, identity_md: str) -> str:
    """Replace the machine-owned Identity block, preserving the user's own
    sections (Goals/Playstyle/Preferences)."""
    if IDENTITY_HEADING in existing_md:
        pattern = re.compile(re.escape(IDENTITY_HEADING) + r".*?(?=\n## |\Z)", re.S)
        existing_md = pattern.sub("", existing_md)
    body = existing_md.strip()
    ident = identity_md.strip()
    return f"{ident}\n\n{body}\n" if body else f"{ident}\n"


def apply_character(slug: str, char, identity_md: str) -> dict:
    """Merge a scraped character's identity block into the workspace profile, save
    character.json, and record the character id in the index."""
    slug = resolve_owner(slug)
    set_profile(slug, _merge_identity(get_profile(slug), identity_md))
    character_json(slug).write_text(
        json.dumps(asdict(char), ensure_ascii=False, indent=2), encoding="utf-8")
    idx = _read_index()
    for w in idx:
        if w.get("slug") == slug:
            w["character_id"] = char.id
    _write_index(idx)
    return {"character_id": char.id, "name": char.name,
            "world": char.world, "data_center": char.data_center}
