"""Hallucination regression suite.

Runs canned fact questions through the REAL agent pipeline (a running backend)
and greps the answers for canonical facts. Every question here is one a model
plausibly answers wrong from memory — most are past real failures. Run it after
ANY prompt or tool change, ideally on the weakest model players use:

    python tools/eval_facts.py                     # backend :8756, claude-haiku-4-5
    FFXIV_BACKEND=http://127.0.0.1:8799 python tools/eval_facts.py
    EVAL_MODEL=claude-opus-4-8 python tools/eval_facts.py

Exit code 0 = all green. Each case costs one agent turn.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("FFXIV_BACKEND", "http://127.0.0.1:8756")
MODEL = os.environ.get("EVAL_MODEL", "claude-haiku-4-5")
AUTH = os.environ.get("EVAL_AUTH", "subscription")

# (question, [regexes that MUST match answer], [regexes that MUST NOT match],
#  expected map-event pin count or None)
CASES = [
    # The real Scouting/Aiming failure (2026-07-18).
    ("Do I get the scouting coffer or aiming coffer for ninja?",
     [r"[Ss]couting"], [r"^.{0,120}[Aa]iming coffer"], None),
    ("Which jobs wear Aiming gear?",
     [r"[Bb]ard|BRD", r"[Mm]achinist|MCH", r"[Dd]ancer|DNC"], [r"[Nn]inja|NIN"], None),
    # The real Garlemald failure (2026-07-18): region-prefixed phrasing.
    ("Can you mark all the aether currents in Ilsabard Garlemald?",
     [], [r"unable to|couldn't find|cannot find"], 4),
    # Fabricated-quest-name class (the old "Paint It Pink" incident).
    ("Where do I find Cordia Sap?",
     [r"Xobr'?it Cinderfield", r"Yak T'el"], [], None),
    # Job-role table sanity from the other direction.
    ("What gear role does Viper share with Ninja?",
     [r"[Ss]couting"], [r"[Aa]iming|[Ss]triking|[Mm]aiming"], None),
    # The real Loporrits failure (2026-07-18): colloquial place + player typo.
    # Must resolve "the moon"/"lopporits" -> Mare Lamentorum and pin, not ask.
    ("Can you pin the aether currents on the moon, where the lopporits live?",
     [r"Mare Lamentorum"], [r"which zone|zone name.{0,30}\?"], 4),
    # The real Sanctuary Carapace failures (2026-07-19, twice): the animal list
    # lives in the wiki item page's Acquisition BULLET LISTS (Common/Bonus) —
    # the answer must name actual animals, not shrug at the page.
    ("What animal do I get sanctuary carapace from?",
     [r"Yellow Coblyn|Adamantoise|Beachcomb|Morbol Seedling|Glyptodon Pup"],
     [r"doesn'?t name|couldn'?t (verify|find|confirm)"], None),
]


def ask(question: str) -> tuple[str, list[dict]]:
    req = urllib.request.Request(f"{BASE}/chats", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    chat_id = json.loads(urllib.request.urlopen(req, timeout=30).read())["id"]
    body = json.dumps({"chat_id": chat_id, "message": question,
                       "model": MODEL, "auth": AUTH}).encode()
    req = urllib.request.Request(f"{BASE}/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    text, events = [], []
    with urllib.request.urlopen(req, timeout=420) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            events.append(d)
            if d.get("type") == "token":
                text.append(d.get("text", ""))
    return "".join(text), events


def main() -> int:
    print(f"eval: {len(CASES)} cases · model={MODEL} · backend={BASE}\n")
    failures = 0
    for question, must, must_not, pins in CASES:
        try:
            answer, events = ask(question)
        except Exception as e:
            print(f"✗ ERROR  {question}\n         {e}")
            failures += 1
            continue
        problems = []
        for rx in must:
            if not re.search(rx, answer):
                problems.append(f"missing /{rx}/")
        for rx in must_not:
            if re.search(rx, answer):
                problems.append(f"forbidden /{rx}/ matched")
        if pins is not None:
            got = max((len(e.get("pins") or []) for e in events
                       if e.get("type") == "map"), default=0)
            if got != pins:
                problems.append(f"expected {pins} map pins, got {got}")
        if problems:
            failures += 1
            print(f"✗ FAIL   {question}")
            for p in problems:
                print(f"         {p}")
            print(f"         answer: {answer[:160]!r}")
        else:
            print(f"✓ pass   {question}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
