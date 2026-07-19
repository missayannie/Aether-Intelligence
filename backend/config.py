"""Central configuration: source registry, routing rules, and the model catalog.

Everything the assistant knows about *where* to look and *which* model to use
lives here so the rest of the app stays declarative.
"""
from __future__ import annotations

# --- Wiki sources (MediaWiki -> one client, config differs by base_url) ---
# `kind` drives source-priority routing so the model reaches for the right well.
# `url`/`support` feed the Sources tab; see the note on OTHER_SOURCES below.
# Gamer Escape and FF Fandom were dropped deliberately: Garland covers items/NPCs
# with structured data, and Console Games Wiki covers the rest.
WIKIS = {
    "consolegames": {
        "label": "FFXIV Console Games Wiki",
        "api": "https://ffxiv.consolegameswiki.com/mediawiki/api.php",
        "kind": ["mechanics", "fights", "raids", "dungeons", "items", "quests",
                 "npcs", "lore", "story"],
        "url": "https://ffxiv.consolegameswiki.com/",
        "support": "https://www.patreon.com/ffxivwiki",
    },
}

# --- Non-wiki sources ---
# `support` is the project's OWN funding page, shown beside the source in the app.
# Almost everything here is a volunteer community project we lean on for free; if
# this app ever asks for a coffee, the people whose data it runs on should be one
# click away. Every URL below was read off that project's own site — NEVER guess a
# donate link, because sending someone's money to the wrong place is a real harm.
# A source with no known funding page simply has no `support` key.
OTHER_SOURCES = {
    "universalis": {"label": "Universalis", "kind": ["prices", "market"],
                    "url": "https://universalis.app/",
                    "support": "https://patreon.com/universalis"},
    "garland": {"label": "Garland Tools", "kind": ["items", "data"],
                "url": "https://garlandtools.org/db/",
                # Garland's page uses Patreon's generic bePatron widget; u=14110588
                # is the creator id it actually resolves to.
                "support": "https://www.patreon.com/bePatron?u=14110588"},
    "arealmremapped": {"label": "A Realm Remapped", "kind": ["maps"],
                       "url": "https://arealmremapped.com/",
                       "support": "https://www.patreon.com/ARealmRemapped"},
    "xivapi": {"label": "XIVAPI", "kind": ["items", "actions", "data"],
               "url": "https://xivapi.com/"},
    "lodestone": {"label": "The Lodestone", "kind": ["news", "patch", "events"],
                  "url": "https://na.finalfantasyxiv.com/lodestone/"},
    "sqex_forum": {"label": "Square Enix Forum", "kind": ["news", "official"],
                   "url": "https://forum.square-enix.com/ffxiv/"},
    "official_faq": {"label": "Official UIGuide FAQ", "kind": ["howto", "system"]},
}

# Source-priority routing: question kind -> ordered source ids to try.
# Baked into the system prompt so the model doesn't answer mechanics from a lore wiki.
SOURCE_PRIORITY = {
    "mechanics": ["consolegames"],
    "fights": ["consolegames"],
    "items": ["garland", "consolegames", "xivapi"],
    "quests": ["consolegames"],
    "npcs": ["garland", "consolegames"],
    "lore": ["consolegames"],
    "story": ["consolegames"],
    "news": ["lodestone", "sqex_forum"],
    "patch": ["lodestone", "sqex_forum"],
    "events": ["lodestone"],
    "howto": ["official_faq", "consolegames"],
    "prices": ["universalis"],
}

# --- Model catalog: provider -> models. Maps to litellm model ids. ---
# `tool_use` flags how reliable a model is in the agentic scrape->annotate loop —
# the thing this app actually does. It is NOT raw model quality: a model can be
# smart and still be a poor fit here if it drops tool calls over a 16-turn loop.
#   excellent - drives the full loop reliably
#   good      - fine for lookups; may need retries on long guide builds
#   fair      - workable, expect stumbles
#   untested  - CAPABLE per litellm (function calling + vision), but nobody has
#               driven THIS loop with it yet. Try it and re-rate.
#
# NOTE ON COST: only Anthropic models can run on the Pro/Max subscription (see
# llm/registry.py) — i.e. free at point of use. Every OpenAI/Google/xAI entry
# below bills a real API key per token, so the $/M figures in the comments are
# money you actually spend. Prices are input/output per 1M tokens as reported by
# the installed litellm.
MODEL_CATALOG = {
    "anthropic": {
        "label": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "models": [
            # $5/$25 — most capable, slowest. Default fallback when no OpenAI
            # key is present (subscription = free at point of use).
            {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "tool_use": "excellent"},
            # $2/$10 — near-Opus on agentic work, markedly faster.
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "tool_use": "excellent"},
            # $1/$5 — fastest Claude, 200k context (ample: the system prompt is ~14k
            # chars and tool results are pages, not books). Free on the subscription.
            {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "tool_use": "good"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "models": [
            # $0.75/$4.50 — recommended default (user-rated 2026-07: fastest +
            # most accurate in daily use; verified through the tool loop).
            # GPT-5 and the 5.6 line (luna/terra/sol) were removed 2026-07 at
            # the user's request: they force temperature=1 and have patchy
            # reasoning_effort support, fighting the app's sampling settings.
            {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini", "tool_use": "excellent", "recommended": True},
        ],
    },
    "gemini": {
        "label": "Google",
        "env_key": "GEMINI_API_KEY",
        "models": [
            # Free-tier Gemini keys have ZERO Pro quota (429 limit:0) — needs
            # a billing-enabled key. The friendly stream error explains this.
            {"id": "gemini/gemini-2.5-pro", "label": "Gemini 2.5 Pro", "tool_use": "good"},
            # $0.30/$2.50 — cheapest credible tool-user here.
            {"id": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash", "tool_use": "good"},
            # 2.5 Flash Lite removed 2026-07: Google 404s it for new API users
            # ("no longer available to new users").
        ],
    },
    "xai": {
        "label": "xAI",
        "env_key": "XAI_API_KEY",
        "models": [
            {"id": "xai/grok-4", "label": "Grok 4", "tool_use": "fair"},
        ],
    },
}

# A polite, descriptive UA keeps us in good standing with community APIs.
USER_AGENT = "AetherIntelligence/0.1 (personal FFXIV assistant; contact via app)"
