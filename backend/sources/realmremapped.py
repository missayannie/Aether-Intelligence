"""A Realm Remapped integration — resolve a zone name to its interactive-map URL.

A Realm Remapped (https://arealmremapped.com, by Icarus Twine) is a community
collection of interactive FFXIV zone maps (FATEs, nodes, hunts, aether currents…).
Its per-zone pages follow /<Region>/<Zone>/<Zone>.html, but the assistant won't
know a zone's exact path/region, so we scrape the index page once to build a
{zone name -> url} map and resolve against it.

This only produces URLs; the actual interactive map renders in the app's embedded
Map tab (their site, their UI — credited and linked, never reframed as ours).
"""
from __future__ import annotations

import json
import re

from curl_cffi import requests as cffi

from config import USER_AGENT
from sources import cache

BASE = "https://arealmremapped.com/"
CREDIT = "A Realm Remapped (by Icarus Twine)"

# FFXIV map-icon id for an Aetheryte. Verified against the live data: the only two
# `060453` markers in Eastern La Noscea sit exactly on the Costa del Sol and Wineport
# aetherytes (the zone's only two). These markers carry no name — the icon is the label.
AETHERYTE_ICON = "060453"


class RealmRemappedClient:
    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})
        self._index: dict[str, str] | None = None  # normalized zone name -> full url

    def close(self) -> None:
        self._s.close()

    def _load_index(self) -> dict[str, str]:
        if self._index is not None:
            return self._index
        from bs4 import BeautifulSoup

        idx: dict[str, str] = {}
        try:
            html = self._s.get(BASE, timeout=self._timeout).text
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                name = a.get_text(strip=True)
                # zone links look like Region/Zone/Zone.html; skip malformed/empty
                if not name or not href.endswith(".html") or "//" in href:
                    continue
                url = href if href.startswith("http") else BASE + href.lstrip("/")
                idx.setdefault(_norm(name), url)
        except Exception:
            pass
        self._index = idx
        return idx

    def resolve(self, zone: str) -> dict | None:
        """Resolve a zone name to its map page. Exact match first, then contains."""
        idx = self._load_index()
        if not idx:
            return None
        key = _norm(zone)
        url = idx.get(key)
        if not url:
            # loose match: the query is contained in a zone name or vice-versa
            for name, u in idx.items():
                if key and (key in name or name in key):
                    url = u
                    break
        if not url:
            return None
        return {"source": CREDIT, "zone": zone, "url": url}

    def base_map(self, zone: str) -> dict | None:
        """Fetch A Realm Remapped's LABELED base map image for a zone (the in-game
        parchment map with place names) plus its SizeFactor, so a pin can be drawn on
        it with the same coord math. Returns {image (PNG bytes), size_factor, url,
        source, zone, region} or None if the zone/image can't be found."""
        from urllib.parse import urljoin

        r = self.resolve(zone)
        if not r:
            return None
        page = r["url"]
        # The page path is /<Region>/<Zone>/<Zone>.html, so the region and the
        # canonical zone name come straight from the URL — used for the in-game-style
        # header drawn on the pinned map.
        region, canon_zone = _region_zone_from_url(page)
        # All three fetches below are cached for a month: a zone's page, texture and
        # markers only change when the game patches. The texture alone is ~6.5 MB and
        # was being re-downloaded on EVERY pin.
        try:
            html = cache.fetch_text("map", page, cache.TTL_MAP_ASSET,
                                    lambda: self._s.get(page, timeout=self._timeout).text)
        except Exception:
            return None
        m = re.search(r'var\s+baseurl\s*=\s*"([^"]+)"', html)
        if not m:
            return None
        img_url = urljoin(page, m.group(1))
        sf = re.search(r"SizeFactor\s*:\s*(\d+)", html)
        size_factor = int(sf.group(1)) if sf else 100

        def _load_image() -> bytes:
            resp = self._s.get(img_url, timeout=self._timeout)
            if resp.status_code != 200 or "image" not in (resp.headers.get("content-type") or ""):
                return b""
            return resp.content

        try:
            image = cache.fetch("map", img_url, cache.TTL_MAP_ASSET, _load_image)
        except Exception:
            return None
        if not image:
            return None
        labels, label_ref = self._place_labels(page, html)
        return {"source": CREDIT, "zone": canon_zone or r["zone"], "region": region,
                "url": page, "image": image, "size_factor": size_factor,
                "labels": labels, "label_ref": label_ref}

    def _place_labels(self, page: str, html: str) -> tuple[list[dict], int]:
        """A Realm Remapped renders map markers (place-name labels AND aetherytes) as a
        Leaflet GeoJSON layer (json/mapmarkerGeo.geojson.js), NOT baked into the base
        texture. Fetch it so we can draw them onto the pinned image. Returns markers as
        [{name, x, y, kind}] in the map's reference-pixel space, plus that space's size.
        `kind` is "aetheryte" for aetheryte crystals (they have no name, just the icon)
        or "label" for named places, so the drawing code needn't know FFXIV icon ids.

        Coords are Leaflet CRS.Simple image pixels (0..<bounds>, y measured from the
        top); `var bounds = [[N,0],[0,N]]` on the page gives N (the image is stretched
        to fill it), so a marker at (x, y) sits at (x/N*W, y/N*H) on a W×H base image."""
        from urllib.parse import urljoin

        mb = re.search(r"var\s+bounds\s*=\s*\[\s*\[\s*(\d+)", html)
        ref = int(mb.group(1)) if mb else 2048
        markers_url = urljoin(page, "json/mapmarkerGeo.geojson.js")
        try:
            txt = cache.fetch_text("map", markers_url, cache.TTL_MAP_ASSET,
                                   lambda: self._s.get(markers_url, timeout=self._timeout).text)
            m = re.search(r"=\s*(\{.*\})\s*;?\s*$", txt, re.S)
            data = json.loads(m.group(1) if m else txt)
        except Exception:
            return [], ref
        out: list[dict] = []
        for f in data.get("features", []):
            props = f.get("properties") or {}
            coords = (f.get("geometry") or {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            name = str(props.get("name") or props.get("dataid") or "")
            is_aetheryte = str(f.get("iconUrl") or "") == AETHERYTE_ICON
            if not name and not is_aetheryte:
                continue  # an unnamed, non-aetheryte marker has nothing to show
            try:
                out.append({
                    "name": name, "x": float(coords[0]), "y": float(coords[1]),
                    "kind": "aetheryte" if is_aetheryte else "label",
                })
            except (TypeError, ValueError):
                pass
        return out, ref


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _region_zone_from_url(url: str) -> tuple[str, str]:
    """(region, zone) from a /<Region>/<Zone>/<Zone>.html page url, url-decoded.
    Returns ("", "") if the path doesn't have the expected shape."""
    from urllib.parse import urlsplit, unquote

    parts = [unquote(p).replace("_", " ") for p in urlsplit(url).path.split("/")
             if p and not p.lower().endswith(".html")]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return "", parts[-1]
    return "", ""
