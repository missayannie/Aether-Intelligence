"""Overlay watches — the passive chips' data (docs/overlay-spec.md §6.3).

A watch is something the overlay keeps in front of the player: a map pin
("pin"), a whole category of pins from one agent answer ("pinset" — renders as
ONE chip, never nine), and later timed node/fish windows. Stored in one JSON
file so the app and the overlay window share state: arm in either, see it in
both.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from paths import DATA_DIR

_FILE = DATA_DIR / "overlay_watches.json"
_LOCK = threading.Lock()


def _load() -> list[dict]:
    try:
        v = json.loads(_FILE.read_text(encoding="utf-8-sig"))
        return v if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _save(watches: list[dict]) -> None:
    _FILE.write_text(json.dumps(watches, ensure_ascii=False, indent=2), encoding="utf-8")


def list_watches() -> list[dict]:
    with _LOCK:
        return _load()


def add(watch: dict) -> dict:
    watch = dict(watch)
    watch["id"] = uuid.uuid4().hex[:8]
    watch["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _LOCK:
        ws = _load()
        ws.append(watch)
        _save(ws)
    return watch


def remove(watch_id: str) -> bool:
    with _LOCK:
        ws = _load()
        kept = [w for w in ws if w.get("id") != watch_id]
        if len(kept) == len(ws):
            return False
        _save(kept)
    return True
