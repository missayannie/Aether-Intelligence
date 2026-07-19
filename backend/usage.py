"""Cross-app API usage ledger.

Every agent turn — chat replies, doc-thread edits, follow-up suggestions —
appends one line here with WHERE it ran (context), WHAT ran it (model + auth)
and what it cost. The sidebar's "API usage" meter reads the aggregate.

Costs are real token counts × the provider's per-token price wherever the
engine reports usage; a line is flagged `estimated` when it had to fall back
to a chars/4 guess. Subscription turns cost the player $0 — their would-have-
cost is tracked separately as `covered`.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from paths import DATA_DIR

USAGE_PATH = DATA_DIR / "data" / "usage.jsonl"
_lock = threading.Lock()


def record(*, context: str, model: str, auth: str,
           input_tokens: int = 0, output_tokens: int = 0,
           cost_usd: float = 0.0, estimated: bool = False) -> None:
    """Append one usage line. Never raises — a bookkeeping failure must not
    break the turn that produced it."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "context": context,          # chat | doc | suggestions
        "model": model,
        "auth": auth,                # api | subscription
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cost_usd": round(float(cost_usd or 0.0), 6),
        "estimated": bool(estimated),
    }
    try:
        with _lock:
            USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with USAGE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def summary() -> dict:
    """Aggregate the ledger for the UI.

    `billed_usd` is real money (API-key turns). `covered_usd` is what the
    subscription turns would have cost at API prices. `rows` break both down
    by (model, auth, context) so the meter can show which agent spent what
    where.
    """
    rows: dict[tuple, dict] = {}
    billed = covered = 0.0
    any_estimated = False
    try:
        with USAGE_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (r.get("model", "?"), r.get("auth", "api"),
                       r.get("context", "chat"))
                row = rows.setdefault(key, {
                    "model": key[0], "auth": key[1], "context": key[2],
                    "turns": 0, "input_tokens": 0, "output_tokens": 0,
                    "cost_usd": 0.0,
                })
                row["turns"] += 1
                row["input_tokens"] += r.get("input_tokens", 0)
                row["output_tokens"] += r.get("output_tokens", 0)
                row["cost_usd"] += r.get("cost_usd", 0.0)
                if r.get("estimated"):
                    any_estimated = True
                if r.get("auth") == "subscription":
                    covered += r.get("cost_usd", 0.0)
                else:
                    billed += r.get("cost_usd", 0.0)
    except FileNotFoundError:
        pass
    out_rows = sorted(rows.values(), key=lambda r: -r["cost_usd"])
    for r in out_rows:
        r["cost_usd"] = round(r["cost_usd"], 4)
    return {
        "billed_usd": round(billed, 4),
        "covered_usd": round(covered, 4),
        "estimated": any_estimated,
        "rows": out_rows,
    }
