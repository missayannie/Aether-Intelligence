"""One MediaWiki client for all three FFXIV wikis.

Gamer Escape, Console Games Wiki, and FF Fandom all expose the same MediaWiki
`api.php`, so a single client handles every one of them — you just point it at a
different base URL (see config.WIKIS). This is the consolidation win that made
adding three wikis cheap.

Live-per-question by design: these are clean APIs, so we query them in real time
rather than caching. Results carry a `source` label so the assistant can cite
where an answer came from.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

# curl_cffi impersonates a real browser's TLS fingerprint. Some FFXIV wikis
# (Gamer Escape) sit behind Cloudflare, which fingerprints the TLS handshake and
# 403s ordinary HTTP clients regardless of headers. curl_cffi gets through where
# httpx/requests can't. Used at low volume, live-per-question.
from curl_cffi import requests as cffi

from config import WIKIS, USER_AGENT
from sources import cache


@dataclass
class WikiResult:
    source: str          # human label, e.g. "Console Games Wiki"
    wiki_id: str         # key into WIKIS, e.g. "consolegames"
    title: str
    extract: str         # plain-text summary of the page
    url: str
    details: str = ""    # infobox text (location, coords, level…) for the model to read
    image_url: str = ""  # absolute url of the page's main image (NPC portrait, item render)
    tables: str = ""     # the page's content tables as text — where wikis keep DATA


class WikiClient:
    """Search + fetch against any configured MediaWiki instance."""

    def __init__(self, timeout: float = 15.0):
        self._timeout = timeout
        self._client = cffi.Session(
            impersonate="chrome",
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def fetch_image(self, url: str) -> bytes | None:
        """Download an image (e.g. a wiki portrait) with the impersonating session.

        The FFXIV wikis sit behind Cloudflare, so image requests need the same
        browser-TLS impersonation as page requests — reusing this client's session
        is why a plain http fetch of the url would 403.
        """
        try:
            r = self._get(url, {})
            if "image" in r.headers.get("content-type", "").lower():
                return r.content
        except Exception:
            pass
        return None

    def _get(self, url: str, params: dict):
        r = self._client.get(url, params=params, timeout=self._timeout, allow_redirects=True)
        r.raise_for_status()
        return r

    def _get_json(self, url: str, params: dict) -> dict:
        """A wiki API call, cached for an hour. Wiki pages move slowly, and one
        question often re-reads the same page (search -> get_page), so this cuts
        repeat traffic without ever showing meaningfully stale content."""
        import json as _json

        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        try:
            return _json.loads(cache.fetch_text(
                "wiki", key, cache.TTL_WIKI, lambda: self._get(url, params).text))
        except (ValueError, TypeError):
            return {}

    def _api(self, wiki_id: str) -> str:
        try:
            return WIKIS[wiki_id]["api"]
        except KeyError as exc:
            raise ValueError(f"Unknown wiki '{wiki_id}'. Known: {list(WIKIS)}") from exc

    def search(self, wiki_id: str, query: str, limit: int = 5) -> list[dict]:
        """Full-text search a wiki. Returns [{title, snippet}]."""
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
        }
        hits = self._get_json(self._api(wiki_id), params).get("query", {}).get("search", [])
        return [{"title": h["title"], "snippet": _strip_html(h.get("snippet", ""))} for h in hits]

    def get_page(self, wiki_id: str, title: str, chars: int = 1500) -> WikiResult | None:
        """Fetch a page's plain-text intro and canonical URL.

        Uses action=parse (lead section, section 0) rather than the TextExtracts
        `extracts` prop — the FFXIV wikis don't all have TextExtracts installed,
        but every MediaWiki supports parse. We render the lead HTML and strip it.
        """
        # 1. Resolve redirects + canonical URL.
        info = self._get_json(self._api(wiki_id), {
            "action": "query", "prop": "info", "inprop": "url",
            "redirects": 1, "titles": title, "format": "json",
        }).get("query", {}).get("pages", {})
        page = next(iter(info.values()), {})
        if "missing" in page:
            return None
        resolved_title = page.get("title", title)

        # 2. Fetch the WHOLE page's HTML once; derive prose, infobox, image AND
        # content tables. This used to fetch section 0 only — which meant the
        # tables where wikis keep actual data (Animal Husbandry's animals ->
        # leavings grid, drop tables) were never even downloaded, and the agent
        # would search the same pages over and over finding "nothing". One
        # question cost 54 tool calls before this.
        html = self._page_html(wiki_id, resolved_title)
        extract = _html_to_text(html)
        if len(extract) > chars:
            extract = extract[:chars].rsplit(" ", 1)[0] + "…"
        return WikiResult(
            source=WIKIS[wiki_id]["label"],
            wiki_id=wiki_id,
            title=resolved_title,
            extract=extract,
            url=page.get("fullurl", ""),
            details=_extract_infobox(html),
            image_url=_main_image(html, _origin(self._api(wiki_id)), resolved_title),
            tables=_extract_tables(html),
        )

    def _page_html(self, wiki_id: str, title: str) -> str:
        """Rendered HTML of the full page (cached like every wiki call)."""
        try:
            data = self._get_json(self._api(wiki_id), {
                "action": "parse", "page": title, "prop": "text",
                "redirects": 1, "format": "json",
            })
            return data.get("parse", {}).get("text", {}).get("*", "")
        except Exception:
            return ""

    def lookup(self, wiki_id: str, query: str, chars: int = 1500) -> WikiResult | None:
        """Resolve a query to the best page. The common one-shot path for the model.

        Tries the query as a direct page title first (redirects resolved) — this
        avoids poor search ranking on well-known names. Falls back to search,
        preferring an exact title match over the top hit.
        """
        # Retired wiki ids (gamerescape, fandom) may still arrive from an older
        # conversation transcript or model habit — route them to the wiki we keep
        # rather than crashing on a missing config entry.
        if wiki_id not in WIKIS:
            wiki_id = "consolegames"
        direct = self.get_page(wiki_id, query, chars=chars)
        if direct and direct.extract:
            return direct

        hits = self.search(wiki_id, query, limit=5)
        best = next((h for h in hits if h["title"].lower() == query.lower()), None)
        if not best and hits:
            best = hits[0]
        if best:
            page = self.get_page(wiki_id, best["title"], chars=chars)
            if page and page.extract:
                return page
        return direct


def _strip_html(text: str) -> str:
    """MediaWiki search snippets contain <span> highlight markup — drop it."""
    import re
    return re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()


def _origin(api_url: str) -> str:
    """Scheme + host of a wiki's api.php, for resolving relative image srcs."""
    p = urlsplit(api_url)
    return f"{p.scheme}://{p.netloc}"


