"""Official patch notes — what the CURRENT patch actually changed.

This exists to make cross-checking cheap. Verifying every version-specific claim
against a second source is slow; not verifying is wrong. Patch notes settle it: if
this patch didn't touch Ninja, a cached Ninja rotation is as good today as it was
last week and one source is enough. If it did, go look again.

Source is the official archive (verified live):

    /lodestone/special/patchnote_log/   ->  "Patch 7.51 Notes" -> /lodestone/topics/detail/<hash>/

WHY EXCERPTS, NOT THE PAGE: one patch's notes run ~39,000 characters (~10k tokens).
Returning them whole would cost more context than the whole system prompt and defeat
the point. So callers pass a topic and get back the matching lines with a little
surrounding context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from curl_cffi import requests as cffi

from config import USER_AGENT
from sources import cache
from sources.lodestone_http import waf_get

BASE = "https://na.finalfantasyxiv.com"
ARCHIVE_URL = BASE + "/lodestone/special/patchnote_log/"

_DAY = 24 * 60 * 60
# A published patch note never changes — only which one is current does. The archive
# listing is what goes stale, and it lives in the patch-purged namespace, so patch
# day re-reads it (see gameversion.PATCH_SENSITIVE).
TTL_NOTES = 30 * _DAY
TTL_ARCHIVE = 1 * _DAY


@dataclass
class Excerpt:
    heading: str
    text: str


def _session() -> cffi.Session:
    return cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})


def notes_url(patch: str) -> tuple[str, str]:
    """(label, url) of the notes for `patch`, e.g. "7.51" -> ("Patch 7.51 Notes", …).

    Falls back to the first listed patch when there's no exact match, so a version
    string we can't map (or an archive layout change) still returns something usable
    rather than nothing.
    """
    from bs4 import BeautifulSoup

    s = _session()
    try:
        html = cache.fetch_text("patch", ARCHIVE_URL, TTL_ARCHIVE,
                                lambda: waf_get(s, ARCHIVE_URL, timeout=25.0))
    finally:
        s.close()

    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True)
        if re.match(r"^Patch\s+[\d.]+\s+Notes$", label, re.I):
            href = a.get("href") or ""
            links.append((label, href if href.startswith("http") else BASE + href))
    if not links:
        return "", ""
    if patch:
        # "7.51x2" (XIVAPI's data version) -> "7.51" (the marketing patch number).
        base = re.match(r"(\d+\.\d+)", patch)
        if base:
            want = f"patch {base.group(1)} notes"
            for label, url in links:
                if label.lower() == want:
                    return label, url
    return links[0]


def _fetch_text(url: str) -> str:
    from bs4 import BeautifulSoup

    s = _session()
    try:
        html = cache.fetch_text("patch", url, TTL_NOTES,
                                lambda: waf_get(s, url, timeout=25.0))
    finally:
        s.close()
    soup = BeautifulSoup(html, "html.parser")
    body = (soup.select_one(".news__detail__wrapper")
            or soup.select_one(".news__detail") or soup)
    return body.get_text("\n", strip=True)


def summary(patch: str) -> dict:
    """Headline + section outline for a patch — the cheap "what's in this patch"."""
    label, url = notes_url(patch)
    if not url:
        return {"found": False}
    text = _fetch_text(url)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return {
        "found": True, "patch": label, "url": url,
        "intro": " ".join(lines[:3])[:600],
        "sections": _sections(lines)[:40],
    }


def _sections(lines: list[str]) -> list[str]:
    """Short standalone lines read as section headings in the notes' flat text."""
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if 2 < len(s) <= 40 and not s.endswith((".", ":", ",")) and s not in out:
            out.append(s)
    return out


def search(patch: str, topic: str, context: int = 2, limit: int = 12) -> dict:
    """Lines mentioning `topic` in this patch's notes, with surrounding context.

    An empty result is a REAL answer — "this patch didn't touch it" is exactly the
    signal that makes a re-check unnecessary — so callers must not treat it as a
    failed lookup.
    """
    label, url = notes_url(patch)
    if not url:
        return {"found": False, "note": "Could not read the official patch note archive."}

    text = _fetch_text(url)
    lines = text.split("\n")
    pat = re.compile(re.escape(topic.strip()), re.I)
    hits: list[Excerpt] = []
    used: set[int] = set()
    for i, ln in enumerate(lines):
        if not pat.search(ln):
            continue
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        if any(j in used for j in range(lo, hi)):
            continue                     # overlaps an excerpt we already took
        used.update(range(lo, hi))
        chunk = " ".join(x.strip() for x in lines[lo:hi] if x.strip())
        hits.append(Excerpt(heading=lines[i].strip()[:80], text=chunk[:500]))
        if len(hits) >= limit:
            break

    return {
        "found": True, "patch": label, "url": url, "topic": topic,
        "mentioned": bool(hits),
        "excerpts": [{"heading": h.heading, "text": h.text} for h in hits],
    }
