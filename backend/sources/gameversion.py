"""Current FFXIV game version — the honest expiry signal for cached game data.

A TTL can only guess. A wiki page cached an hour before a patch lands is wrong the
moment it drops; a map texture cached a month into a quiet period is still perfect.
Time was never what made game data stale — PATCHES are.

XIVAPI publishes the game's data versions, newest last, with the current one tagged
"latest":

    {"key": "f8764efd76cdb31a", "names": ["latest", "7.51x2"]}

`key` is a content hash of the game data, so it moves exactly when the data does —
including hotfixes that don't bump the marketing version. We stamp the cache with it
and purge patch-sensitive namespaces when it changes, which lets the TTLs stay long
(cheap, fast) without ever serving pre-patch facts.

`names` also gives the human patch number ("7.51x2"), which goes into the system
prompt so the assistant can say what it's current to, and can tell a stale source
from a fresh one.
"""
from __future__ import annotations

import json

from curl_cffi import requests as cffi

from config import USER_AGENT
from paths import DATA_DIR
from sources import cache

VERSION_URL = "https://v2.xivapi.com/api/version"
STAMP_PATH = DATA_DIR / "game_version.json"

# Namespaces holding data a patch can invalidate. `news` is deliberately absent: it
# is time-sensitive, not patch-sensitive, and already carries a 15-minute TTL.
# `patch` holds the notes archive listing, which must be re-read to discover the new
# patch's notes; the notes pages themselves are immutable and keyed by their own url,
# so re-caching them costs one fetch.
PATCH_SENSITIVE = ("item", "map", "itemid", "wiki", "patch", "garland")

# The version list itself barely moves; an hour keeps patch-day detection prompt
# without asking XIVAPI on every fetch.
TTL_VERSION = 60 * 60


def current() -> tuple[str, str]:
    """(content_key, patch_name) for the game data this machine should serve.

    The INSTALLED CLIENT is the primary signal when present: ffxivgame.ver flips
    the moment the launcher finishes patching — hours before third-party sites
    re-parse — and it works offline. XIVAPI supplies the human patch name (the
    .ver string has no marketing number) and remains the whole signal on
    machines without the game installed.
    """
    try:
        from sources import gameclient
        local = gameclient.version()
    except Exception:
        local = ""
    if local:
        _, name = _xivapi_current()      # best-effort label; "" offline is fine
        return "local:" + local, name
    return _xivapi_current()


def _xivapi_current() -> tuple[str, str]:
    """XIVAPI's (content_key, patch_name), or ("", "") when unreachable —
    callers must treat that as "don't know", never as "patched". Guessing wrong
    here would either purge the whole cache on every network blip or silently
    serve pre-patch data forever.
    """
    def _load() -> str:
        s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})
        try:
            return s.get(VERSION_URL, timeout=15).text
        finally:
            s.close()

    try:
        raw = cache.fetch_text("version", VERSION_URL, TTL_VERSION, _load)
        versions = json.loads(raw).get("versions") or []
    except Exception:
        return "", ""
    for v in reversed(versions):                  # newest last
        names = v.get("names") or []
        if "latest" in names:
            name = next((n for n in names if n != "latest"), "")
            return v.get("key", ""), name
    # No "latest" tag (XIVAPI changed shape) — fall back to the last entry.
    if versions:
        last = versions[-1]
        return last.get("key", ""), (last.get("names") or [""])[0]
    return "", ""


def _read_stamp() -> dict:
    try:
        return json.loads(STAMP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_stamp(key: str, name: str) -> None:
    try:
        STAMP_PATH.write_text(
            json.dumps({"key": key, "name": name}, indent=2), encoding="utf-8")
    except OSError:
        pass          # a stamp we can't write just means we re-check next launch


def patch_name() -> str:
    """Human patch number for the system prompt, e.g. "7.51x2" ("" if unknown).

    Reads the stamp first so a chat turn never blocks on XIVAPI.
    """
    return _read_stamp().get("name") or current()[1]


def purge_if_patched() -> dict:
    """Drop patch-sensitive cache entries if the game data changed since last run.

    Returns {"patched": bool, "from": str, "to": str, "removed": int}.
    """
    key, name = current()
    if not key:
        return {"patched": False, "from": "", "to": "", "removed": 0}   # unknown: keep cache

    old = _read_stamp()
    if old.get("key") == key:
        return {"patched": False, "from": name, "to": name, "removed": 0}

    removed = 0
    if old.get("key"):        # first run has no stamp — stamp it, don't purge a cold cache
        for ns in PATCH_SENSITIVE:
            removed += cache.clear(ns)
    _write_stamp(key, name)
    # A patch also invalidates the gameclient's derived index — its own version
    # stamp catches this too, but kicking the rebuild HERE means fresh answers
    # minutes after the launcher finishes, not on the next restart.
    try:
        from sources import gameclient
        gameclient.ensure_index(background=True)
    except Exception:
        pass
    return {"patched": bool(old.get("key")), "from": old.get("name", ""),
            "to": name, "removed": removed}
