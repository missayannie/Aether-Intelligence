"""Garland Tools' community-curated data, vendored under its MIT license.

The game client answers almost everything, but a few facts were never IN the
client: which patch a record shipped in, and duty loot tables. Garland's own
build pipeline keeps those as hand-maintained files in their open-source repo
(github.com/ufx/GarlandTools, MIT) — so instead of asking their API per
record, this module downloads the files ONCE (pinned to a known commit),
stores them beside the app's data with attribution, and serves lookups
locally. Refreshing the pin is a one-line bump of _COMMIT.

Nothing here blocks anything: files absent (offline first run) simply mean
patch tags read as "" until the background download lands.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from paths import DATA_DIR

# Pinned for reproducibility — bump deliberately, not implicitly.
# KNOWN GAP at this commit: patches.json covers through Endwalker (item ids to
# ~41709, no 7.x tags) — the repo lags the live site. Newer records read as
# untagged (""), which the UI already renders as "no patch shown". Bump the
# pin when upstream refreshes.
_COMMIT = "04cadd2e1e0de86c20aa9303faa082c7971f8d8b"
_RAW = f"https://raw.githubusercontent.com/ufx/GarlandTools/{_COMMIT}/Supplemental"
_FILES = {
    "patches.json": "patches.json",
    "duties.json": "FFXIV Data - Duties.json",
}
_DIR = DATA_DIR / "supplemental"

_ATTRIBUTION = """This directory contains data files from the Garland Tools
project (https://github.com/ufx/GarlandTools), used under the MIT license.
Copyright (c) the Garland Tools contributors. Vendored at commit {commit}.
"""

_PATCH_INDEX: dict | None = None
_DUTIES: list | None = None
_LOCK = threading.Lock()


def ensure_downloaded(background: bool = True) -> None:
    """Fetch the pinned files if this commit's copies aren't on disk yet."""
    marker = _DIR / f".commit-{_COMMIT}"
    if marker.exists():
        return
    if background:
        threading.Thread(target=lambda: ensure_downloaded(False),
                         daemon=True, name="supplemental-dl").start()
        return
    from sources.garland import _session
    _DIR.mkdir(parents=True, exist_ok=True)
    s = _session()
    try:
        for local, remote in _FILES.items():
            dest = _DIR / local
            try:
                r = s.get(f"{_RAW}/{remote.replace(' ', '%20')}", timeout=120)
                if r.status_code == 200 and r.content:
                    dest.write_bytes(r.content)
            except Exception:
                return          # partial is fine; retried next launch (no marker)
        (_DIR / "LICENSE-ATTRIBUTION.txt").write_text(
            _ATTRIBUTION.format(commit=_COMMIT), encoding="utf-8")
        if all((_DIR / f).exists() for f in _FILES):
            marker.write_text("", encoding="utf-8")
            global _PATCH_INDEX, _DUTIES
            _PATCH_INDEX = None       # reload from the fresh files
            _DUTIES = None
    finally:
        s.close()


def _load_json(name: str):
    try:
        return json.loads((_DIR / name).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _patches() -> dict:
    global _PATCH_INDEX
    if _PATCH_INDEX is None:
        with _LOCK:
            if _PATCH_INDEX is None:
                rows = _load_json("patches.json") or []
                _PATCH_INDEX = {(r.get("type"), str(r.get("id"))): r.get("patch")
                                for r in rows if r.get("type")}
    return _PATCH_INDEX


def patch_tag(kind: str, ident) -> str:
    """The patch a record shipped in ("6.4"), or "" when unknown. Formats the
    way Garland's site does: whole versions keep one decimal ("2.0")."""
    v = _patches().get((kind, str(ident)))
    if v is None:
        return ""
    s = f"{float(v):g}"
    return s if "." in s else s + ".0"


def duties() -> list[dict]:
    """The curated duty list (categories, Lodestone links, level bands) —
    stored for the GarlandDB tab's future local detail pages."""
    global _DUTIES
    if _DUTIES is None:
        with _LOCK:
            if _DUTIES is None:
                _DUTIES = _load_json("duties.json") or []
    return _DUTIES


def duty_info(name: str) -> dict | None:
    want = (name or "").strip().lower()
    for d in duties():
        if (d.get("name") or "").strip().lower() == want:
            return d
    return None


def status() -> dict:
    return {"dir": str(_DIR), "commit": _COMMIT,
            "downloaded": (_DIR / f".commit-{_COMMIT}").exists(),
            "patch_entries": len(_patches()) if (_DIR / "patches.json").exists() else 0}
