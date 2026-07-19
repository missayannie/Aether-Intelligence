"""Eorzea Database — the OFFICIAL FFXIV item database on the Lodestone.

The authoritative source for item facts (item level, category, stats, the exact
in-game description, the icon). No API, so we scrape two pages, live-per-question
like the wikis, using curl_cffi (Chrome impersonation) since the Lodestone is
bot-sensitive:

    search: /lodestone/playguide/db/item/?q=<name>  -> result rows -> detail urls
    detail: /lodestone/playguide/db/item/<hash>/     -> the item's fields

Verified against the live site (e.g. item 16ab5e814a6 -> "Grade 3 Shroud Topsoil").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from curl_cffi import requests as cffi

from config import USER_AGENT
from sources import cache
from sources.lodestone_http import LodestoneBlocked, waf_get

BASE = "https://na.finalfantasyxiv.com"
ITEM_DB = BASE + "/lodestone/playguide/db/item/"
SOURCE = "Eorzea Database (official)"

# Database sections that actually exist, verified live against the Lodestone.
# NOTE there is deliberately no "npc": /playguide/db/npc/ is a 404 — the official
# database has no NPC pages, so an NPC can only be linked to a wiki. Don't add one
# here hoping it works; it doesn't.
DB_KINDS = {
    "item": "item",           # gear, materia, consumables, mats
    "duty": "duty",           # dungeons, trials, raids
    "quest": "quest",
    "recipe": "recipe",
    "achievement": "achievement",
    "shop": "shop",
}


@dataclass
class ItemHit:
    name: str
    url: str


@dataclass
class ItemComment:
    """A player-posted comment on the item's database page. Often the most useful
    thing on the page — corrected gathering coords, spawn timings, tips."""
    text: str
    author: str = ""    # "Shay M'iles Behemoth [Primal]"
    date: str = ""      # YYYY-MM-DD (from the comment's data-epoch)


@dataclass
class ItemResult:
    source: str
    name: str
    url: str
    category: str = ""      # "Gardening", "Head", "Arcanist's Arm"…
    item_level: str = ""    # "" when the item has no item level (e.g. materials)
    description: str = ""    # the in-game flavour/effect text
    details: str = ""        # compact stat/bonus text for the model to read
    icon: str = ""           # absolute icon url
    comments: list[ItemComment] = field(default_factory=list)


class EorzeaDBClient:
    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._s.close()

    def search(self, query: str, limit: int = 8) -> list[ItemHit]:
        """Search the item database. Returns [ItemHit(name, url)]."""
        from bs4 import BeautifulSoup

        # waf_get retries the Lodestone's bot-challenge; without it a challenge page
        # parses to zero rows and looks like "no such item". Cached a day — item search
        # results only shift when the game patches.
        html = cache.fetch_text(
            "item", f"search:{query.lower()}", cache.TTL_ITEM_PAGE,
            lambda: waf_get(self._s, ITEM_DB, params={"q": query}, timeout=self._timeout),
        )
        soup = BeautifulSoup(html, "html.parser")
        hits: list[ItemHit] = []
        seen: set[str] = set()
        # Result rows link to a hashed detail path via a.db-table__txt--detail_link.
        for a in soup.select("a.db-table__txt--detail_link, a.db_popup"):
            href = a.get("href", "")
            if not re.search(r"/db/item/[0-9a-f]{6,}/?$", href):
                continue
            name = a.get_text(" ", strip=True)
            url = href if href.startswith("http") else BASE + href
            if name and url not in seen:
                seen.add(url)
                hits.append(ItemHit(name=name, url=url))
            if len(hits) >= limit:
                break
        return hits

    def search_kind(self, kind: str, query: str, limit: int = 20) -> list[ItemHit]:
        """Search any database section (see DB_KINDS). Returns [ItemHit(name, url)]."""
        from bs4 import BeautifulSoup

        section = DB_KINDS.get(kind)
        if not section or not query.strip():
            return []
        url_base = f"{BASE}/lodestone/playguide/db/{section}/"
        try:
            html = cache.fetch_text(
                "item", f"{section}:search:{query.lower()}", cache.TTL_ITEM_PAGE,
                lambda: waf_get(self._s, url_base, params={"q": query},
                                timeout=self._timeout),
            )
        except LodestoneBlocked:
            raise
        except Exception:
            return []

        soup = BeautifulSoup(html, "html.parser")
        hits: list[ItemHit] = []
        seen: set[str] = set()
        pat = re.compile(rf"/db/{section}/[0-9a-f]{{6,}}/?$")
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not pat.search(href):
                continue
            name = a.get_text(" ", strip=True)
            full = href if href.startswith("http") else BASE + href
            if name and full not in seen:
                seen.add(full)
                hits.append(ItemHit(name=name, url=full))
            if len(hits) >= limit:
                break
        return hits

    def find(self, kind: str, query: str) -> ItemHit | None:
        """Best official-database page for `query` in section `kind` (see DB_KINDS).

        This is what lets a guide table LINK its entries — "Ihuykatumu" ->
        /db/duty/259c37be2ea/ — instead of naming things the player then has to go
        search for. Prefers an exact (case-insensitive) name match and only falls back
        to the first row, because a fuzzy search for "Vanguard" happily returns a
        dozen unrelated items and linking the wrong page is worse than not linking.
        """
        hits = self.search_kind(kind, query)
        if not hits:
            return None
        want = query.strip().lower()
        for h in hits:
            if h.name.strip().lower() == want:
                return h
        return hits[0]

    def item(self, url: str) -> ItemResult | None:
        """Fetch a single item's detail page and parse its fields."""
        from bs4 import BeautifulSoup

        if not url.startswith("http"):
            url = BASE + ("" if url.startswith("/") else "/") + url
        # A day: the item's own data is static between patches, and its player
        # comments move slowly enough that a day-old copy is still useful.
        html = cache.fetch_text("item", url, cache.TTL_ITEM_PAGE,
                                lambda: waf_get(self._s, url, timeout=self._timeout))
        soup = BeautifulSoup(html, "html.parser")

        def txt(sel: str) -> str:
            el = soup.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""

        name = txt(".db-view__item__text__name")
        if not name:
            return None
        icon_el = soup.select_one(".db-view__item__icon img")
        icon = (icon_el.get("src") or icon_el.get("data-src") or "") if icon_el else ""

        # Item level appears as "Item Level NNN" somewhere in the detail region.
        il = ""
        ilm = re.search(r"Item Level\s*([\d,]+)", soup.get_text(" ", strip=True))
        if ilm:
            il = ilm.group(1)

        # A compact dump of the item detail block for stats/bonuses the model can read.
        block = soup.select_one(".db-view__item_equipment, .db-view__item__text, .db-view__wrapper")
        details = re.sub(r"\s+", " ", block.get_text(" ", strip=True)).strip()[:800] if block else ""

        return ItemResult(
            source=SOURCE, name=name, url=url,
            category=txt(".db-view__item__text__category"),
            item_level=il,
            description=txt(".db-view__item__text__description"),
            details=details,
            icon=icon,
            comments=_parse_comments(soup),
        )

    def comments(self, url: str) -> list[ItemComment]:
        """Just the player comments for an item page (see _parse_comments)."""
        from bs4 import BeautifulSoup

        if not url.startswith("http"):
            url = BASE + ("" if url.startswith("/") else "/") + url
        try:
            html = cache.fetch_text("item", url, cache.TTL_ITEM_PAGE,
                                    lambda: waf_get(self._s, url, timeout=self._timeout))
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
        return _parse_comments(soup)

    def lookup(self, query: str) -> ItemResult | None:
        """One-shot: search then fetch the best match (exact name preferred)."""
        hits = self.search(query, limit=8)
        if not hits:
            return None
        best = next((h for h in hits if h.name.lower() == query.lower()), hits[0])
        return self.item(best.url)