def _main_image(html: str, origin: str, title: str) -> str:
    """Pick the page's main image (NPC portrait / item render) from the lead HTML.

    Verified against the live Gamer Escape layout, which has no PageImages API:
    the infobox holds several `<img>`s but most are UI chrome — category badges
    and quest markers named `*_Icon.png`, all tiny. The real portrait is a large
    image whose filename matches the page title (e.g. `Ardashir.png` on the
    "Ardashir" page). So: drop icons, prefer a title-name match, else take the
    largest by declared area. Returns an absolute url, or "" if nothing qualifies.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("[class*=infobox], table.itembox, [class*=portable-infobox]") or soup
    want = title.lower().replace(" ", "_")
    best_url, best_area = "", 0
    for img in box.select("img"):
        src = img.get("src") or ""
        if not src:
            continue
        fname = src.rsplit("/", 1)[-1]
        if "icon" in fname.lower():          # skip UI badges/markers (NPC_Icon, Map33_Icon…)
            continue
        w, h = int(img.get("width") or 0), int(img.get("height") or 0)
        if w < 100:                           # skip stray thumbnails/sprites
            continue
        url = src if src.startswith("http") else origin + ("" if src.startswith("/") else "/") + src
        stem = fname.split(".")[0].lower()   # thumbs look like "350px-Ardashir" — substring match still hits
        if want and want in stem:
            return url
        if w * h > best_area:
            best_url, best_area = url, w * h
    return best_url


def _extract_infobox(html: str) -> str:
    """Pull the infobox as compact key/value text.

    Infoboxes are stripped from the prose extract, but they hold exactly what the
    model needs to place a map pin — the location line, e.g. "Location Limsa
    Lominsa Lower Decks (x8, y11)". We surface that text so the assistant can read
    the zone + coordinates and call pin_on_map with them.
    """
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    # Infobox class varies by wiki: infobox-n (Console Games), itembox (Gamer
    # Escape), portable-infobox (Fandom).
    box = soup.select_one("[class*=infobox], table.itembox, [class*=portable-infobox]")
    if not box:
        return ""
    text = re.sub(r"\s+", " ", box.get_text(" ", strip=True)).strip()
    return text[:600]


def _extract_tables(html: str, max_chars: int = 6000) -> str:
    """The page's content tables, flattened to pipe-separated text.

    Wikis keep their DATA in tables — which animal drops which leaving, what a
    coffer contains — and prose extraction throws tables away by design. Each
    table renders as its nearest heading, then one line per row with cells
    joined by " | ". Infoboxes/navboxes are excluded (they're chrome, and the
    infobox already has its own field). Bounded hard: this text rides into the
    model's context on every wiki lookup.
    """
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("div", class_="mw-parser-output") or soup
    out: list[str] = []
    used = 0
    for table in root.find_all("table"):
        cls = " ".join(table.get("class") or [])
        if any(k in cls for k in ("infobox", "navbox", "itembox", "toc")):
            continue
        # Nearest preceding heading names the table ("Animal Husbandry").
        heading = ""
        for prev in table.find_all_previous(["h2", "h3", "h4", "caption"]):
            heading = prev.get_text(" ", strip=True).replace("[edit]", "").strip()
            if heading:
                break
        rows = []
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True))
                     for td in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
        if len(rows) < 2:      # a one-row "table" is layout, not data
            continue
        body = "\n".join(rows[:60])
        if len(rows) > 60:
            body += f"\n(+{len(rows) - 60} more rows)"
        block = (f"## {heading}\n" if heading else "") + body
        if used + len(block) > max_chars:
            out.append("(more tables on the page — truncated)")
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def _html_to_text(html: str) -> str:
    """Turn a rendered wiki lead section into clean prose.

    Two-step, tuned against the actual FFXIV wikis:
    1. Strip page furniture — infoboxes (which appear as div.infobox-n on
       Console Games, not <table>), hatnotes, maintenance banners (.mbox on
       Fandom), navboxes, figures, references, edit links.
    2. Keep text from <p> paragraphs only. The real summary lives in <p>; this
       dodges stray non-paragraph noise (e.g. Fandom's floating release-date div)
       that survives selective stripping. Falls back to full text if no <p>.
    """
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("div", class_="mw-parser-output") or soup

    junk = (
        "table", "style", "script", "img", "figure",
        "sup.reference", "span.mw-editsection", ".toc",
        "[class*=infobox]", ".hatnote", ".dablink",
        ".mbox", ".ambox", ".messagebox", ".notice", ".metadata",
        "[class*=navbox]", "blockquote.pull-quote",
    )
    for sel in junk:
        for el in root.select(sel):
            el.decompose()

    paras = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    text = " ".join(p for p in paras if p) or root.get_text(" ", strip=True)
    text = re.sub(r"\[\d+\]", "", text)      # stray reference markers
    text = re.sub(r"\s+", " ", text).strip()  # collapse whitespace
    return text


if __name__ == "__main__":
    # Quick manual smoke test (hits the live wikis).
    client = WikiClient()
    for wid in WIKIS:
        label = WIKIS[wid]["label"]
        try:
            res = client.lookup(wid, "Bahamut")
            if res:
                print(f"[OK]   {label}: {res.title} -> {res.url}\n       {res.extract[:110]}...\n")
            else:
                print(f"[MISS] {label}: no result\n")
        except Exception as exc:
            print(f"[FAIL] {label}: {type(exc).__name__}: {exc}\n")
    client.close()
