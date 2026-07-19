"""Annotate images with fight-guide callouts.

Takes a source image (a map, arena diagram, or screenshot) plus a list of
annotations and draws them on top: numbered markers, safe-spot circles, arrows,
and text labels. This is what turns a raw wiki image into a "stack here on cast 2,
spread on cast 3" guide diagram — the capability existing sites do worst.

The model calls this via the `annotate_image` tool with a structured spec; the
result is saved to the per-chat assets folder and shown in the right-hand panel.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

# Palette tuned to read on the dark arena backdrops FFXIV screenshots tend to have.
COLORS = {
    "safe": (93, 202, 165),      # teal   — safe spots
    "danger": (240, 153, 123),   # coral  — AoE / avoid
    "marker": (250, 199, 117),   # amber  — numbered sequence
    "note": (175, 169, 236),     # purple — neutral labels
    "boss": (240, 149, 122),
}


@dataclass
class Annotation:
    kind: str                    # "marker" | "circle" | "arrow" | "label"
    x: float                     # 0..1 relative coords (resolution-independent)
    y: float
    x2: float | None = None      # arrow endpoint (relative)
    y2: float | None = None
    radius: float = 0.06         # circle radius, relative to image width
    text: str = ""
    color: str = "note"          # key into COLORS


@dataclass
class AnnotationSpec:
    title: str = ""
    annotations: list[Annotation] = field(default_factory=list)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate(image_bytes: bytes, spec: AnnotationSpec) -> bytes:
    """Draw `spec` over the source image; return PNG bytes."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size

    marker_n = 0
    for a in spec.annotations:
        color = COLORS.get(a.color, COLORS["note"])
        px, py = a.x * w, a.y * h

        if a.kind == "circle":
            r = a.radius * w
            draw.ellipse([px - r, py - r, px + r, py + r], outline=color + (255,), width=max(3, w // 300))
            if a.text:
                _text(draw, px, py - r - 18, a.text, color)

        elif a.kind == "arrow":
            x2, y2 = (a.x2 or a.x) * w, (a.y2 or a.y) * h
            _arrow(draw, px, py, x2, y2, color, width=max(3, w // 300))
            if a.text:
                _text(draw, (px + x2) / 2, (py + y2) / 2 - 16, a.text, color)

        elif a.kind == "label":
            _text(draw, px, py, a.text, color, boxed=True)

        else:  # "marker" — numbered sequence dot
            marker_n += 1
            r = max(14, w // 45)
            draw.ellipse([px - r, py - r, px + r, py + r], fill=color + (235,))
            _centered_number(draw, px, py, str(marker_n), r)
            if a.text:
                _text(draw, px, py + r + 6, a.text, color)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _arrow(draw, x1, y1, x2, y2, color, width):
    import math
    draw.line([x1, y1, x2, y2], fill=color + (255,), width=width)
    ang = math.atan2(y2 - y1, x2 - x1)
    head = max(12, width * 4)
    for s in (-0.5, 0.5):
        hx = x2 - head * math.cos(ang - s)
        hy = y2 - head * math.sin(ang - s)
        draw.line([x2, y2, hx, hy], fill=color + (255,), width=width)


def _text(draw, x, y, text, color, boxed=False):
    font = _font(22)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x0 = x - tw / 2
    if boxed:
        pad = 6
        draw.rectangle([x0 - pad, y - pad, x0 + tw + pad, y + th + pad], fill=(20, 20, 20, 200))
    draw.text((x0, y), text, fill=color + (255,), font=font)


def _centered_number(draw, cx, cy, text, r):
    font = _font(int(r * 1.2))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2, cy - th / 2 - bbox[1]), text, fill=(20, 20, 20, 255), font=font)


def spec_from_dict(data: dict) -> AnnotationSpec:
    """Build a spec from the JSON the model passes to the annotate_image tool."""
    return AnnotationSpec(
        title=data.get("title", ""),
        annotations=[Annotation(**a) for a in data.get("annotations", [])],
    )
