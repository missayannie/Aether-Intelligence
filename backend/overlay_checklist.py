"""The overlay's guide checklist (docs/overlay-spec.md, concept 2).

One doc at a time is "pinned to the overlay". Its checkbox steps are read out
for the in-game widget, and ticking a step there writes straight back into the
doc — the same file the app's editor shows, so the two never diverge.

Checkboxes exist in two forms in these docs, and the app's renderer counts
BOTH in document order: GFM task lines (`- [ ] step`) and raw
`<input type="checkbox">` inside table cells (guide tables, where GFM task
syntax isn't defined). Indices here must match that order exactly or ticking
the third box in game would flip a different row in the app.
"""
from __future__ import annotations

import json
import re
import threading

from paths import DATA_DIR

_FILE = DATA_DIR / "overlay_checklist.json"
_LOCK = threading.Lock()

# One pattern, both forms, so a single scan yields the renderer's ordering.
_BOX = re.compile(
    r"^([ \t]*[-*+][ \t]+)\[( |x|X)\][ \t]*(?P<gfm>.*)$"
    r"|<input type=\"checkbox\"(?P<checked> checked)?[^>]*>",
    re.M,
)


def pinned() -> dict:
    try:
        v = json.loads(_FILE.read_text(encoding="utf-8-sig"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def pin(chat_id: str, doc_id: str) -> dict:
    with _LOCK:
        data = {"chat_id": chat_id, "doc_id": doc_id}
        _FILE.write_text(json.dumps(data), encoding="utf-8")
    return data


def unpin() -> None:
    with _LOCK:
        try:
            _FILE.unlink()
        except OSError:
            pass


def _plain(s: str) -> str:
    """Strip the markup a label shouldn't show: tags, links, emphasis."""
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)   # [text](url)
    s = re.sub(r"[*_`]{1,3}", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _row_label(md: str, pos: int) -> str:
    """For a table-cell checkbox, the step reads as the rest of its row —
    a guide row like "Field | (X: 18.9, Y: 11.6) | Next to Cid's Airship" is
    only useful with the location, not just its type."""
    start = md.rfind("\n", 0, pos) + 1
    end = md.find("\n", pos)
    line = md[start:end if end != -1 else len(md)]
    cells = [_plain(c) for c in line.split("|")]
    keep = [c for c in cells if c and not set(c) <= {"-", ":", " "}]
    return " · ".join(keep[:3]) or "Step"


def steps(md: str) -> list[dict]:
    """Every checkbox in the doc, in the order the app renders them."""
    out: list[dict] = []
    for i, m in enumerate(_BOX.finditer(md)):
        if m.group("gfm") is not None:
            text = _plain(m.group("gfm"))
            done = m.group(2).lower() == "x"
        else:
            text = _row_label(md, m.start())
            done = bool(m.group("checked"))
        out.append({"index": i, "text": text[:120] or "Step", "done": done})
    return out


def toggle(md: str, index: int) -> str:
    """Flip the nth checkbox — the app's toggleTask, server-side."""
    i = -1

    def sub(m: re.Match) -> str:
        nonlocal i
        i += 1
        if i != index:
            return m.group(0)
        if m.group("gfm") is not None:
            state = "[x]" if m.group(2) == " " else "[ ]"
            return f"{m.group(1)}{state} {m.group('gfm')}".rstrip()
        return ('<input type="checkbox">' if m.group("checked")
                else '<input type="checkbox" checked>')

    return _BOX.sub(sub, md)
