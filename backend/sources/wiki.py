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

    def get_page(self, wiki_id: str, title: str, chars: int = 1500,
                 query: str = "") -> WikiResult | None:
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
            # Tables AND section bullet lists — both are where wikis keep the
            # actual data (drop grids, reward catalogues). The query steers
            # which sections win the budget on oversized pages — minus the
            # page-title words, which would boost every section equally
            # ("Cosmic Fortunes" ranking high because the query said "Cosmic").
            tables=(_extract_tables(html) + "\n\n"
                    + _extract_section_lists(html, query=_query_minus_title(
                        query, resolved_title))).strip(),
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
        # Models send Google-style quoted phrases; MediaWiki takes them
        # literally and finds nothing. Strip before any resolution.
        query = query.replace('"', " ").strip()
        direct = self.get_page(wiki_id, query, chars=chars, query=query)
        if direct and direct.extract:
            return direct

        hits = self.search(wiki_id, query, limit=5)
        if not hits:
            # This wiki's search is effectively AND: "Cosmic Exploration
            # mounts" gets ZERO hits even though the page exists (one flailing
            # chat logged ~40 such misses). Retry with the TitleCase run —
            # the page-name part of the query; the leftover words still steer
            # section extraction via `query`.
            import re
            runs = re.findall(r"[A-Z][\w']*(?: [A-Z][\w']*)+", query)
            for run in sorted(runs, key=len, reverse=True)[:2]:
                hits = self.search(wiki_id, run, limit=5)
                if hits:
                    break
        best = next((h for h in hits if h["title"].lower() == query.lower()), None)
        if not best and hits:
            best = hits[0]
        if best:
            page = self.get_page(wiki_id, best["title"], chars=chars, query=query)
            if page and page.extract:
                return page
        return direct

    def _wikitext(self, wiki_id: str, title: str) -> tuple[str, str]:
        """(resolved_title, raw wikitext) for a page, redirects resolved; empty
        strings if it's missing. Wikitext, not rendered HTML, because an action
        page's ==History== is a list of {{patch|version|text}} templates that read
        cleanest at the source (and the HTML section extractor deliberately drops
        History as chrome)."""
        data = self._get_json(self._api(wiki_id), {
            "action": "parse", "page": title, "prop": "wikitext",
            "redirects": 1, "format": "json",
        })
        parse = data.get("parse", {})
        return parse.get("title", ""), parse.get("wikitext", {}).get("*", "")

    def ability_history(self, name: str, wiki_id: str = "consolegames") -> dict:
        """Patch-by-patch change history for an ABILITY, from its wiki action
        page's ==History== section. This is what catches the reworks the official
        patch notes never name — e.g. Huton's weaponskill/auto-attack-speed buff
        being revamped out in 7.0. Returns {found, topic, url, changes:[{patch,
        text}]} newest-first, or {found: False} when `name` isn't an action page (a
        lore namesake, an item, a system), so the caller can fall back to the notes
        archive."""
        name = (name or "").strip()
        if wiki_id not in WIKIS:
            wiki_id = "consolegames"
        if not name:
            return {"found": False}

        resolved, wt = self._wikitext(wiki_id, name)
        entries = _parse_ability_history(wt)
        if not entries:
            # A bare name may resolve to the lore page (e.g. "Bahamut" the Primal);
            # the action lives at a different title. Scan search hits for the one
            # that actually carries a {{patch}} history block.
            for hit in self.search(wiki_id, name, limit=5):
                if hit["title"].lower() == name.lower():
                    continue
                r2, wt2 = self._wikitext(wiki_id, hit["title"])
                e2 = _parse_ability_history(wt2)
                if e2:
                    resolved, entries = r2, e2
                    break
        if not entries:
            return {"found": False, "topic": name}

        return {
            "found": True,
            "topic": resolved,
            "source": WIKIS[wiki_id]["label"],
            "url": _origin(self._api(wiki_id)) + "/wiki/" + resolved.replace(" ", "_"),
            "changed_in": [e["patch"] for e in entries],
            "changes": entries,       # newest first, as the wiki lists them
        }