def _parse_comments(soup) -> list[ItemComment]:
    """Player comments from an item page.

    Structure (verified live on e.g. "Cordia Sap", which has 2):
        .comment_list > .comment
            .balloon_body_inner            -> the comment text
            .player_id                     -> "<Name> <World [DC]>"
            .datetime_dynamic_ymdhm        -> date, JS-rendered from data-epoch
    The visible date text is "-" until the page's JS runs, so read the epoch attr.
    Items with no comments simply yield [].
    """
    out: list[ItemComment] = []
    for el in soup.select(".comment_list .comment"):
        body = el.select_one(".balloon_body_inner")
        text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip() if body else ""
        if not text:
            continue
        who = el.select_one(".player_id")
        dt = el.select_one(".datetime_dynamic_ymdhm")
        date = ""
        epoch = dt.get("data-epoch") if dt else None
        if epoch:
            try:
                date = datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                pass
        out.append(ItemComment(
            text=text[:600],
            author=who.get_text(" ", strip=True) if who else "",
            date=date,
        ))
    return out


if __name__ == "__main__":
    c = EorzeaDBClient()
    for q in ("Grade 3 Shroud Topsoil", "Ronkan Ring of Casting"):
        r = c.lookup(q)
        print(f"[{'OK' if r else 'MISS'}] {q}: "
              + (f"{r.name} | cat={r.category} | ilvl={r.item_level or '-'} | {r.url}" if r else "no result"))
    c.close()
