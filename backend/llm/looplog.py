"""Per-turn log of the agentic loop — the flight recorder.

"Why did that answer take 14 tool calls?" was unanswerable: the loop's
rounds, tool calls and failures lived only in the UI's transient chips.
Every event now lands in DATA_DIR/logs/agent-YYYYMMDD.jsonl, one JSON
object per line:

    {"t": …, "chat": "…", "engine": "api|subscription", "ev": "tool",
     "name": "pin_on_map", "args": {…}, …}

Args are truncated hard — this is a trace of WHAT ran, not a data store.
Failures to write never break a chat turn.
"""
from __future__ import annotations

import json
import threading
import time

from paths import DATA_DIR

_DIR = DATA_DIR / "logs"
_LOCK = threading.Lock()


def _short(v, limit: int = 200):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def log(chat_id: str, engine: str, ev: str, **fields) -> None:
    rec = {"t": round(time.time(), 3), "chat": chat_id or "?", "engine": engine,
           "ev": ev}
    for k, v in fields.items():
        rec[k] = _short(v) if k in ("args", "detail") else v
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        path = _DIR / f"agent-{time.strftime('%Y%m%d')}.jsonl"
        with _LOCK, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
