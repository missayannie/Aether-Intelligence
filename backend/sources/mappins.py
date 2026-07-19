"""The player's custom map pins — one store, two writers.

Both the UI (add/remove in the Map tab) and the AGENT (pin_on_map) write here, so
an agent-placed pin behaves exactly like one the player placed: it shows under
"My pins", persists across restarts, and right-click removes it. Coordinates are in
the map's 2048 space, same as the game's own markers.

Global, not per chat: a pin marks a place in the world, and the world doesn't
belong to one conversation.
"""
from __future__ import annotations

import json
import uuid

from paths import DATA_DIR

_PATH = DATA_DIR / "map_pins.json"


def load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    _PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")


def for_zone(zone: str) -> list:
    return load().get(zone, [])


DEFAULT_COLOR = "#e6b800"   # the gold the pins launched with


def add(zone: str, x: float, y: float, label: str = "",
        color: str = DEFAULT_COLOR, kind: str = "", icon: str = "") -> dict:
    """kind groups pins under their own toolbar toggle ("gathering" -> the
    Custom – Gathering layer); icon is a named game symbol (sources/icons.py)
    drawn instead of the coloured dot. Both empty for a plain player pin."""
    data = load()
    pin = {"id": uuid.uuid4().hex[:8], "x": x, "y": y,
           "label": (label or "").strip()[:80],
           "color": color or DEFAULT_COLOR,
           "kind": (kind or "").strip().lower()[:24],
           "icon": (icon or "").strip().lower()[:32]}
    data.setdefault(zone, []).append(pin)
    _save(data)
    return pin


def update(zone: str, pin_id: str, label: str | None = None,
           color: str | None = None) -> dict | None:
    """Change a pin's label and/or colour. Returns the updated pin, or None."""
    data = load()
    for pin in data.get(zone, []):
        if pin.get("id") == pin_id:
            if label is not None:
                pin["label"] = label.strip()[:80]
            if color is not None:
                pin["color"] = color or DEFAULT_COLOR
            _save(data)
            return pin
    return None


def remove(zone: str, pin_id: str) -> bool:
    data = load()
    pins = data.get(zone, [])
    keep = [p for p in pins if p.get("id") != pin_id]
    if len(keep) == len(pins):
        return False
    data[zone] = keep
    _save(data)
    return True
