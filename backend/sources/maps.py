"""Map pin engine — place an exact pin on the real FFXIV zone map.

The whole point: never *generate* a map (that hallucinates geography). Instead we
fetch the OFFICIAL map image and place the pin by math, using the map's SizeFactor
to convert an in-game coordinate (the X/Y "flag" values players see) to a texture
pixel. The pin lands exactly right — it's arithmetic, not estimation.

Data comes from XIVAPI v2 (v2.xivapi.com); the older xivapi.com (v1) is currently
unreliable (500s). v2 gives map metadata (SizeFactor, offsets, the map Id like
"s1t2/01") and serves the composed map image at /api/asset/map/{territory}/{index}.

Coordinate -> pixel formula (validated against known map behavior):
    c = SizeFactor / 100
    pixel_2048 = (coord - 1) * c / 41 * 2048
Invariants: coord 1 -> pixel 0; SizeFactor 100 -> visible range ~1..42;
SizeFactor 200 -> ~1..21.5 (zoomed). The served image may differ from 2048px, so
we rescale to the actual fetched dimensions.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from curl_cffi import requests as cffi
from PIL import Image, ImageDraw, ImageFont

from config import USER_AGENT

V2 = "https://v2.xivapi.com/api"
TEX = 2048.0  # source map texture size the formula is defined against


@dataclass
class MapInfo:
    map_id: str          # e.g. "s1t2/01"
    territory: str       # e.g. "s1t2"
    index: str           # e.g. "01"
    place_name: str
    size_factor: int
    offset_x: int
    offset_y: int


class MapClient:
    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._s.close()

    def find_map(self, place_name: str) -> MapInfo | None:
        """Resolve a zone/place name to its map metadata via XIVAPI v2 search."""
        r = self._s.get(
            f"{V2}/search",
            params={
                "sheets": "Map",
                "query": f'PlaceName.Name~"{place_name}"',
                "fields": "Id,SizeFactor,OffsetX,OffsetY,PlaceName.Name",
                "limit": 1,
            },
            timeout=self._timeout,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        f = results[0]["fields"]
        map_id = f["Id"]  # "territory/index"
        territory, _, index = map_id.partition("/")
        place = (f.get("PlaceName") or {}).get("fields", {}).get("Name", place_name)
        return MapInfo(
            map_id=map_id, territory=territory, index=index, place_name=place,
            size_factor=f.get("SizeFactor", 100),
            offset_x=f.get("OffsetX", 0), offset_y=f.get("OffsetY", 0),
        )

    def fetch_image(self, info: MapInfo) -> bytes:
        r = self._s.get(f"{V2}/asset/map/{info.territory}/{info.index}", timeout=self._timeout)
        r.raise_for_status()
        return r.content

    def pin(self, place_name: str, x: float, y: float, label: str = "") -> dict | None:
        """Fetch the real map for `place_name` and draw a pin at in-game coord (x, y).

        Returns {image (PNG bytes), place_name, pixel: [px, py], map_id} or None if
        the map can't be resolved.
        """
        info = self.find_map(place_name)
        if not info:
            return None
        img_bytes = self.fetch_image(info)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        w, h = img.size

        px = coord_to_pixel(x, info.size_factor, w)
        py = coord_to_pixel(y, info.size_factor, h)
        out = _draw_pin(img, px, py, label or f"{x:.1f}, {y:.1f}")
        _draw_header(out, "", info.place_name)

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return {
            "image": buf.getvalue(),
            "place_name": info.place_name,
            "map_id": info.map_id,
            "pixel": [round(px), round(py)],
            "coord": [x, y],
        }


def pin_on_image(img_bytes: bytes, x: float, y: float, size_factor: int,
                 label: str = "", max_dim: int = 1600,
                 region: str = "", zone: str = "",
                 labels: list | None = None, label_ref: int = 2048,
                 zoom: float = 0.6) -> dict:
    """Draw a pin at in-game coord (x, y) on ANY full map texture (e.g. the A Realm
    Remapped labeled map). Uses the same SizeFactor coord math, then downscales the
    result to `max_dim`. Extras that make it read like the in-game map:
    - `region`/`zone`: an in-game-style location header, top-left.
    - `labels`/`label_ref`: A Realm Remapped place-name labels (name, x, y in a
      `label_ref`-sized pixel space) drawn onto the map.
    - `zoom`: crop a square window (this fraction of the short edge) CENTERED on the
      pin so the spot fills the frame instead of the whole zone shrunk. 0 disables."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    OW, OH = img.size
    px = coord_to_pixel(x, size_factor, OW)
    py = coord_to_pixel(y, size_factor, OH)

    ox = oy = 0
    if zoom and 0 < zoom < 1:
        cw = int(min(OW, OH) * zoom)
        ox = int(min(max(px - cw / 2, 0), max(OW - cw, 0)))
        oy = int(min(max(py - cw / 2, 0), max(OH - cw, 0)))
        img = img.crop((ox, oy, min(ox + cw, OW), min(oy + cw, OH)))
        px, py = px - ox, py - oy

    if labels:
        _draw_labels(img, labels, label_ref, OW, OH, ox, oy)
    out = _draw_pin(img, px, py, label or f"{x:.1f}, {y:.1f}")
    _draw_header(out, region, zone)
    if max(out.size) > max_dim:
        scale = max_dim / max(out.size)
        out = out.resize((round(out.size[0] * scale), round(out.size[1] * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return {"image": buf.getvalue(), "pixel": [round(px), round(py)], "coord": [x, y]}


def _draw_labels(img: Image.Image, labels: list, ref: int,
                 orig_w: int, orig_h: int, ox: int, oy: int) -> None:
    """Draw A Realm Remapped markers onto the map: place-name labels (Wineport, Costa
    del Sol…) and aetheryte crystals. Each marker's (x, y) is in a `ref`-sized pixel
    space over the FULL map, so map it to the full image, subtract the crop offset, and
    skip any that fall off-frame. Markers are dicts: {name, x, y, kind}."""
    if not labels or ref <= 0:
        return
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    fsize = max(13, W // 58)
    font = _font(fsize)
    stroke = max(2, fsize // 7)
    for mk in labels:
        try:
            if isinstance(mk, dict):
                name, lx, ly = str(mk.get("name", "")), float(mk["x"]), float(mk["y"])
                kind = mk.get("kind", "label")
            else:  # tolerate the older (name, x, y) tuple shape
                name, lx, ly, kind = str(mk[0]), float(mk[1]), float(mk[2]), "label"
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        fx = lx / ref * orig_w - ox
        fy = ly / ref * orig_h - oy
        if not (0 <= fx <= W and 0 <= fy <= H):
            continue
        if kind == "aetheryte":
            _draw_aetheryte(d, fx, fy, max(7, W // 90), font, stroke)
            continue
        if not name:
            continue
        b = d.textbbox((0, 0), name, font=font, stroke_width=stroke)
        d.text((fx - (b[2] - b[0]) / 2, fy - (b[3] - b[1]) / 2), name, font=font,
               fill=(255, 255, 255, 240), stroke_width=stroke, stroke_fill=(18, 20, 26, 240))


def _draw_aetheryte(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float,
                    font, stroke: int) -> None:
    """An aetheryte crystal at (cx, cy) — the blue gem players teleport to, drawn the
    way the in-game map flags them, with a small caption so it's unambiguous."""
    blue, glow, white = (74, 190, 232, 255), (140, 226, 250, 255), (255, 255, 255, 240)
    # Crystal: a tall diamond with a lighter top facet.
    d.polygon([(cx, cy - s * 1.5), (cx + s * 0.8, cy), (cx, cy + s * 1.5), (cx - s * 0.8, cy)],
              fill=blue, outline=white, width=max(1, int(s // 5)))
    d.polygon([(cx, cy - s * 1.5), (cx + s * 0.8, cy), (cx, cy)], fill=glow)
    cap, cy2 = "Aetheryte", cy + s * 1.5 + 3
    b = d.textbbox((0, 0), cap, font=font, stroke_width=stroke)
    d.text((cx - (b[2] - b[0]) / 2, cy2), cap, font=font,
           fill=(176, 232, 252, 245), stroke_width=stroke, stroke_fill=(12, 24, 34, 240))


def coord_to_pixel(coord: float, size_factor: int, actual_dim: int) -> float:
    """In-game (flag) map coordinate -> pixel on the fetched image.

    Verified formula: pixel_2048 = (coord - 1) * (SizeFactor/100) / 41 * 2048,
    rescaled to the actual image size. OffsetX/OffsetY intentionally NOT used —
    they apply only to raw world coordinates, not the flag coords players see.
    """
    c = size_factor / 100.0
    pixel_2048 = (coord - 1.0) * c / 41.0 * TEX
    return pixel_2048 / TEX * actual_dim


def _font(size: int):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_pin(img: Image.Image, px: float, py: float, label: str) -> Image.Image:
    """Draw a teardrop map pin whose BOTTOM TIP sits exactly at (px, py).

    (px, py) is the anchor — the location. The circle floats above it and the
    tail narrows down to the tip, so the point (not the circle center) marks the
    spot, matching how map pins are read.
    """
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    coral, white = (216, 90, 48), (255, 255, 255)
    r = max(10, img.size[0] // 90)
    cy = py - r * 2.4  # circle center sits above the tip

    # Tail: from the circle's lower flanks down to the exact tip at (px, py).
    d.polygon([(px - r * 0.62, cy + r * 0.35), (px + r * 0.62, cy + r * 0.35), (px, py)], fill=coral + (255,))
    # Head: filled circle with a white ring and a white center dot.
    d.ellipse([px - r, cy - r, px + r, cy + r], fill=coral + (255,), outline=white + (255,), width=max(2, r // 4))
    d.ellipse([px - r // 3, cy - r // 3, px + r // 3, cy + r // 3], fill=white + (255,))

    if label:
        w, h = img.size
        font = _font(max(16, w // 60))
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad, m = 6, 8                      # box padding; margin from the image edge
        lx = px - tw / 2                   # centered over the pin…
        ly = cy - r - th - 10              # …and above the head by default
        if ly - 4 < m:                     # would clip the top edge -> drop it below the tip
            ly = py + r + 10
        # Keep the whole label box on-canvas so text never runs off an edge (the
        # bug when a pin sits near the left/top border).
        lx = min(max(lx, m + pad), w - tw - m - pad)
        ly = min(max(ly, m + 4), h - th - m - 6)
        d.rectangle([lx - pad, ly - 4, lx + tw + pad, ly + th + 6], fill=(20, 20, 20, 210))
        d.text((lx, ly - tb[1]), label, fill=white + (255,), font=font)

    return Image.alpha_composite(img, overlay).convert("RGB")


def _draw_header(img: Image.Image, region: str, zone: str) -> None:
    """Draw an in-game-style location header in the TOP-LEFT: the region (small,
    parchment-gold) over the zone/town (bold, white) in a translucent panel — the
    way FFXIV's own map labels the area you're looking at. Mutates `img` in place."""
    region, zone = (region or "").strip(), (zone or "").strip()
    if not (region or zone):
        return
    d = ImageDraw.Draw(img, "RGBA")
    W = img.size[0]
    zone_font = _font(max(20, W // 40))
    region_font = _font(max(13, W // 66))
    m = max(10, W // 70)                 # margin from the top-left corner
    padx, pady, gap = 14, 10, 3

    # (text, font, width, height, color), region first so it sits on top.
    lines = []
    for text, font, color in ((region, region_font, (198, 172, 120, 255)),
                              (zone, zone_font, (255, 255, 255, 255))):
        if not text:
            continue
        b = d.textbbox((0, 0), text, font=font)
        lines.append((text, font, b, color))

    box_w = padx * 2 + max(b[2] - b[0] for _, _, b, _ in lines)
    box_h = pady * 2 + sum(b[3] - b[1] for _, _, b, _ in lines) + gap * (len(lines) - 1)
    panel = [m, m, m + box_w, m + box_h]
    try:
        d.rounded_rectangle(panel, radius=8, fill=(18, 20, 24, 205))
    except AttributeError:               # older Pillow without rounded_rectangle
        d.rectangle(panel, fill=(18, 20, 24, 205))

    ty = m + pady
    for text, font, b, color in lines:
        d.text((m + padx, ty - b[1]), text, font=font, fill=color)
        ty += (b[3] - b[1]) + gap
