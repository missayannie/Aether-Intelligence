"""TTL disk cache for source fetches.

Cache by VOLATILITY, never blanket-cache: market prices are the whole point of
being live, while an A Realm Remapped map texture (6.5 MB!) only changes when the
game patches and was being re-downloaded on every single pin. The TTLs below encode
that judgement in one place.

Note this is about waste and speed, NOT about dodging the Lodestone's bot-challenge
— that WAF challenges in bursts regardless of volume (see lodestone_http), so it
needs a retry, not a cache.

Lives in DATA_DIR so it survives reinstalls, and is capped per namespace so the map
textures can't grow unbounded. Entries are plain files; a corrupt or missing one
just reads as a miss, so the cache can never break a fetch — worst case it re-fetches.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from paths import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"

_MIN = 60
_HOUR = 60 * _MIN
_DAY = 24 * _HOUR

# --- TTLs, chosen per how fast each source actually changes ---
TTL_MAP_ASSET = 30 * _DAY   # ARR base textures + marker geojson: change on patches
TTL_ITEM_PAGE = 1 * _DAY    # Eorzea DB item pages: stats are static; comments drift
TTL_WIKI = 1 * _HOUR        # wiki lookups: slow-moving, helps repeat questions
TTL_NEWS = 15 * _MIN        # Lodestone news: needs to feel current
TTL_ITEM_ID = 30 * _DAY     # XIVAPI name -> item id: effectively immutable

# Per-namespace disk budget. Map textures are megabytes each, so cap them; the rest
# are small. Oldest entries are evicted first once a namespace exceeds its budget.
_BUDGET = {"map": 400 * 1024 * 1024}
_DEFAULT_BUDGET = 40 * 1024 * 1024


def _path(ns: str, key: str) -> Path:
    # Hash the key: it's usually a URL, which isn't a legal filename.
    return CACHE_DIR / ns / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}.bin"


def get(ns: str, key: str, ttl: float) -> bytes | None:
    """Cached bytes for `key`, or None if absent/expired/unreadable."""
    p = _path(ns, key)
    try:
        if time.time() - p.stat().st_mtime > ttl:
            return None
        return p.read_bytes()
    except OSError:
        return None


def put(ns: str, key: str, data: bytes) -> None:
    """Store bytes. Written to a temp file then renamed, so a crash mid-write can't
    leave a truncated entry that later reads as valid."""
    p = _path(ns, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        _prune(ns)
    except OSError:
        pass  # a cache that can't write must not break the caller


def fetch(ns: str, key: str, ttl: float, loader: Callable[[], bytes]) -> bytes:
    """Return cached bytes for `key`, else call `loader()` and cache what it returns.
    Empty/false results are never cached, so a failed fetch isn't remembered."""
    hit = get(ns, key, ttl)
    if hit is not None:
        return hit
    data = loader()
    if data:
        put(ns, key, data)
    return data


def fetch_text(ns: str, key: str, ttl: float, loader: Callable[[], str]) -> str:
    """fetch() for text sources (HTML/JSON)."""
    raw = fetch(ns, key, ttl, lambda: loader().encode("utf-8"))
    return raw.decode("utf-8", "replace")


def _prune(ns: str) -> None:
    """Keep a namespace under budget by dropping the oldest entries."""
    budget = _BUDGET.get(ns, _DEFAULT_BUDGET)
    d = CACHE_DIR / ns
    try:
        files = [(f, f.stat()) for f in d.glob("*.bin")]
    except OSError:
        return
    total = sum(s.st_size for _, s in files)
    if total <= budget:
        return
    for f, s in sorted(files, key=lambda x: x[1].st_mtime):  # oldest first
        try:
            f.unlink()
            total -= s.st_size
        except OSError:
            pass
        if total <= budget:
            break


def clear(ns: str | None = None) -> int:
    """Drop cached entries (a namespace, or everything). Returns files removed."""
    root = CACHE_DIR / ns if ns else CACHE_DIR
    n = 0
    for f in root.rglob("*.bin"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
