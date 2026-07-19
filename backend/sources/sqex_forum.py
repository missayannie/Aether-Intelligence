"""Square Enix official FFXIV forum — community discussion (forum 667).

Note: forum 667 is *General Discussion* (community threads), not official news —
the Lodestone covers official announcements. So this is surfaced as community
sentiment/topics ("what are players discussing"), kept separate from news.

No API; scraped with curl_cffi (the forum is bot-sensitive), live with a light
cache fallback.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict

from curl_cffi import requests as cffi

from config import USER_AGENT
from paths import KNOWLEDGE_DIR

BASE = "https://forum.square-enix.com/ffxiv/"
FORUM_URL = BASE + "forums/667"
SEARCH_URL = BASE + "search.php"
CACHE = KNOWLEDGE_DIR / "news" / "sqex_forum.json"

# Pinned/boilerplate threads to drop.
_SKIP = ("welcome to", "official rules", "forum rules", "read before")


@dataclass
class ForumThread:
    title: str
    url: str


class SqexForumClient:
    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._s.close()

    def threads(self, limit: int = 12) -> list[ForumThread]:
        try:
            items = self._scrape(limit)
            if items:
                self._write_cache(items)
                return items
        except Exception:
            pass
        return self._read_cache(limit)

    def search(self, query: str, limit: int = 8) -> list[ForumThread]:
        """Search the official forum for a topic (title match). Returns matching
        threads — the agent can then read one with posts() to pull player comments."""
        from bs4 import BeautifulSoup

        try:
            r = self._s.get(
                SEARCH_URL,
                params={"do": "process", "query": query, "showposts": 0},
                timeout=self._timeout, allow_redirects=True,
            )
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            return []
        out: list[ForumThread] = []
        seen: set[str] = set()
        for a in soup.select("a.title"):
            title = a.get_text(strip=True)
            href = a.get("href", "")
            # Thread links are relative on the results page (e.g. "threads/389560-…").
            if not title or not re.search(r"threads/\d+", href) or title.lower().startswith(_SKIP):
                continue
            url = href if href.startswith("http") else BASE + href.lstrip("/")
            if url in seen:
                continue
            seen.add(url)
            out.append(ForumThread(title=title, url=url))
            if len(out) >= limit:
                break
        return out

    def posts(self, thread_url: str, limit: int = 8) -> list[str]:
        """Read a thread's posts (the player comments/discussion). Quote blocks are
        stripped to cut noise; each post is capped so a long thread stays readable."""
        from bs4 import BeautifulSoup

        try:
            html = self._s.get(thread_url, timeout=self._timeout).text
        except Exception:
            return []
        soup = BeautifulSoup(html, "html.parser")
        # Scope to the actual post list (#posts) so the quick-reply/new-post editor —
        # also a .postcontent — doesn't leak in as a bogus "post".
        els = soup.select("#posts .postcontent") or soup.select(".postcontent")
        out: list[str] = []
        for el in els:
            for q in el.select(".bbcode_container, blockquote, .quote"):
                q.decompose()  # drop quoted text so we keep the poster's own words
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            if len(text) >= 15:            # skip empty/template fragments
                out.append(text[:1200])
            if len(out) >= limit:
                break
        return out

    def _scrape(self, limit: int) -> list[ForumThread]:
        from bs4 import BeautifulSoup

        html = self._s.get(FORUM_URL, timeout=self._timeout).text
        soup = BeautifulSoup(html, "html.parser")
        out: list[ForumThread] = []
        seen = set()
        for a in soup.select("a.title"):
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or not href or title.lower().startswith(_SKIP):
                continue
            url = href if href.startswith("http") else BASE + href.lstrip("/")
            if url in seen:
                continue
            seen.add(url)
            out.append(ForumThread(title=title, url=url))
            if len(out) >= limit:
                break
        return out

    def _write_cache(self, items: list[ForumThread]) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_cache(self, limit: int) -> list[ForumThread]:
        try:
            return [ForumThread(**d) for d in json.loads(CACHE.read_text(encoding="utf-8"))][:limit]
        except (FileNotFoundError, json.JSONDecodeError):
            return []
