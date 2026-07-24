"""Canonical tool definitions + executor.

Tools are defined ONCE here in OpenAI function-calling schema. litellm translates
this schema to each provider's native tool format, so Claude, GPT, Gemini, and
Grok all call the same tools without per-provider tool code.

`execute_tool` runs a tool call against the real source clients and returns a
JSON-serializable result the model reads back.
"""
from __future__ import annotations

import json
import re

from sources.wiki import WikiClient
from sources.universalis import UniversalisClient
from sources.maps import MapClient
from sources.realmremapped import RealmRemappedClient
from sources.lodestone import LodestoneClient
from sources.sqex_forum import SqexForumClient
from sources import garland as _garland
from sources.lodestone_http import LodestoneBlocked
from config import WIKIS

# Shared clients (cheap to keep open; closed on shutdown).
_wiki = WikiClient()
_universalis = UniversalisClient()
_maps = MapClient()
_realm = RealmRemappedClient()
_lodestone = LodestoneClient()
_forum = SqexForumClient()


def _slim(v):
    """Drop empty leaves (None / "" / [] / {}) from a result, recursively.

    Booleans and numbers always survive — found=False and tradeable=False are
    answers, not noise. UI-facing keys (source, url, asset_id, map_url, focus,
    pin, doc_id, error…) pass through whenever they're set; the event layer in
    client.py / agent_engine only reads keys that are present, so a dropped
    empty never breaks it."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            x = _slim(x)
            if x is None or (isinstance(x, (str, list, dict)) and not x):
                continue
            out[k] = x
        return out
    if isinstance(v, list):
        return [_slim(x) for x in v]
    return v


def _dump(payload: dict) -> str:
    """Serialize a tool result for the model: empties dropped, compact JSON.

    Every result is re-sent as input on EVERY later round of the agentic loop,
    so each wasted key and each pretty-print space is paid for many times over."""
    return json.dumps(_slim(payload), separators=(",", ":"))


def _cap(items: list, n: int) -> list:
    """Cap a long list with a '+N more' marker instead of dumping it whole —
    the model relays the note; it never needed entry 30 of a vendor list."""
    if len(items) <= n:
        return items
    return items[:n] + [f"+{len(items) - n} more"]


def _wiki_fallback(query: str, extra: dict) -> str | None:
    """One consolegames search when Garland comes up empty — done SERVER-SIDE so
    the model gets both sources for the price of one round trip. The source/url
    fields are the wiki's, so the citation UI stays accurate about where the
    answer really came from. Returns None when the wiki is empty too."""
    try:
        w = _wiki.lookup("consolegames", query)
    except Exception:
        w = None
    if not w:
        return None
    return _dump({
        "found": True, "source": w.source, "title": w.title, "url": w.url,
        "extract": w.extract, "details": w.details, "tables": w.tables,
        "image_url": w.image_url,
        "fallback_note": ("Garland Tools had no record, so the wiki was searched "
                          "automatically — this already IS the second source. Don't "
                          "re-verify the same fact elsewhere unless check_patch_notes "
                          "says the current patch touched it."),
        **extra,
    })

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the player a short clarifying question when their request is "
                "ambiguous or you need them to choose before you can proceed well "
                "(e.g. which job, which data center, casual vs. optimized). Prefer "
                "this over guessing. Give 2-4 concise options; the player can also "
                "type their own answer. Returns the player's answer as a string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 short suggested answers the player can click.",
                    },
                    "header": {"type": "string", "description": "Very short topic label (<=14 chars), e.g. 'Job' or 'Data center'."},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_character",
            "description": (
                "Bind an FFXIV character to THIS chat's profile workspace from the "
                "Lodestone, auto-filling the player's identity (jobs, world, role). "
                "Pass a Lodestone character URL or id, OR a character name (+ optional "
                "home world). If several characters match a name, this returns "
                "candidates — show them and ask which one, then call again with the id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Lodestone URL, character id, or character name."},
                    "world": {"type": "string", "description": "Home world (helps disambiguate a name)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_doc",
            "description": (
                "Save a substantial piece of content (a guide, build, rotation, "
                "checklist, plan) as a DRAFT reference document instead of pasting it "
                "all into the chat. Provide a title and the full markdown content. "
                "The player sees a small 'Review' link in the chat that opens the doc; "
                "after calling this, keep your chat reply to a 1-2 sentence summary. "
                "Use markdown checkboxes ('- [ ] item') for any checklist so the player "
                "can tick them off. Only for meaty content — answer short questions normally."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title for the doc."},
                    "content": {"type": "string", "description": "Full document body in markdown."},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": (
                "Search the FFXIV Console Games Wiki and return the top page's "
                "summary — fight mechanics, raids, dungeons, quests, drop data, "
                "lore. wiki is always 'consolegames'. For item facts prefer "
                "lookup_item; for NPC locations prefer find_npc. Both of those "
                "already fall back to this wiki BY THEMSELVES when Garland comes "
                "up empty — never repeat their query here to double-check an "
                "answer you already have."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wiki": {"type": "string", "enum": list(WIKIS.keys())},
                    "query": {"type": "string", "description": "What to look up."},
                },
                "required": ["wiki", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_item",
            "description": (
                "Look up an item in this app's item database (the player's own "
                "installed game client when available, community data otherwise; "
                "the result's `source` says which — cite THAT). Returns "
                "item level, the jobs that can equip it, stats, materia slots, the "
                "in-game description, an icon image_url, its page url, HOW YOU GET "
                "ONE — gathering_nodes (each has the node `name` AND the `zone` it "
                "sits in; the zone is what the player needs), vendors, "
                "retainer_ventures, ingredient_of, sell_price_gil, "
                "tradeable — AND its "
                "progression chain — upgrades_to / downgrades_from, which is the "
                "direct answer to 'what replaces this?'. Prefer this over a wiki for "
                "any item fact — for a plain what/where/how-much question this alone "
                "is the answer, so don't chase a second source. If Garland has no "
                "record, the wiki is searched AUTOMATICALLY and the result says "
                "which source answered — one call covers both, so never re-run the "
                "same fact through search_wiki. Only when the fact is "
                "load-bearing in a recommendation (an ilvl you're telling them to "
                "chase) is check_patch_notes worth the round trip."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Item name to look up."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_price",
            "description": "Get current market-board listings for an item by name, on a world or data center (default Aether).",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "world_or_dc": {"type": "string", "description": "World or DC name, e.g. 'Aether', 'Gilgamesh'."},
                },
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "whats_new",
            "description": (
                "Get recent official FFXIV news from the Lodestone — patch notes, "
                "maintenance, events, updates, and status. Use for 'what's new', "
                "'any maintenance', 'latest patch', or catching the player up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many recent items (default 12)."},
                    "include_community": {"type": "boolean", "description": "Also include community discussion threads from the official forum."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_doc",
            "description": (
                "Rewrite the document currently open in the editor WHOLESALE. "
                "Reach for this only when RESTRUCTURING most of the doc — for a "
                "targeted change (a row, a cell, a heading, a sentence) use "
                "edit_doc instead, which doesn't resend every part you're keeping. "
                "Only available while the player is chatting from inside a doc. "
                "Pass the COMPLETE new markdown — it replaces the file wholesale, "
                "so include every part you are keeping. Make only the change they "
                "asked for and preserve everything else exactly: headings, table "
                "rows, checkbox states (`<input type=\"checkbox\" checked>` stays "
                "checked), `![](asset:…)` images, links, and the Sources section. "
                "After calling it, tell them in one or two sentences what you "
                "changed — that sentence is your reply in the thread."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The full revised document, in markdown.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_doc",
            "description": (
                "Make a TARGETED change to the document currently open in the "
                "editor: replace one exact snippet with new text, leaving the rest "
                "of the doc byte-for-byte untouched. Only available while the "
                "player is chatting from inside a doc. old_text must match the "
                "saved markdown EXACTLY (whitespace included) and pin down one "
                "spot — a missing or ambiguous match returns an error instead of "
                "guessing; retry with a longer snippet, or pass occurrence. Prefer "
                "this over update_doc for anything short of a restructure. After "
                "calling it, tell them in one or two sentences what you changed — "
                "that sentence is your reply in the thread."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace, verbatim from the doc.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement (empty string deletes the snippet).",
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": "1-based match to replace when old_text appears more than once.",
                    },
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": (
                "Read ONE of the saved reference docs in this chat, by the id from "
                "the saved-docs list in the system context. The list carries titles "
                "only — pull a doc's content when it's actually relevant to the "
                "question, never all of them 'just in case'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "The doc id from the saved-docs list.",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_gear",
            "description": (
                "Save a job's gear set into the player's profile, so it's known from "
                "now on. The Lodestone only ever shows the job they logged out "
                "wearing, so this is the ONLY way to learn about any other job. Use "
                "it after reading an in-game character-window SCREENSHOT they sent "
                "(source='screenshot'), or when they simply tell you their set "
                "(source='player'). ALWAYS read the gear back to them and get a "
                "confirmation before saving — a misread item name becomes a wrong "
                "fact you'll repeat for weeks. Never guess pieces you can't clearly "
                "see; record only what you can actually read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job": {"type": "string", "description": "The job this set is worn on, e.g. 'Ninja'."},
                    "source": {
                        "type": "string",
                        "enum": ["screenshot", "player"],
                        "description": "Where the data came from. Never claim 'lodestone' here.",
                    },
                    "pieces": {
                        "type": "array",
                        "description": "One entry per equipped piece you can actually read.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "slot": {"type": "string", "description": "Head, Body, Hands, Legs, Feet, Ring…"},
                                "name": {"type": "string"},
                                "item_level": {"type": "integer", "description": "0 if not visible."},
                            },
                            "required": ["slot", "name"],
                        },
                    },
                },
                "required": ["job", "source", "pieces"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_links",
            "description": (
                "Get this app's database URLs for a BATCH of named things at once "
                "— items/gear, dungeons & trials (kind 'instance'), NPCs, quests, "
                "achievements, mobs, FATEs, gathering nodes. Use it when you name "
                "in-game content so each name is a clickable link the app opens in "
                "its Database tab. Pass EVERY name in ONE call — never one call per "
                "row. Names not found come back in 'missing'; leave those as plain "
                "text and never invent a url."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entries": {
                        "type": "array",
                        "description": "Every name you want to link, with its kind.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Exact in-game name."},
                                "kind": {
                                    "type": "string",
                                    "enum": ["item", "instance", "npc", "quest", "achievement", "mob", "fate", "node", "leve"],
                                    "description": "'instance' = dungeon/trial/raid. 'item' = gear/materia/mats.",
                                },
                            },
                            "required": ["name", "kind"],
                        },
                    },
                },
                "required": ["entries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_preference",
            "description": (
                "Remember a standing BEHAVIOUR preference the player just asked for "
                "('always …', 'never …', 'from now on …'). It is appended to a "
                "preferences file read into every future chat, so the change sticks "
                "across conversations. Behaviour only — character facts (gear, jobs) "
                "belong in the profile, not here. Phrase it as one short instruction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preference": {"type": "string",
                                   "description": "One imperative sentence, e.g. 'Always show prices in gil.'"},
                    "reason": {"type": "string",
                               "description": "Optional: what the player said that prompted this."},
                },
                "required": ["preference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_npc",
            "description": (
                "Where an NPC stands: every location with EXACT in-game flag "
                "coordinates, plus the quests they give at each spot — straight from "
                "the app's database, no text parsing. THE tool for 'where is <NPC>' and "
                "for pinning a quest giver: find the quest's NPC, call this, pick "
                "the location whose quests match, then pin_on_map with its zone and "
                "x/y. An NPC can stand in several zones, so check the quest list "
                "rather than assuming the first entry. If Garland doesn't know the "
                "NPC, the wiki is searched AUTOMATICALLY and the result says so — "
                "read the zone and (x, y) from its `details`; don't re-run the same "
                "name through search_wiki."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The NPC's exact name."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_patch_notes",
            "description": (
                "Check whether the CURRENT patch changed a given topic, reading the "
                "official patch notes. Use this to decide if a fact needs "
                "re-verifying: if this patch didn't touch the thing you're talking "
                "about, one good source is enough and you can answer. If it DID, "
                "treat older/community info as suspect and confirm against an "
                "official source. Cheap and cached — prefer it over a speculative "
                "second lookup. A result with mentioned=false is a real, useful "
                "answer (the patch left this alone), not a failed search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "The thing to check, as it would appear in patch notes — "
                            "a job ('Ninja'), duty, item, or system ('Triple Triad'). "
                            "Omit to get a summary of what's in this patch."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_history",
            "description": (
                "Trace how a job/ability/item/system CHANGED over time. For an "
                "ABILITY it reads the wiki action page's full patch-by-patch history "
                "— which captures the reworks the official patch notes never name "
                "(e.g. Huton losing its weaponskill/auto-attack-speed buff in 7.0); "
                "for anything else it sweeps the official patch-notes archive. Use it "
                "whenever the player asks how or when something changed, what it USED "
                "TO do, why it differs from what they remember, or its history — "
                "every OTHER source (the item database, a normal wiki lookup, the "
                "game client) reflects only the CURRENT patch and will otherwise "
                "assume today's values were always true. Returns the past patches "
                "that changed the topic, newest first, with what changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "The exact in-game name to trace — an ability ('Huton'), "
                            "a job ('Ninja'), an item, or a system, spelled as it "
                            "appears in patch notes."
                        ),
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_forum",
            "description": (
                "Search the OFFICIAL Square Enix FFXIV forum for a topic and get "
                "matching community threads (title + url). Players share a lot of "
                "extra info there — strategies, tips, opinions, fixes. Follow up with "
                "read_forum_thread on a thread url to read the actual posts/comments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to search the official forum for."},
                    "limit": {"type": "integer", "description": "Max threads to return (default 6)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_forum_thread",
            "description": (
                "Read the posts (player comments/discussion) from an OFFICIAL forum "
                "thread url — e.g. one returned by search_forum or whats_new. Use it "
                "to pull the community's additional info, tips, and context. Credit "
                "the source as the official FFXIV forum."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The forum thread url to read."},
                    "limit": {"type": "integer", "description": "Max posts to read (default 8)."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_zone_map",
            "description": (
                "Open the interactive A Realm Remapped map for a zone in the app's "
                "Map tab. Use when the player wants to browse/see a zone's map, or "
                "for gathering nodes, FATEs, hunt marks, aether currents, or "
                "treasure locations — that community map shows them all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string", "description": "Zone name, e.g. 'Labyrinthos', 'Middle La Noscea'."},
                },
                "required": ["zone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin_on_map",
            "description": (
                "Place an exact pin on the LABELED in-game zone map (A Realm Remapped's "
                "parchment map with place names) at in-game coordinates. Use for 'where "
                "is X' questions. Look up the place name and the X/Y flag coordinates "
                "first (e.g. from the wiki), then call this — the pin is placed by exact "
                "math, not guessed. Pass the ZONE name as 'place' (e.g. 'Labyrinthos')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "Zone/map name, e.g. 'Limsa Lominsa Lower Decks'."},
                    "x": {"type": "number", "description": "In-game X coordinate (the flag value)."},
                    "y": {"type": "number", "description": "In-game Y coordinate (the flag value)."},
                    "label": {"type": "string", "description": "Short label for the pin, e.g. 'Aetheryte'."},
                    "icon": {"type": "string", "description": (
                        "Optional icon NAME matching what's marked (mining, logging, "
                        "fishing, fate, mob, quest, aetheryte, dungeon, shop, flag…) — "
                        "the pin then shows the game's own map symbol.")},
                    "area_radius": {"type": "number", "description": (
                        "Optional: mark an AREA, not a point — a translucent dashed "
                        "circle of this radius in map coordinates (1.0 = one grid "
                        "square). Use for mob spawn zones, FATE areas, node clusters, "
                        "fishing holes.")},
                },
                "required": ["place", "x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_image",
            "description": (
                "Show the player a picture by saving it into the chat's assets, "
                "where it appears in the Assets tab. Pass an image url that ANOTHER "
                "tool returned — e.g. search_wiki's `image_url` (an NPC portrait or "
                "item render). Use it to display an NPC's portrait alongside a map "
                "pin, or to show what an item looks like. Never invent an image url; "
                "only pass one a tool actually returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Direct image url from a tool result (e.g. search_wiki image_url)."},
                    "label": {"type": "string", "description": "Short caption, e.g. the NPC or item name."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_zone",
            "description": (
                "Resolve/confirm a ZONE name against the app's map index — "
                "instant and local. Use it to verify your own canonical guess "
                "when the player describes a place colloquially ('the moon' -> "
                "guess 'Mare Lamentorum', confirm here) or types an approximate "
                "name. Returns matching drawable zones with their regions. "
                "Confirm BEFORE asking the player what zone they mean."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Zone name or your best canonical guess."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin_points_on_map",
            "description": (
                "Pin a whole CATEGORY of points on ONE zone's interactive map at "
                "once — every aether current, every vista, every hunt spawn. Look "
                "the coordinates up first (wiki tables, database entries); NEVER "
                "invent them, and say so if you can only find some. All points "
                "show as TEMPORARY typed pins; the map offers a Save button that "
                "keeps the whole set permanently as 'Custom – <category>'. Paste "
                "the returned `link` in your reply so the set is re-openable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "Zone/map name, e.g. \"Yak T'el\"."},
                    "category": {"type": "string", "description": "Plural Title Case set name, e.g. 'Aether Currents', 'Vistas'."},
                    "icon": {"type": "string", "description": "Named game symbol matching the category (aethernet for aether currents, mining, fishing, fate, mob, quest, flag…)."},
                    "preset": {"type": "string", "description": (
                        "'aether_currents': coordinates come EXACTLY from the "
                        "installed game client — omit points entirely. If the "
                        "result says the client isn't available, research the "
                        "wiki and call again with points.")},
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number", "description": "In-game X flag coordinate."},
                                "y": {"type": "number", "description": "In-game Y flag coordinate."},
                                "label": {"type": "string", "description": "Short label, e.g. '1' or 'NE ledge'. Optional."},
                            },
                            "required": ["x", "y"],
                        },
                        "description": "Up to 40 points, all in THIS zone. Omit when using a preset.",
                    },
                },
                "required": ["place", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "annotate_image",
            "description": (
                "Draw fight-guide callouts on an image already in the chat's assets. "
                "Provide annotations with relative (0..1) coords: markers (numbered "
                "sequence), circles (safe/danger zones), arrows, and labels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "Id of a source image in this chat's assets."},
                    "title": {"type": "string"},
                    "annotations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["marker", "circle", "arrow", "label"]},
                                "x": {"type": "number"}, "y": {"type": "number"},
                                "x2": {"type": "number"}, "y2": {"type": "number"},
                                "radius": {"type": "number"},
                                "text": {"type": "string"},
                                "color": {"type": "string", "enum": ["safe", "danger", "marker", "note", "boss"]},
                            },
                            "required": ["kind", "x", "y"],
                        },
                    },
                },
                "required": ["asset_id", "annotations"],
            },
        },
    },
]


# Hard per-question ceiling on search-family calls. The prompt already says
# "stop searching after 3-4 attempts", but small models ignore it — GPT-5.4
# mini once ran ~50 wiki searches in one question. ctx is a fresh dict per
# /chat request, so the counter naturally scopes to one user turn.
_SEARCH_TOOLS = {"search_wiki", "search_forum"}
_SEARCH_BUDGET = 12


def execute_tool(name: str, args: dict, ctx: dict | None = None) -> str:
    """Run a tool call. Returns a JSON string the model reads as the tool result.
    `ctx` carries per-chat state (e.g. the assets dir) for tools that need it."""
    import time as _time
    _t0 = _time.time()
    if name in _SEARCH_TOOLS and ctx is not None:
        n = ctx["_searches"] = ctx.get("_searches", 0) + 1
        if n > _SEARCH_BUDGET:
            result = _dump({
                "error": "search budget exhausted",
                "note": (f"You have already run {n - 1} searches for this "
                         "question. STOP searching — further searches will "
                         "also be refused. Either ask_user ONE narrowing "
                         "question (only if the player's answer would change "
                         "where to look), or answer NOW from the results you "
                         "already have, briefly noting anything you could "
                         "not confirm."),
            })
        else:
            result = _execute_tool(name, args, ctx)
    else:
        result = _execute_tool(name, args, ctx)
    # The flight recorder: ONE log point covers both engines (api + subscription
    # both come through here). See llm/looplog.py.
    try:
        from llm import looplog
        parsed = json.loads(result) if result and result[0] == "{" else {}
        looplog.log((ctx or {}).get("chat_id", ""),
                    (ctx or {}).get("engine", "?"), "tool",
                    name=name, args=args,
                    ok=not (isinstance(parsed, dict) and parsed.get("error")),
                    ms=int((_time.time() - _t0) * 1000))
    except Exception:
        pass
    return result


def _execute_tool(name: str, args: dict, ctx: dict | None = None) -> str:
    try:
        if name == "search_wiki":
            res = _wiki.lookup(args["wiki"], args["query"])
            if not res:
                return _dump({"found": False})
            return _dump({
                "found": True, "source": res.source, "title": res.title,
                "url": res.url, "extract": res.extract, "details": res.details,
                # The page's data tables — often THE answer (drop tables,
                # husbandry grids). _slim drops the key when a page has none.
                "tables": res.tables,
                "image_url": res.image_url,
            })

        if name == "update_doc":
            handler = (ctx or {}).get("update_doc")
            if not handler:
                return _dump({
                    "ok": False,
                    "note": ("No document is open. update_doc only works when the "
                             "player is chatting from inside a doc."),
                })
            return _dump(handler(args))

        if name == "edit_doc":
            # Same plumbing as update_doc: the diff-apply itself lives in the
            # doc-thread endpoint's closure, because only it holds the open doc.
            handler = (ctx or {}).get("edit_doc")
            if not handler:
                return _dump({
                    "ok": False,
                    "note": ("No document is open. edit_doc only works when the "
                             "player is chatting from inside a doc."),
                })
            return _dump(handler(args))

        if name == "read_doc":
            # ctx-bound like create_doc: only the chat endpoint knows this chat's docs.
            handler = (ctx or {}).get("read_doc")
            if not handler:
                return _dump({"ok": False, "note": "No saved docs are available in this context."})
            return _dump(handler(args))

        if name == "record_gear":
            # Needs the chat's owning profile, which only the endpoint knows.
            handler = (ctx or {}).get("record_gear")
            if not handler:
                return _dump({"ok": False, "note": "Gear can't be recorded here."})
            return _dump(handler(args))

        if name == "db_links":
            entries = args.get("entries") or []
            links: dict[str, str] = {}
            missing: list[str] = []
            for e in entries[:40]:            # a table that big is already too big
                nm = (e.get("name") or "").strip()
                kind = (e.get("kind") or "item").strip()
                if not nm:
                    continue
                try:
                    hit = _garland.find(nm, kind=kind)
                except Exception:
                    hit = None
                if hit:
                    links[nm] = hit.url
                else:
                    missing.append(nm)
            return _dump({"links": links, "missing": missing, "source": _garland.SOURCE})

        if name == "check_patch_notes":
            from sources import gameversion, patchnotes
            patch = gameversion.patch_name()
            if not patch:
                return _dump({
                    "found": False,
                    "note": ("Could not determine the current game version, so patch "
                             "impact is unknown — verify version-specific claims the "
                             "normal way."),
                })
            topic = (args.get("topic") or "").strip()
            if not topic:
                return _dump(patchnotes.summary(patch))
            return _dump(patchnotes.search(patch, topic))

        if name == "patch_history":
            from sources import patchnotes
            topic = (args.get("topic") or "").strip()
            if not topic:
                return _dump({"found": False, "note": "topic is required"})
            # An ABILITY's wiki action page carries a structured {{patch}} timeline
            # that captures the reworks the patch notes never name (Huton's
            # attack-speed buff removal, etc.) — try that first, cheaply.
            wiki_hist = _wiki.ability_history(topic)
            if wiki_hist.get("found"):
                return _dump(wiki_hist)
            # Not an action page (item, system, or lore namesake) -> sweep the
            # official patch-notes archive instead.
            return _dump(patchnotes.history(topic))

        if name == "lookup_item":
            res = _garland.lookup(args["name"])
            if not res:
                return _wiki_fallback(args["name"], {}) or _dump({
                    "found": False,
                    "note": ("Not in Garland Tools, and no wiki page either — try a "
                             "different spelling before telling the player it "
                             "doesn't exist."),
                })
            # Items whose record shows NO acquisition path (pasture leavings, mob
            # drops, coffer-only cosmetics — the databases simply don't carry it):
            # the wiki item page's Acquisition section usually does, so fetch it
            # here instead of leaving the model to say "it doesn't name which"
            # and stop. (Real failure: Sanctuary Carapace, whose Common/Bonus
            # animal lists live only on the wiki page.)
            wiki_acq = None
            if not (res.nodes or res.vendors or res.ventures):
                w = _wiki.lookup("consolegames", res.name + " acquisition")
                if w and (w.tables or w.extract):
                    wiki_acq = {
                        "source": w.source, "page": w.title, "url": w.url,
                        "extract": w.extract, "sections": w.tables,
                        "note": ("How to obtain, from the wiki — the item databases "
                                 "have no acquisition path for this item. Only trust "
                                 "it if `page` is actually about this item."),
                    }
            return _dump({
                **({"wiki_acquisition": wiki_acq} if wiki_acq else {}),
                "found": True, "source": res.source, "name": res.name, "url": res.url,
                "item_level": res.item_level, "jobs": res.jobs, "patch": res.patch,
                "description": res.description, "details": res.details,
                "image_url": res.icon,
                "attributes": res.attributes, "materia_slots": res.sockets,
                # The progression chain. This is the direct answer to "what replaces
                # this?" — the official database had no equivalent field. The model
                # links by NAME (db_links), so the internal ids stay out of the result.
                "upgrades_to": [{"name": u.get("name"), "item_level": u.get("item_level")}
                                for u in res.upgrades],
                "downgrades_from": [{"name": d.get("name"), "item_level": d.get("item_level")}
                                    for d in res.downgrades],
                # "Sources & Uses" — HOW YOU GET ONE. Garland has always returned this;
                # it just wasn't being passed on, so the model would say "no node
                # location is available" while the Database tab displayed the node
                # right next to it. If it's in the record, the model gets to see it.
                # `name` is the NODE, `zone` is the map, and x/y are REAL in-game
                # coords straight from Garland — so there is never cause to say "no
                # coordinates exist" or to invent one. spawn_times are ET hours.
                "gathering_nodes": [{k: v for k, v in n.items() if k != "id"}
                                    for n in _garland.nodes_detail(res.nodes)],
                # Vendor names only, capped — a piece of tome gear can list dozens of
                # vendors, and the model only ever says "sold by X" or links by name.
                "vendors": _cap([v.get("name") for v in res.vendors if v.get("name")], 8),
                # The venture ids are internal keys; the count says all the model
                # needs ("obtainable via retainer ventures").
                "retainer_ventures": len(res.ventures),
                "ingredient_of": _cap([{"name": i.get("name"), "qty": i.get("qty")}
                                       for i in res.ingredient_of], 10),
                "sell_price_gil": res.sell_price,
                "tradeable": res.tradeable,
                "source_note": (
                    "Cite the `source` field above verbatim — do NOT attribute this to "
                    "a database brand it didn't come from. Community data (not the "
                    "game client) can lag a patch: for anything the CURRENT patch "
                    "touched, confirm with check_patch_notes."
                ),
            })

        if name == "get_market_price":
            res = _universalis.get_price(args["item"], args.get("world_or_dc", "Aether"))
            if not res:
                return _dump({"found": False})
            return _dump({
                "found": True, "source": res.source, "item": res.item_name,
                "where": res.world_or_dc, "avg_price": round(res.avg_price),
                "min_price": res.min_price, "listings": res.listings,
            })

        if name == "whats_new":
            limit = int(args.get("limit", 12))
            items = _lodestone.news(limit=limit)
            result = {
                "found": bool(items), "source": "The Lodestone",
                "url": "https://na.finalfantasyxiv.com/lodestone/news/",
                "news": [{"category": i.category, "title": i.title, "url": i.url} for i in items],
            }
            if args.get("include_community"):
                threads = _forum.threads(limit=8)
                result["community_topics"] = {
                    "note": "General Discussion threads from the official forum — community chatter, not official news.",
                    "threads": [{"title": t.title, "url": t.url} for t in threads],
                }
            return _dump(result)

        if name == "search_forum":
            threads = _forum.search(args["query"], limit=int(args.get("limit", 6)))
            return _dump({
                "found": bool(threads),
                "source": "Square Enix Official Forum",
                "note": ("Official FFXIV community forum threads. Call read_forum_thread "
                         "with a url to read its posts (player comments)."),
                "threads": [{"title": t.title, "url": t.url} for t in threads],
            })

        if name == "read_forum_thread":
            posts = _forum.posts(args["url"], limit=int(args.get("limit", 8)))
            return _dump({
                "found": bool(posts),
                "source": "Square Enix Official Forum",
                "url": args["url"],
                "posts": posts,
            })

        if name == "open_zone_map":
            # The app's own rebuilt in-game map (Garland texture + the game's marker
            # table) covers every zone through Dawntrail, so this no longer depends
            # on A Realm Remapped's coverage. map_url in the result is what makes
            # the app switch its Map tab to this zone.
            zone = args["zone"]
            url = _garland.map_image_url(zone)
            if url:
                return _dump({"found": True, "source": _garland.SOURCE,
                              "zone": zone, "map_url": url,
                              "note": "Opened in the app's Map tab."})
            return _dump({"found": False, "note": f"no zone map found for '{zone}'"})

        if name == "save_preference":
            import workspaces
            line = workspaces.append_preference(args["preference"], args.get("reason", ""))
            return _dump({
                "ok": True, "saved": line,
                "note": ("Saved — this now applies in every chat. The player can "
                         "review or edit it under Settings."),
            })

        if name == "find_npc":
            hit = _garland.find(args["name"], kind="npc")
            if not hit:
                # An NPC Garland doesn't know is usually a wiki page — search it
                # here so the model doesn't spend a round trip doing the same.
                extra = {"note": ("This is the WIKI page for the NPC. Read the zone "
                                  "and (x, y) from `details`, then pin_on_map.")}
                return _wiki_fallback(args["name"], extra) or _dump({
                    "found": False,
                    "note": f"no NPC named '{args['name']}' in Garland or on the wiki",
                })
            data = _garland.npc_locations(hit.id)
            if not data or not data["locations"]:
                # Garland knows the NPC but not where it stands — the wiki infobox
                # usually carries the location line, so try it before giving up.
                extra = {"note": ("Garland has no coordinates for this NPC; this is "
                                  "the wiki page instead. Read the zone and (x, y) "
                                  "from `details`, then pin_on_map.")}
                return _wiki_fallback(hit.name, extra) or _dump({
                    "found": False, "name": hit.name, "url": hit.url,
                    "note": "NPC exists but neither Garland nor the wiki has coordinates for it.",
                })
            return _dump({
                "found": True, "source": data["source"], "name": data["name"],
                "url": data["url"], "locations": _cap(data["locations"], 12),
                "note": ("Pick the location whose quests match what the player needs, "
                         "then call pin_on_map with that zone, x, y."),
            })

        if name == "pin_on_map":
            place, x, y = args["place"], args["x"], args["y"]
            label = args.get("label", "")
            # Optional typed marker: a named game icon and/or an area circle.
            # canonical() resolves aliases ("botany" -> "logging") so downstream
            # consumers (the map's pin grouping, map: links) see one name per
            # symbol. An unknown name still degrades to a plain pin, never an
            # error — but the result SAYS so, or the model can't learn the
            # vocabulary and the player keeps getting unlabeled gold dots.
            from sources import icons as _icons
            asked_icon = (args.get("icon") or "").strip().lower()
            icon = _icons.canonical(asked_icon)
            icon_note = (f"Unknown icon '{asked_icon}' — shown as a plain pin. "
                         f"Valid names: {_icons.names()}."
                         if asked_icon and not icon else "")
            try:
                radius = max(0.0, float(args.get("area_radius") or 0))
            except (TypeError, ValueError):
                radius = 0.0

            # Pins are TEMPORARY: shown on the interactive map now, and re-openable
            # from a map: link in the reply — but NOT written into the player's
            # "My pins" store. An answer's pointer shouldn't permanently annotate
            # their map; if they want to keep the spot, they place their own pin.
            # map_texture, NOT map_image_url: a constructible URL is not a drawable
            # zone (dungeon interiors build a URL that 404s). The texture bytes are
            # disk-cached, so for a real zone this is the fetch the map needs anyway.
            if _garland.map_texture(place):
                from sources import gamemap
                from sources.maps import coord_to_pixel
                idx = gamemap._map_index().get(place.strip().lower()) or {}
                sf = idx.get("size_factor", 100)
                px = coord_to_pixel(x, sf, gamemap.TEX)
                py = coord_to_pixel(y, sf, gamemap.TEX)
                shown = label or f"({x:.1f}, {y:.1f})"
                # Length (not point) conversion for the area radius: map px per
                # one in-game coordinate unit.
                px_per_coord = (sf / 100) / 41 * gamemap.TEX
                radius_px = radius * px_per_coord if radius else 0
                # The exact markdown the model should paste — a link that reopens the
                # map on this pin. Angle brackets keep the spaced destination valid.
                extra = (f"&icon={icon}" if icon else "") + \
                        (f"&r={radius:.2f}" if radius else "")
                link = (f"[{place} ({x:.1f}, {y:.1f})]"
                        f"(<map:{place}?x={x:.2f}&y={y:.2f}&label={shown}{extra}>)")
                return _dump({
                    "found": True, "source": _garland.SOURCE, "zone": place,
                    "coord": [x, y],
                    # map_url + focus + pin make the app open the Map tab centred on
                    # a TEMPORARY pin (cleared when another zone is opened).
                    "map_url": _garland.map_image_url(place),
                    "focus": {"x": px, "y": py},
                    "pin": {"x": px, "y": py, "label": shown,
                            "icon": icon, "radius_px": radius_px},
                    "link": link,
                    "note": ("Temporary pin shown on the interactive map. Paste the "
                             "`link` string in your reply so the player can re-open "
                             "this exact spot — the pin clears when they view another "
                             "zone. They can save a picture with the map's camera "
                             "button, or keep the pin with the map's 📌 button."),
                    **({"icon_note": icon_note} if icon_note else {}),
                })

            # No drawable zone (a dungeon interior) — fall back to a static image on
            # the official XIVAPI map, saved as an asset, so the answer still shows
            # SOMETHING rather than nothing.
            saver = (ctx or {}).get("save_asset")
            res = _maps.pin(place, x, y, label)
            if not res:
                return _dump({"found": False, "note": f"no map found for '{place}'"})
            asset_id = saver(res["image"], "png") if saver else None
            return _dump({
                "found": True, "source": "XIVAPI (official map)",
                "place": res["place_name"], "coord": res["coord"],
                "pixel": res["pixel"], "asset_id": asset_id,
                "asset_kind": "map", "zone": res["place_name"],
            })

        if name == "find_zone":
            import difflib
            from sources import gamemap
            q = (args.get("query") or "").strip().lower()
            regs = gamemap.regions()
            all_zones = [(z, g["region"]) for g in regs for z in g["zones"]]
            matches = [{"zone": z, "region": r} for z, r in all_zones
                       if q and (q == z.lower() or q in z.lower() or z.lower() in q)]
            if not matches and q:
                close = set(difflib.get_close_matches(
                    q, [z.lower() for z, _ in all_zones], n=5, cutoff=0.6))
                matches = [{"zone": z, "region": r} for z, r in all_zones
                           if z.lower() in close]
            return _dump({
                "found": bool(matches), "matches": matches[:8],
                "note": "" if matches else (
                    "Nothing close. Only drawable overworld zones are listed — "
                    "try the exact in-game zone name, or a different guess."),
            })

        if name == "pin_points_on_map":
            place = args["place"]
            category = " ".join(str(args.get("category") or "Points").split())[:40]
            from sources import icons as _icons
            icon = _icons.canonical(args.get("icon") or "")
            raw_pts = args.get("points") or []
            source_note = ""
            # Aether currents come EXACTLY from the installed game client when
            # it's present — no research, no transcription drift. The model
            # passes preset="aether_currents" and no points.
            if (args.get("preset") == "aether_currents"
                    or (not raw_pts and "aether current" in category.lower())):
                from sources import aethercurrents
                got = aethercurrents.find(place)
                if got:
                    # canonical zone name: the request may have said "Ilsabard
                    # Garlemald" — the map lookup below needs "Garlemald".
                    place, raw_pts = got
                    category = category if "aether" in category.lower() else "Aether Currents"
                    # The game has NO aether-current map symbol (the shard icon
                    # reads as a city aetheryte) — the white star is the marker
                    # for exactly this case. Always, whatever the model passed.
                    icon = "star"
                    source_note = (f" Coordinates read from the installed game "
                                   f"client ({len(raw_pts)} field currents; the "
                                   "zone's remaining currents come from quests, "
                                   "not map points).")
                elif not raw_pts:
                    from sources import gameclient as _gc
                    why = ("the game client isn't installed on this machine"
                           if not _gc.available() else
                           f"'{place}' didn't match a zone — pass the ZONE name "
                           "('Garlemald'), not a region ('Ilsabard')")
                    return _dump({"found": False, "note": (
                        f"No client data: {why}. If the zone name is right, look "
                        "the coordinates up on the wiki page 'Aether Currents' "
                        "(it lists every zone's table) and call again WITH points.")})
            if not isinstance(raw_pts, list) or not raw_pts:
                return _dump({"found": False, "note": "points is empty"})
            if not _garland.map_texture(place):
                return _dump({"found": False,
                              "note": f"'{place}' has no drawable overworld map"})
            from sources import gamemap
            from sources.maps import coord_to_pixel
            idx = gamemap._map_index().get(place.strip().lower()) or {}
            sf = idx.get("size_factor", 100)
            pins, link_pts = [], []
            for p in raw_pts[:40]:
                try:
                    x, y = float(p["x"]), float(p["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                # labels ride inside the link's pts= list: strip its separators.
                label = str(p.get("label") or "").replace("|", "/").replace(",", " ")
                label = " ".join(label.split())[:40]
                pins.append({"x": coord_to_pixel(x, sf, gamemap.TEX),
                             "y": coord_to_pixel(y, sf, gamemap.TEX),
                             "label": label})
                link_pts.append(f"{x:.2f},{y:.2f}" + (f",{label}" if label else ""))
            if not pins:
                return _dump({"found": False, "note": "no valid points"})
            cx = sum(p["x"] for p in pins) / len(pins)
            cy = sum(p["y"] for p in pins) / len(pins)
            link = (f"[{place} — {len(pins)} × {category}]"
                    f"(<map:{place}?cat={category}"
                    + (f"&icon={icon}" if icon else "")
                    + f"&pts={'|'.join(link_pts)}>)")
            return _dump({
                "found": True, "source": _garland.SOURCE, "zone": place,
                "count": len(pins),
                "map_url": _garland.map_image_url(place),
                "focus": {"x": cx, "y": cy},
                # pins/category/icon ride the map event -> the app shows the whole
                # set as TEMPORARY pins with a Save button.
                "pins": pins, "category": category, "icon": icon,
                "link": link,
                "note": (f"All {len(pins)} points shown as TEMPORARY pins on the "
                         "interactive map. Paste the `link` in your reply; the "
                         "player can press the map's Save button to keep the set "
                         f"permanently as 'Custom – {category}'." + source_note),
            })

        if name == "show_image":
            data = _wiki.fetch_image(args["url"])
            if not data:
                return _dump({"found": False, "note": "image url could not be fetched"})
            # A ZONE MAP is not a picture to post — the interactive map is the app's
            # only map surface, and a static map in chat carries no pin and misleads
            # ("gives me a map, no pin"). Zone maps are big near-squares; portraits
            # and item renders aren't. The url saying "map" seals it.
            try:
                from PIL import Image
                import io as _io
                w, h = Image.open(_io.BytesIO(data)).size
                big = w > 700 and h > 700
                mappish_name = bool(re.search(r"(^|[_\-/ %])map([_\-. %]|$)",
                                              args["url"].rsplit("/", 1)[-1], re.I))
                # Named "map" + big -> map. Unnamed but big AND near-square -> map.
                # Small "map"-named images (item renders like Timeworn Maps) pass.
                if big and (mappish_name or (w > 1000 and 0.85 < (w / h) < 1.18)):
                    return _dump({
                        "found": False,
                        "note": ("That image looks like a ZONE MAP — never post maps "
                                 "as pictures. Use pin_on_map / open_zone_map: the "
                                 "app shows maps on its interactive Map tab."),
                    })
            except Exception:
                pass   # unreadable image data — let the normal path handle it
            ext = args["url"].rsplit(".", 1)[-1].lower()
            if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
                ext = "png"
            saver = (ctx or {}).get("save_asset")
            asset_id = saver(data, ext) if saver else None
            # The saved file is a TEMPORARY (tmp_*) asset: it renders inline in
            # chat, and the player promotes it to the Assets shelf themselves.
            return _dump({"found": True, "asset_id": asset_id, "label": args.get("label", "")})

        if name == "annotate_image":
            # Wired to the assets pipeline in app.py, which supplies ctx.
            handler = (ctx or {}).get("annotate_handler")
            if not handler:
                return _dump({"error": "annotation not available in this context"})
            return _dump(handler(args))

        if name == "create_doc":
            handler = (ctx or {}).get("create_doc")
            if not handler:
                return _dump({"error": "cannot create docs in this context"})
            return _dump(handler(args))

        if name == "import_character":
            handler = (ctx or {}).get("import_character")
            if not handler:
                return _dump({"error": "character import not available here"})
            return _dump(handler(args))

        return _dump({"error": f"unknown tool {name}"})
    except Exception as exc:  # surface errors to the model rather than crashing the loop
        # `or repr` because _dump drops empty strings — a blank str(exc) would
        # otherwise erase the error key and read as success downstream.
        return _dump({"error": str(exc) or repr(exc)})


def close_clients() -> None:
    _wiki.close()
    _universalis.close()
    _maps.close()
    _realm.close()
    _lodestone.close()
    _forum.close()
