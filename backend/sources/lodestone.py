"""The Lodestone — official FFXIV news (patches, maintenance, events, status).

The Lodestone has no API, so this scrapes the news list. Per the source design,
it's live-first with a thin cache fallback: every successful fetch writes a local
copy, and if a later fetch fails (site down, layout change, rate-limit) we serve
the cached copy rather than nothing. A scheduled daily pull can call refresh() to
keep the cache warm.

Uses curl_cffi (browser impersonation) since the Lodestone is bot-sensitive.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict

from curl_cffi import requests as cffi

from config import USER_AGENT
from paths import KNOWLEDGE_DIR
from sources import cache
from sources.lodestone_http import LodestoneBlocked, waf_get

BASE = "https://na.finalfantasyxiv.com"
NEWS_URL = BASE + "/lodestone/news/"
CACHE = KNOWLEDGE_DIR / "news" / "lodestone.json"

# Category landing pages (the main /news/ page mixes recent items).
CATEGORIES = {
    "topics": "/lodestone/news/category/0",
    "notices": "/lodestone/news/category/1",
    "maintenance": "/lodestone/news/category/2",
    "updates": "/lodestone/news/category/3",
    "status": "/lodestone/news/category/4",
}


@dataclass
class NewsItem:
    category: str
    title: str
    url: str
    date: str = ""


class LodestoneClient:
    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._s.close()

    def news(self, limit: int = 15) -> list[NewsItem]:
        """Recent Lodestone news, live with cache fallback."""
        try:
            items = self._scrape(NEWS_URL, limit)
            if items:
                self._write_cache(items)
                return items
        except Exception:
            pass
        return self._read_cache(limit)

    def category(self, name: str, limit: int = 15) -> list[NewsItem]:
        path = CATEGORIES.get(name.lower())
        if not path:
            return []
        try:
            return self._scrape(BASE + path, limit)
        except Exception:
            return []

    def refresh(self) -> int:
        """Warm the cache (for a scheduled pull). Returns item count."""
        items = self._scrape(NEWS_URL, 30)
        self._write_cache(items)
        return len(items)

    # --- internals ---
    def _scrape(self, url: str, limit: int) -> list[NewsItem]:
        from bs4 import BeautifulSoup

        # Retry through the WAF bot-challenge. Without this a challenge page parses
        # to zero items and looks like "no news" — the caller then serves the cache
        # (or nothing) with no clue why.
        # Only a 15-minute cache: news must still feel current.
        html = cache.fetch_text("news", url, cache.TTL_NEWS,
                                lambda: waf_get(self._s, url, timeout=self._timeout))
        soup = BeautifulSoup(html, "html.parser")
        out: list[NewsItem] = []
        for li in soup.select("li.news__list")[:limit]:
            a = li.find("a", href=True)
            if not a:
                continue
            raw = a.get_text(" ", strip=True).rstrip(" -").strip()
            m = re.match(r"^\[(.+?)\]\s*(.*)$", raw)
            cat, title = (m.group(1), m.group(2)) if m else ("News", raw)
            time_el = li.find("time")
            out.append(NewsItem(
                category=cat, title=title.strip(),
                url=BASE + a["href"] if a["href"].startswith("/") else a["href"],
                date=time_el.get_text(strip=True) if time_el else "",
            ))
        return out

    def _write_cache(self, items: list[NewsItem]) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_cache(self, limit: int) -> list[NewsItem]:
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            return [NewsItem(**d) for d in data][:limit]
        except (FileNotFoundError, json.JSONDecodeError):
            return []