def _split_top_pipes(s: str, limit: int) -> list[str]:
    """Split `s` on top-level `|` only (never inside {{…}} or [[…]]), at most
    `limit` times — so {{patch|7.0|…{{action icon|Foo}}…}} splits into
    ['patch', '7.0', '…{{action icon|Foo}}…'] without breaking on the inner pipe."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        if two in ("{{", "[["):
            depth += 1; buf.append(two); i += 2
        elif two in ("}}", "]]"):
            depth = max(0, depth - 1); buf.append(two); i += 2
        elif s[i] == "|" and depth == 0 and len(parts) < limit:
            parts.append("".join(buf)); buf = []; i += 1
        else:
            buf.append(s[i]); i += 1
    parts.append("".join(buf))
    return parts


def _clean_wikitext(t: str) -> str:
    """Flatten a wiki change line to plain text — nested templates to their name,
    links to their label, drop bullet/bold markup."""
    import re
    t = re.sub(r"\{\{[^{}|]*\|([^{}|]*)(?:\|[^{}]*)?\}\}", r"\1", t)  # {{action icon|Foo}} -> Foo
    t = re.sub(r"\{\{([^{}]*)\}\}", r"\1", t)                        # {{bare}} -> bare
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)               # [[link|label]] -> label
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)                        # [[link]] -> link
    t = re.sub(r"'''?", "", t).replace("*", " ")                    # bold/italic, bullets
    t = re.sub(r"<[^>]+>", "", t)                                   # stray <br> etc.
    return re.sub(r"\s+", " ", t).strip()


def _parse_ability_history(wikitext: str) -> list[dict]:
    """An action page's ==History== section as [{patch, text}], newest first,
    parsed from its {{patch|version|text}} entries. [] if there's no such section."""
    import re
    if not wikitext:
        return []
    m = re.search(r"==+\s*History\s*==+", wikitext, re.I)
    if not m:
        return []
    section = wikitext[m.end():]
    nxt = re.search(r"\n==+[^=]", section)     # next top-level heading ends it
    if nxt:
        section = section[:nxt.start()]

    entries: list[dict] = []
    for start in re.finditer(r"\{\{patch\|", section, re.I):
        j = start.start()
        depth = 0
        k = j
        while k < len(section):               # brace-match to the closing }}
            if section[k:k + 2] == "{{":
                depth += 1; k += 2
            elif section[k:k + 2] == "}}":
                depth -= 1; k += 2
                if depth == 0:
                    break
            else:
                k += 1
        parts = _split_top_pipes(section[j + 2:k - 2], limit=2)   # patch|version|text
        if len(parts) < 2 or parts[0].strip().lower() != "patch":
            continue
        version = parts[1].strip()
        if not version:
            continue
        text = _clean_wikitext(parts[2]) if len(parts) >= 3 else ""
        entries.append({"patch": version, "text": text[:600]})
        if len(entries) >= 40:
            break
    return entries


def _query_minus_title(query: str, title: str) -> str:
    """The query words that are NOT part of the resolved page title — the part
    that says what the caller wants FROM the page ("mounts", "outfits")."""
    import re
    title_words = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", title.lower())}
    kept = [w for w in query.split()
            if w.strip('"').lower().rstrip("s") not in title_words]
    return " ".join(kept)


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


