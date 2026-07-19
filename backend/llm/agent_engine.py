"""Subscription engine — runs Claude via the user's Pro/Max subscription.

Uses the Claude Agent SDK (which runs on the Claude Code CLI) instead of litellm.
The SDK drives the agentic loop itself; we expose OUR tools to it as an in-process
MCP server, wrapping the SAME execute_tool logic the API path uses so behavior is
identical. Emits the same event dicts as the API engine so the app layer is agnostic.

NOTE: The actual query path can only be exercised with a real subscription token,
so streaming granularity and model-id mapping may need a tweak once run live.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import AsyncIterator

from claude_agent_sdk import (
    query, tool, create_sdk_mcp_server, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ToolUseBlock,
)

from llm.tools import execute_tool
from llm import interactive
from subscription import get_oauth_token, claude_cli_path, full_env_path
from paths import DATA_DIR

# Sentinel marking the end of the event queue in chat().
_SENTINEL = object()

# Built-in Claude Code tools we never want a game assistant touching.
_BLOCKED_BUILTINS = ["Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch", "NotebookEdit"]


async def _emit_result(out_q: "asyncio.Queue", name: str, result_json: str) -> None:
    """Push tool_result + any source/asset/map events derived from a tool result."""
    try:
        d = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        d = {}
    ok = not (isinstance(d, dict) and d.get("error"))
    await out_q.put({"type": "tool_result", "name": name, "ok": ok})
    if isinstance(d, dict):
        src, url = d.get("source"), d.get("url", "")
        if src:
            await out_q.put({"type": "source", "label": src, "url": url})
        if d.get("asset_id"):
            # kind routes the asset in the UI: "map" assets (pinned maps) show in
            # the Map view, everything else lands in the Assets tab as an image.
            # url = the zone's interactive map, linked from the pinned image.
            await out_q.put({"type": "asset", "name": d["asset_id"],
                             "kind": d.get("asset_kind", "image"), "zone": d.get("zone", ""),
                             "url": d.get("interactive_url", "")})
        if d.get("map_url"):
            await out_q.put({"type": "map", "url": d["map_url"], "zone": d.get("zone", ""),
                             # focus (2048-space x/y) centres the map; pin is a
                             # TEMPORARY marker drawn there (not in the pin store);
                             # pins/category/icon are a whole temp SET at once
                             "focus": d.get("focus"), "pin": d.get("pin"),
                             "pins": d.get("pins"), "category": d.get("category", ""),
                             "icon": d.get("icon", "")})


def _build_server(ctx: dict, out_q: "asyncio.Queue"):
    """Wrap our tools as SDK tools. Each reuses execute_tool and streams result
    events (tool_result/source/asset/map) onto the shared queue. Also exposes
    ask_user, which pauses the run for a real answer from the player."""

    @tool("search_wiki", "Search the FFXIV Console Games Wiki — mechanics, raids, "
          "quests, drops, lore. wiki is always 'consolegames'. Prefer lookup_item "
          "for item facts and find_npc for NPC locations — both fall back to this "
          "wiki BY THEMSELVES when Garland is empty, so never repeat their query "
          "here to double-check an answer you already have.",
          {"wiki": str, "query": str})
    async def search_wiki(args):
        res = await asyncio.to_thread(execute_tool, "search_wiki", args, ctx)
        await _emit_result(out_q, "search_wiki", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("lookup_item", "Look up an item in Garland Tools (this app's item database) "
          "— item level, the jobs that can equip it, stats, materia slots, icon, and "
          "its upgrades_to/downgrades_from progression chain (what replaces it). "
          "Prefer over a wiki for item facts. If Garland has no record the wiki is "
          "searched AUTOMATICALLY and the result says which source answered — one "
          "call covers both; re-verify only when check_patch_notes says the current "
          "patch touched the topic.", {"name": str})
    async def lookup_item(args):
        res = await asyncio.to_thread(execute_tool, "lookup_item", args, ctx)
        await _emit_result(out_q, "lookup_item", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("get_market_price", "Current market-board listings for an item.",
          {"item": str, "world_or_dc": str})
    async def get_market_price(args):
        res = await asyncio.to_thread(execute_tool, "get_market_price", args, ctx)
        await _emit_result(out_q, "get_market_price", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("whats_new", "Recent official FFXIV news from the Lodestone (patches, "
          "maintenance, events, status).", {"limit": int})
    async def whats_new(args):
        res = await asyncio.to_thread(execute_tool, "whats_new", args, ctx)
        await _emit_result(out_q, "whats_new", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("update_doc", "Rewrite the doc currently open in the editor WHOLESALE — "
          "only for restructuring most of it; for a targeted change (a row, a "
          "heading, a sentence) use edit_doc instead. Pass the COMPLETE new "
          "markdown — it replaces the file, so include everything you "
          "keep: headings, table rows, checkbox states, asset images, links, "
          "Sources. Change only what they asked. Then say in 1-2 sentences what you "
          "changed — that is your reply in the thread.", {"content": str})
    async def update_doc(args):
        res = await asyncio.to_thread(execute_tool, "update_doc", args, ctx)
        await _emit_result(out_q, "update_doc", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("edit_doc", "Targeted change to the doc currently open in the editor: "
          "replace ONE exact snippet, leaving the rest untouched. old_text must "
          "match the saved markdown exactly (whitespace included) and pin down one "
          "spot — a missing or ambiguous match returns an error; retry with a "
          "longer snippet, or pass occurrence (1-based). Prefer this over "
          "update_doc for anything short of a restructure. Then say in 1-2 "
          "sentences what you changed — that is your reply in the thread.",
          {"old_text": str, "new_text": str, "occurrence": int})
    async def edit_doc(args):
        res = await asyncio.to_thread(execute_tool, "edit_doc", args, ctx)
        await _emit_result(out_q, "edit_doc", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("read_doc", "Read ONE saved reference doc by id (the system context lists "
          "ids + titles). Pull a doc only when it's actually relevant — never all "
          "of them 'just in case'.", {"doc_id": str})
    async def read_doc(args):
        res = await asyncio.to_thread(execute_tool, "read_doc", args, ctx)
        return {"content": [{"type": "text", "text": res}]}

    @tool("record_gear", "Save a job's gear set to the player's profile. The Lodestone "
          "only shows the job they logged out on, so this is the ONLY way to learn "
          "any other job's gear — use it after reading an in-game character-window "
          "screenshot (source='screenshot') or when they tell you (source='player'). "
          "ALWAYS read the gear back and get confirmation first; a misread name "
          "becomes a wrong fact you repeat for weeks. job, source, "
          "pieces=[{slot,name,item_level}].",
          {"job": str, "source": str, "pieces": list})
    async def record_gear(args):
        res = await asyncio.to_thread(execute_tool, "record_gear", args, ctx)
        await _emit_result(out_q, "record_gear", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("db_links", "Garland Tools database urls for a BATCH of names — items/gear, "
          "dungeons+trials (kind 'instance'), NPCs, quests, achievements, mobs, "
          "FATEs, nodes. Use when you name in-game content so each name links to the "
          "app's Database tab. Pass EVERY name in ONE call, never one per row. "
          "Unfound names return in 'missing' — leave those plain, never invent a url. "
          "entries=[{name, kind}].", {"entries": list})
    async def db_links(args):
        res = await asyncio.to_thread(execute_tool, "db_links", args, ctx)
        await _emit_result(out_q, "db_links", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("check_patch_notes", "Did the CURRENT patch change this topic? Reads the "
          "official patch notes. Use it to decide whether a fact needs re-verifying: "
          "untouched by this patch -> one source is enough; changed -> confirm "
          "officially. mentioned=false is a real answer, not a failed search. Omit "
          "topic for a summary of the patch.", {"topic": str})
    async def check_patch_notes(args):
        res = await asyncio.to_thread(execute_tool, "check_patch_notes", args, ctx)
        await _emit_result(out_q, "check_patch_notes", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("search_forum", "Search the OFFICIAL Square Enix FFXIV forum for a topic; "
          "returns community threads (title + url). Then read_forum_thread to pull "
          "the posts. Players share lots of extra info/tips there.", {"query": str})
    async def search_forum(args):
        res = await asyncio.to_thread(execute_tool, "search_forum", args, ctx)
        await _emit_result(out_q, "search_forum", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("read_forum_thread", "Read the posts (player comments/discussion) from an "
          "OFFICIAL forum thread url — the community's extra info, tips, context.",
          {"url": str})
    async def read_forum_thread(args):
        res = await asyncio.to_thread(execute_tool, "read_forum_thread", args, ctx)
        await _emit_result(out_q, "read_forum_thread", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("open_zone_map", "Open a zone on the app's interactive in-game map — "
          "every zone through Dawntrail, with the game's own markers (aetherytes, "
          "settlements, dungeons, area names).", {"zone": str})
    async def open_zone_map(args):
        res = await asyncio.to_thread(execute_tool, "open_zone_map", args, ctx)
        await _emit_result(out_q, "open_zone_map", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("save_preference", "Remember a standing BEHAVIOUR preference the player "
          "asked for ('always/never/from now on …'). Appended to a preferences file "
          "read into every future chat. Behaviour only — character facts belong in "
          "the profile. One short imperative sentence.",
          {"preference": str, "reason": str})
    async def save_preference(args):
        res = await asyncio.to_thread(execute_tool, "save_preference", args, ctx)
        await _emit_result(out_q, "save_preference", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("find_npc", "Where an NPC stands: every location with EXACT in-game "
          "coordinates plus the quests they give at each spot (from Garland Tools). "
          "THE tool for 'where is <NPC>' and for pinning a quest giver — find the "
          "quest's NPC, call this, pick the location whose quests match, then "
          "pin_on_map with its zone and x/y. If Garland doesn't know the NPC the "
          "wiki is searched AUTOMATICALLY and the result says so — read zone and "
          "(x, y) from its `details`; don't re-run the name through search_wiki.",
          {"name": str})
    async def find_npc(args):
        res = await asyncio.to_thread(execute_tool, "find_npc", args, ctx)
        await _emit_result(out_q, "find_npc", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("pin_on_map", "Drop a TEMPORARY pin on the app's interactive in-game map "
          "at in-game flag coordinates and open the map centred on it. Look up "
          "place (zone name) + X/Y coords first. Returns a markdown `link` — paste "
          "it in your reply so the player can re-open the spot (the pin clears "
          "when they view another zone). Optional: icon (a named game symbol "
          "matching what's marked — mining, fishing, fate, mob, quest, shop…) and "
          "area_radius (map coords, 1.0 = one grid square) to mark an AREA as a "
          "translucent dashed circle instead of a point.",
          {"place": str, "x": float, "y": float, "label": str,
           "icon": str, "area_radius": float})
    async def pin_on_map(args):
        res = await asyncio.to_thread(execute_tool, "pin_on_map", args, ctx)
        await _emit_result(out_q, "pin_on_map", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("find_zone", "Resolve/confirm a ZONE name against the app's map index "
          "— instant and local. Verify your canonical guess when the player "
          "describes a place colloquially ('the moon' -> confirm 'Mare "
          "Lamentorum') BEFORE asking them what zone they mean.",
          {"query": str})
    async def find_zone(args):
        res = await asyncio.to_thread(execute_tool, "find_zone", args, ctx)
        return {"content": [{"type": "text", "text": res}]}

    @tool("pin_points_on_map", "Pin a whole CATEGORY of points on ONE zone's "
          "interactive map at once (every aether current, vista, hunt spawn…). "
          "For AETHER CURRENTS pass preset='aether_currents' and NO points — "
          "coordinates come exactly from the installed game client. Otherwise "
          "look all coordinates up first; never invent them. Points show as "
          "TEMPORARY typed pins with a Save button that keeps the set as "
          "'Custom – <category>'. Paste the returned `link` in your reply.",
          {"place": str, "category": str, "icon": str, "preset": str, "points": list})
    async def pin_points_on_map(args):
        res = await asyncio.to_thread(execute_tool, "pin_points_on_map", args, ctx)
        await _emit_result(out_q, "pin_points_on_map", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("show_image", "Show the player a picture (e.g. an NPC portrait) by saving "
          "an image url a tool returned into the chat's assets.",
          {"url": str, "label": str})
    async def show_image(args):
        res = await asyncio.to_thread(execute_tool, "show_image", args, ctx)
        await _emit_result(out_q, "show_image", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("annotate_image", "Draw fight-guide callouts on a chat asset image.",
          {"asset_id": str, "title": str, "annotations": list})
    async def annotate_image(args):
        res = await asyncio.to_thread(execute_tool, "annotate_image", args, ctx)
        await _emit_result(out_q, "annotate_image", res)
        return {"content": [{"type": "text", "text": res}]}

    @tool("create_doc", "Save substantial content (a guide, build, checklist, plan) "
          "as a DRAFT reference doc the player reviews via a chat link, instead of "
          "pasting it all into the reply. Give title + full markdown content; use "
          "'- [ ] item' checkboxes for checklists. Then keep your reply to 1-2 sentences.",
          {"title": str, "content": str})
    async def create_doc(args):
        res = await asyncio.to_thread(execute_tool, "create_doc", args, ctx)
        try:
            d = json.loads(res)
        except (json.JSONDecodeError, TypeError):
            d = {}
        if d.get("doc_id"):
            await out_q.put({
                "type": "doc", "id": d["doc_id"],
                "title": d.get("title") or args.get("title", ""),
                "draft": True, "content": args.get("content", ""),
            })
            payload = {"ok": True, "doc_id": d["doc_id"],
                       "note": "Draft doc created and shown to the player for review."}
        else:
            payload = d
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    @tool("import_character", "Bind an FFXIV character to this chat's profile "
          "workspace from the Lodestone (auto-fills identity). Pass a Lodestone URL/id "
          "or a character name (+ optional world). If several match, returns candidates "
          "— ask the player which, then call again with the id.",
          {"query": str, "world": str})
    async def import_character(args):
        res = await asyncio.to_thread(execute_tool, "import_character", args, ctx)
        return {"content": [{"type": "text", "text": res}]}

    @tool("ask_user", "Ask the player a short clarifying question when the request "
          "is ambiguous or you need them to choose before proceeding well. Give 2-4 "
          "concise options; they can also type their own. Returns their answer.",
          {"question": str, "options": list, "header": str})
    async def ask_user(args):
        ask_id, fut = interactive.create()
        await out_q.put({
            "type": "ask", "id": ask_id,
            "question": args.get("question", ""),
            "options": args.get("options") or [],
            "header": args.get("header", ""),
        })
        answer = await interactive.wait(ask_id, fut)
        return {"content": [{"type": "text", "text": json.dumps({"answer": answer})}]}

    return create_sdk_mcp_server(
        name="ffxiv", version="1.0.0",
        # A tool must ALSO be in allowed_tools below — and @tool alone registers
        # nothing: forgetting a tool here makes it silently invisible to the model
        # (find_npc shipped decorated-but-unlisted once, and the model ground
        # through 12 wiki searches it didn't need).
        tools=[search_wiki, lookup_item, get_market_price, whats_new, check_patch_notes,
               db_links, find_npc, save_preference,
               record_gear, update_doc, edit_doc, read_doc,
               search_forum, read_forum_thread, open_zone_map, find_zone,
               pin_on_map, pin_points_on_map, show_image, annotate_image,
               create_doc, import_character, ask_user],
    )


async def selftest() -> dict:
    """Minimal subscription init test that captures the CLI's stderr — for
    diagnosing the packaged-app 'initialize timeout'. Uses the stored token."""
    token = get_oauth_token()
    if not token:
        return {"ok": False, "error": "No subscription token stored."}
    cli = claude_cli_path()
    if not cli:
        return {"ok": False, "error": "Claude Code CLI not found."}
    buf: list[str] = []
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "ANTHROPIC_API_KEY": "", "PATH": full_env_path()}
    options = ClaudeAgentOptions(
        model="claude-opus-4-8", cli_path=cli, env=env, cwd=str(DATA_DIR),
        setting_sources=[], max_turns=1, load_timeout_ms=45000,
        stderr=lambda line: buf.append(line),
    )
    try:
        async for m in query(prompt="Reply with exactly: ok", options=options):
            return {"ok": True, "msg_type": type(m).__name__, "cli_path": cli, "stderr": "\n".join(buf[-15:])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "cli_path": cli, "stderr": "\n".join(buf[-15:])}
    return {"ok": False, "error": "no messages returned", "cli_path": cli, "stderr": "\n".join(buf[-15:])}


def _flatten(messages: list[dict]) -> tuple[str, str]:
    """Split our message list into (system_prompt, conversation_transcript).
    Concatenates ALL system messages (base prompt + attachment context + saved
    docs) so later ones don't clobber the base prompt."""
    systems = []
    lines = []
    for m in messages:
        if m["role"] == "system":
            systems.append(m["content"])
        else:
            lines.append(f"{m['role'].upper()}: {m['content']}")
    return "\n\n".join(systems), "\n\n".join(lines)


