"""In-app updates — check GitHub Releases and fetch the installer.

This feature downloads a file the shell then EXECUTES, so every input is
pinned rather than passed in: the repository is hardcoded here, only a release
asset served from that repo may be downloaded, and the file always lands in
our own directory under a name we choose. Nothing the model, the chat, or a
redirect suggests can steer it.

The app compares its own version (from the Tauri shell) against the newest
release tag; the frontend decides what to show.
"""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from config import USER_AGENT
from paths import DATA_DIR

REPO = "missayannie/Aether-Intelligence"
_API = f"https://api.github.com/repos/{REPO}/releases/latest"
# A release asset may be served from either host; anything else is refused.
_ALLOWED_HOSTS = {"github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com"}
_DL_DIR = DATA_DIR / "updates"


@dataclass
class Release:
    version: str          # normalised, e.g. "1.2.0"
    tag: str
    name: str
    notes: str
    url: str              # the installer asset
    size: int
    published_at: str


def _norm(v: str) -> str:
    """'v1.1' / 'V1.1.0' / '1.1.0' -> '1.1.0'."""
    v = (v or "").strip().lstrip("vV")
    parts = re.split(r"[.\-+]", v)
    nums = []
    for p in parts[:3]:
        if p.isdigit():
            nums.append(int(p))
        else:
            break
    while len(nums) < 3:
        nums.append(0)
    return ".".join(str(n) for n in nums)


def is_newer(latest: str, current: str) -> bool:
    def key(v: str) -> tuple:
        return tuple(int(x) for x in _norm(v).split("."))
    try:
        return key(latest) > key(current)
    except ValueError:
        return False


def latest_release(timeout: float = 20.0) -> Release | None:
    """The newest published (non-draft, non-prerelease) release, or None."""
    r = httpx.get(_API, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": USER_AGENT,
                           "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    d = r.json()
    if d.get("draft") or d.get("prerelease"):
        return None
    asset = next((a for a in d.get("assets") or []
                  if str(a.get("name", "")).lower().endswith("-setup.exe")), None)
    if not asset:
        return None
    url = asset.get("browser_download_url") or ""
    if urlparse(url).hostname not in _ALLOWED_HOSTS:
        return None
    return Release(
        version=_norm(d.get("tag_name") or ""),
        tag=d.get("tag_name") or "",
        name=d.get("name") or "",
        notes=(d.get("body") or "")[:4000],
        url=url,
        size=int(asset.get("size") or 0),
        published_at=d.get("published_at") or "",
    )


# ---- download (background, with progress the UI can poll) ------------------
_state: dict = {"status": "idle", "pct": 0, "path": "", "error": "", "version": ""}
_lock = threading.Lock()


def state() -> dict:
    with _lock:
        return dict(_state)


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def download_async(rel: Release) -> None:
    """Fetch the installer to DATA_DIR/updates. Refuses anything not served by
    the pinned repo's release hosts."""
    if urlparse(rel.url).hostname not in _ALLOWED_HOSTS:
        _set(status="error", error="Refused: download host is not GitHub.")
        return
    if state().get("status") == "downloading":
        return
    _set(status="downloading", pct=0, path="", error="", version=rel.version)

    def run() -> None:
        try:
            _DL_DIR.mkdir(parents=True, exist_ok=True)
            # OUR filename, not the server's — a remote name must never decide
            # where this lands.
            dest = _DL_DIR / f"AetherIntelligence-{rel.version}-setup.exe"
            tmp = dest.with_suffix(".part")
            with httpx.stream("GET", rel.url, follow_redirects=True, timeout=60.0,
                              headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or rel.size or 0)
                got = 0
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            _set(pct=min(99, int(got * 100 / total)))
            if rel.size and abs(tmp.stat().st_size - rel.size) > 1024:
                tmp.unlink(missing_ok=True)
                _set(status="error", error="Download size didn't match the release.")
                return
            tmp.replace(dest)
            _set(status="ready", pct=100, path=str(dest))
        except Exception as exc:  # network, disk, anything
            _set(status="error", error=str(exc)[:300])

    threading.Thread(target=run, daemon=True).start()


def as_dict(rel: Release | None) -> dict:
    return asdict(rel) if rel else {}