def _extract_section_lists(html: str, max_chars: int = 6000, query: str = "") -> str:
    """Per-section content — paragraphs AND bullet lists — flattened for the model.

    Tables aren't the only place wikis keep data: reward and unlock catalogues
    are often icon+link BULLET LISTS under an h2/h3 (Cosmic Exploration keeps
    its whole Mounts/Glamour rewards list this way), and some facts live only
    in mid-page PROSE ("completing Mech Ops rewards the ... mount") that the
    1500-char lead extract never reaches. Case study: without these, GPT-5.4
    mini re-searched one page ~50 times across 24 rounds looking for content
    that was on it all along. Same bounds philosophy as _extract_tables: this
    text rides into the model's context on every lookup.
    """
    import re
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("div", class_="mw-parser-output") or soup
    # Sections that are navigation/citation/media chrome or patch-note noise,
    # never answer data.
    skip_headings = {
        "contents", "references", "navigation", "resources", "external links",
        "gallery", "screenshots", "concept art", "history", "trivia",
    }
    prose_cap = 900   # per section — keeps one lore-heavy section from eating the budget

    def _chrome(el) -> bool:
        for anc in el.parents:
            cls = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
            if any(k in cls for k in ("toc", "navbox", "infobox", "gallery", "reference")):
                return True
        return False

    # Pass 1: one walk in document order, grouping content under its heading.
    # (find_all preserves document order; tracking the current heading here
    # avoids an O(n²) find_all_previous per element on 400KB pages.)
    sections: list[tuple[str, list[str], list[str]]] = []  # (heading, prose, items)
    heading, prose, items = "", [], []

    def _flush():
        nonlocal heading, prose, items
        keep_prose = prose if heading else []  # lead prose already ships as `extract`
        if heading.lower() not in skip_headings and (len(items) >= 2 or keep_prose):
            sections.append((heading, keep_prose, items))
        heading, prose, items = "", [], []

    for el in root.find_all(["h2", "h3", "h4", "p", "ul"]):
        if el.name in ("h2", "h3", "h4"):
            _flush()
            heading = el.get_text(" ", strip=True).replace("[edit]", "").strip()
            continue
        if _chrome(el) or el.find_parent("table"):
            continue
        if el.name == "p":
            txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if len(txt) >= 40:  # shorter = captions/stubs
                prose.append(txt)
        else:  # ul — top-level only; nested ones ride along with their parent item
            if el.find_parent("ul"):
                continue
            for li in el.find_all("li", recursive=False):
                for sub in li.find_all("ul"):
                    sub.extract()  # child items would otherwise repeat as their own text
                txt = re.sub(r"\s+", " ", li.get_text(" ", strip=True))
                if txt:
                    items.append(txt[:200])
    _flush()

    blocks: list[tuple[str, str]] = []  # (heading, block text)
    for heading, prose, items in sections:
        parts = []
        if prose:
            p = " ".join(prose)
            parts.append(p[:prose_cap] + ("…" if len(p) > prose_cap else ""))
        if len(items) >= 2:  # a one-item "list" is layout, not data
            body = "\n".join("- " + i for i in items[:60])
            if len(items) > 60:
                body += f"\n(+{len(items) - 60} more)"
            parts.append(body)
        if not parts:
            continue
        blocks.append((heading, (f"## {heading}\n" if heading else "") + "\n".join(parts)))

    # Pass 2: the budget goes to query-relevant sections first. A huge page
    # (Cosmic Exploration: 30+ sections) can't fit whole, and document order
    # would spend the whole budget on preamble while the asked-about section
    # ("Mounts") sits at the bottom. Plural-insensitive word match, headings
    # weighted; ties keep document order (sort is stable).
    words = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", query.lower())}
    def score(b: tuple[str, str]) -> int:
        head = {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", b[0].lower())}
        body_hits = sum(1 for w in words if w and w in b[1].lower())
        return 10 * len(words & head) + body_hits
    ranked = sorted(blocks, key=score, reverse=True) if words else blocks

    out: list[str] = []
    dropped: list[str] = []
    used = 0
    for heading, block in ranked:
        if used + len(block) > max_chars:
            # Name what didn't fit: the model can re-query "<page> <section>"
            # and relevance ranking will put that section first.
            if heading and heading not in dropped:
                dropped.append(heading)
            continue
        out.append(block)
        used += len(block)
    if dropped:
        out.append("(lists truncated — the page also has sections: "
                   + ", ".join(dropped[:15])
                   + ". Search again as '<page title> <section name>' to read one.)")
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