async def chat(model_id: str, messages: list[dict], ctx: dict | None = None) -> AsyncIterator[dict]:
    token = get_oauth_token()
    if not token:
        yield {"type": "error", "message": f"No Claude subscription token. Run '{'claude setup-token'}' then add it in settings."}
        return
    cli = claude_cli_path()
    if not cli:
        yield {"type": "error", "message": "Claude Code not found on this machine. Install it to use the subscription path."}
        return

    ctx = ctx or {}
    # All events (tokens, tool calls, tool results, sources, and interactive
    # `ask` questions from the ask_user tool) funnel through one queue so they
    # stream out in order even while an MCP tool is awaiting a user's answer.
    out_q: asyncio.Queue = asyncio.Queue()
    server = _build_server(ctx, out_q)
    system, transcript = _flatten(messages)

    # Hand the system prompt over as a FILE, not a command-line argument.
    # The SDK's default is `--system-prompt "<the whole thing>"`. Ours runs ~8k chars
    # (and grows with the player's profile). When the resolved CLI is a .cmd wrapper —
    # e.g. an npm install of Claude Code — the call goes through cmd.exe, whose command
    # line caps at 8191 chars, and the run dies with "The command line is too long".
    # `--system-prompt-file` keeps the command line tiny no matter how big the prompt
    # gets. (Verified against the live CLI that the file's contents are really applied.)
    sp_path: str | None = None
    try:
        fd, sp_path = tempfile.mkstemp(prefix="ffxiv-sysprompt-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
    except OSError:
        sp_path = None  # fall back to passing it inline

    # Isolate auth: force the OAuth token and neutralize any API key so the
    # subscription is used (ANTHROPIC_API_KEY would otherwise take precedence and bill).
    child_env = {
        **os.environ,
        "CLAUDE_CODE_OAUTH_TOKEN": token,
        "ANTHROPIC_API_KEY": "",
        # Full PATH so the CLI's launcher finds PowerShell/Node even if the
        # packaged app inherited a stale PATH.
        "PATH": full_env_path(),
    }
    stderr_buf: list[str] = []  # capture the CLI's stderr to diagnose init failures

    options = ClaudeAgentOptions(
        system_prompt=({"type": "file", "path": sp_path} if sp_path else system),
        mcp_servers={"ffxiv": server},
        allowed_tools=[
            "mcp__ffxiv__search_wiki",
            "mcp__ffxiv__lookup_item",
            "mcp__ffxiv__get_market_price",
            "mcp__ffxiv__whats_new",
            "mcp__ffxiv__check_patch_notes",
            "mcp__ffxiv__db_links",
            "mcp__ffxiv__find_npc",
            "mcp__ffxiv__save_preference",
            "mcp__ffxiv__record_gear",
            "mcp__ffxiv__update_doc",
            "mcp__ffxiv__edit_doc",
            "mcp__ffxiv__read_doc",
            "mcp__ffxiv__search_forum",
            "mcp__ffxiv__read_forum_thread",
            "mcp__ffxiv__open_zone_map",
            "mcp__ffxiv__find_zone",
            "mcp__ffxiv__pin_on_map",
            "mcp__ffxiv__pin_points_on_map",
            "mcp__ffxiv__show_image",
            "mcp__ffxiv__annotate_image",
            "mcp__ffxiv__create_doc",
            "mcp__ffxiv__import_character",
            "mcp__ffxiv__ask_user",
        ],
        disallowed_tools=_BLOCKED_BUILTINS,
        permission_mode="bypassPermissions",  # auto-run our own safe tools
        model=model_id,
        cli_path=cli,
        env=child_env,
        cwd=str(DATA_DIR),  # a writable dir (packaged install dir may be read-only)
        stderr=lambda line: stderr_buf.append(line),
        setting_sources=[],  # don't inherit the user's Claude Code project/settings
        # 48, not 16: bulk requests ("pin every quest in the relic line") spend
        # a search + locate + pin turn PER QUEST — 16 died mid-list with
        # "Reached maximum number of turns" and no answer at all.
        max_turns=48,
        load_timeout_ms=120000,  # cold CLI start can be slow; avoid init timeouts
    )

    # If images are attached, send a structured user message (text + image
    # blocks, Anthropic format) instead of a plain string.
    images = ctx.get("images") or []
    if images:
        async def _prompt():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": transcript}, *images],
                },
            }
        prompt = _prompt()
    else:
        prompt = transcript

    async def run_query():
        try:
            async for message in query(prompt=prompt, options=options):
                # The SDK's final ResultMessage carries real usage + the
                # would-have-cost at API prices — recorded as subscription-
                # covered spend in the usage ledger.
                if type(message).__name__ == "ResultMessage":
                    try:
                        import usage as usage_ledger
                        u = getattr(message, "usage", None) or {}
                        usage_ledger.record(
                            context=ctx.get("usage_context", "chat"),
                            model=model_id, auth="subscription",
                            input_tokens=(u.get("input_tokens", 0)
                                          + u.get("cache_read_input_tokens", 0)
                                          + u.get("cache_creation_input_tokens", 0)),
                            output_tokens=u.get("output_tokens", 0),
                            cost_usd=getattr(message, "total_cost_usd", 0) or 0,
                            estimated=False)
                    except Exception:
                        pass
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            await out_q.put({"type": "token", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            # Only surface OUR tools; hide CLI built-ins (ToolSearch
                            # etc.) that the SDK uses internally to load our MCP tools.
                            if not block.name.startswith("mcp__ffxiv__"):
                                continue
                            name = block.name.split("__")[-1]
                            # ask_user surfaces via its own `ask` event, not a chip.
                            if name != "ask_user":
                                await out_q.put({"type": "tool", "name": name,
                                                 "args": block.input or {}})
        except Exception as exc:  # noqa: BLE001
            detail = "\n".join(stderr_buf[-12:]).strip()
            msg = f"{type(exc).__name__}: {exc}"
            if detail:
                msg += f"\n\nClaude CLI output:\n{detail}"
            from llm import looplog
            looplog.log(ctx.get("chat_id", ""), "subscription", "error", detail=msg)
            await out_q.put({"type": "error", "message": msg})
        finally:
            await out_q.put(_SENTINEL)

    task = asyncio.create_task(run_query())
    try:
        while True:
            ev = await out_q.get()
            if ev is _SENTINEL:
                break
            yield ev
    finally:
        if not task.done():
            task.cancel()
        if sp_path:  # don't leave system-prompt temp files behind
            try:
                os.unlink(sp_path)
            except OSError:
                pass

    yield {"type": "done"}
