"""API engine — the litellm agentic loop (billed API keys).

Streams a model's response, running tools across multiple rounds until it produces
a final answer. Covers all four providers via API keys. Async so it composes with
the async subscription engine behind one dispatcher. Yields the shared event dicts:

  {"type": "token"|"tool"|"tool_result"|"source"|"done"|"error", ...}
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import litellm

from llm.tools import TOOLS, execute_tool
from llm.registry import provider_for_model
from llm import interactive
from keys import vault

# Guard against runaway tool loops. 24, not 10: a bulk request ("pin every
# quest in the relic line") legitimately needs a search + locate + pin round
# PER QUEST, and prompt caching makes the extra rounds cheap. Hitting the cap
# now logs where the budget went (looplog) instead of dying silently.
MAX_ROUNDS = 24

# GPT-5-family models reject temperature != 1 with a hard 400; litellm knows
# which params each model supports, so let it drop the unsupported ones.
litellm.drop_params = True


def _extra_params(model_id: str) -> dict:
    """Per-model litellm kwargs. gpt-5.4+ reasoning models reject function
    tools on /v1/chat/completions ("use /v1/responses or reasoning_effort
    'none'"); sending an explicit reasoning_effort makes litellm bridge the
    call to /v1/responses, where tools and reasoning work together."""
    name = model_id.split("/")[-1]
    if name.startswith("gpt-5."):
        try:
            if int(name[len("gpt-5."):].split("-")[0].split(".")[0]) >= 4:
                return {"reasoning_effort": "medium"}
        except ValueError:
            pass
    return {}


def _friendly_llm_error(exc: Exception) -> str:
    """One readable line for a provider failure. litellm's raw text is a page of
    nested JSON, and an uncaught streaming exception reaches the player as a
    blank 'Network Error' — this turns both into something actionable."""
    s = str(exc)
    m = re.search(r'"message":\s*"([^"]+)"', s)
    detail = m.group(1) if m else s.splitlines()[0][:200]
    name = type(exc).__name__
    if "RateLimit" in name or "RESOURCE_EXHAUSTED" in s or "429" in s:
        # Gemini free-tier keys report limit 0 for models the tier excludes
        # (e.g. 2.5 Pro) — that's a plan gap, not a transient rate limit.
        if "free_tier" in s and re.search(r"limit:\s*0", s):
            return ("This model isn't included in your API key's free tier "
                    "(quota is 0). Pick a different model from this provider, "
                    "or enable billing on the key.")
        return f"Rate limit hit — wait a moment and try again. ({detail})"
    if "NotFound" in name or "404" in s:
        return f"The provider doesn't offer this model to your key: {detail}"
    if "Authentication" in name or "401" in s:
        return f"The API key was rejected — check it in Settings. ({detail})"
    return f"{name}: {detail}"


def _as_blocks(msg: dict) -> list | None:
    """A message's content as a content-block list (converting a plain string in
    place), or None when it can't carry a cache_control marker — Anthropic
    rejects empty text blocks, so an empty content gets no marker."""
    c = msg.get("content")
    if isinstance(c, str):
        if not c:
            return None
        msg["content"] = [{"type": "text", "text": c}]
    return msg["content"] if isinstance(msg["content"], list) else None


def _mark_system_cache(convo: list[dict]) -> None:
    """Pin the stable prefix for Anthropic prompt caching (litellm passes the
    content-block `cache_control` through). One marker on the LAST system
    message caches everything up to it — the tool schemas and every system
    block — as a single prefix, and stays within Anthropic's 4-marker limit."""
    last_sys = None
    for m in convo:
        if m.get("role") == "system":
            last_sys = m
    blocks = _as_blocks(last_sys) if last_sys else None
    if not blocks:
        return
    for b in reversed(blocks):
        if isinstance(b, dict) and b.get("type") == "text":
            b["cache_control"] = {"type": "ephemeral"}
            return


def _mark_round_cache(convo: list[dict]) -> None:
    """Move the per-round marker to the newest message before each request, so
    the next round re-reads this whole convo from cache instead of re-billing
    it. The previous round's marker is stripped first — the loop appends an
    assistant + tool messages every round, and leaving old markers would blow
    Anthropic's 4-per-request cap within a few rounds."""
    for m in convo:
        if m.get("role") == "system":
            continue          # the fixed marker from _mark_system_cache stays
        m.pop("cache_control", None)
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    last = convo[-1]
    if last.get("role") == "tool":
        # Message-level, not block-level: litellm forwards this onto the
        # translated tool_result block, and the content stays a plain string.
        last["cache_control"] = {"type": "ephemeral"}
        return
    if last.get("role") != "user":
        return                # nothing markable; the system marker still covers the prefix
    blocks = _as_blocks(last)
    if not blocks:
        return
    for b in reversed(blocks):
        if isinstance(b, dict) and b.get("type") == "text":
            b["cache_control"] = {"type": "ephemeral"}
            return


async def chat(model_id: str, messages: list[dict], ctx: dict | None = None) -> AsyncIterator[dict]:
    """Public entry: runs the turn and, no matter how it ends (done, error,
    or the player hitting Stop mid-stream), records what it cost to the
    usage ledger — real token counts when the provider reported them,
    a chars/4 estimate flagged as such when it didn't."""
    tally = {"in": 0, "out": 0, "out_chars": 0, "got_usage": False}
    try:
        async for ev in _chat_inner(model_id, messages, ctx, tally):
            yield ev
    finally:
        _record_usage(model_id, ctx, messages, tally)


def _record_usage(model_id: str, ctx: dict | None, messages: list[dict],
                  tally: dict) -> None:
    import usage as usage_ledger
    if not (tally["got_usage"] or tally["out_chars"]):
        return   # the turn died before the model produced anything
    if tally["got_usage"]:
        tin, tout, est = tally["in"], tally["out"], False
    else:
        tin = sum(len(str(m.get("content") or "")) for m in messages) // 4
        tout = tally["out_chars"] // 4
        est = True
    try:
        cin, cout = litellm.cost_per_token(
            model=model_id, prompt_tokens=tin, completion_tokens=tout)
        cost = (cin or 0.0) + (cout or 0.0)
    except Exception:
        cost = 0.0
    usage_ledger.record(
        context=(ctx or {}).get("usage_context", "chat"),
        model=model_id, auth="api",
        input_tokens=tin, output_tokens=tout,
        cost_usd=cost, estimated=est)


async def _chat_inner(model_id: str, messages: list[dict], ctx: dict | None,
                      tally: dict) -> AsyncIterator[dict]:
    provider = provider_for_model(model_id)
    if not provider:
        yield {"type": "error", "message": f"Unknown model '{model_id}'."}
        return
    if not vault.has_key(provider):
        yield {"type": "error", "message": f"No API key set for {provider}. Add one in settings."}
        return
    vault.load_into_env(provider)

    convo = list(messages)
    seen_sources: set[str] = set()
    from llm import looplog
    chat_id = (ctx or {}).get("chat_id", "")
    rounds = 0

    # Prompt caching, Anthropic only. Anthropic bills cache writes at 1.25x and
    # reads at 0.1x, and this loop re-sends the ENTIRE convo + tool schemas every
    # round — so the fixed prefix is marked once, and a second marker rides the
    # newest message each round. OpenAI and Gemini cache implicitly with no
    # request-side markup, and xAI is left untouched.
    cache_prompts = provider == "anthropic"
    if cache_prompts:
        _mark_system_cache(convo)

    for _ in range(MAX_ROUNDS):
        rounds += 1
        looplog.log(chat_id, "api", "round", n=rounds, msgs=len(convo))
        if cache_prompts:
            _mark_round_cache(convo)
        try:
            stream = await litellm.acompletion(
                model=model_id, messages=convo, tools=TOOLS,
                stream=True, temperature=0.3,
                # The final chunk then carries real token usage (drop_params
                # sheds this on providers that don't support it).
                stream_options={"include_usage": True},
                **_extra_params(model_id),
            )
        except Exception as exc:
            yield {"type": "error", "message": _friendly_llm_error(exc)}
            return

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}

        # litellm opens the HTTP request lazily, so provider errors (quota,
        # retired model, bad key) surface HERE, not at acompletion() above.
        try:
            async for chunk in stream:
                u = getattr(chunk, "usage", None)
                if u and (getattr(u, "prompt_tokens", 0) or getattr(u, "completion_tokens", 0)):
                    tally["got_usage"] = True
                    tally["in"] += getattr(u, "prompt_tokens", 0) or 0
                    tally["out"] += getattr(u, "completion_tokens", 0) or 0
                if not chunk.choices:
                    continue   # the usage-only final chunk has no choices
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    tally["out_chars"] += len(delta.content)
                    yield {"type": "token", "text": delta.content}
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
        except Exception as exc:
            yield {"type": "error", "message": _friendly_llm_error(exc)}
            return

        if not tool_calls:
            looplog.log(chat_id, "api", "done", rounds=rounds)
            yield {"type": "done"}
            return

        convo.append({
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in tool_calls.values()
            ],
        })

        for c in tool_calls.values():
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}

            # ask_user pauses the run for a real answer instead of running a tool.
            if c["name"] == "ask_user":
                ask_id, fut = interactive.create()
                yield {
                    "type": "ask", "id": ask_id,
                    "question": args.get("question", ""),
                    "options": args.get("options") or [],
                    "header": args.get("header", ""),
                }
                answer = await interactive.wait(ask_id, fut)
                convo.append({"role": "tool", "tool_call_id": c["id"],
                              "content": json.dumps({"answer": answer})})
                yield {"type": "tool_result", "name": "ask_user", "ok": True}
                continue

            # create_doc emits a chat doc-link (with content) but hands the model a
            # short result so it doesn't re-read the whole document.
            if c["name"] == "create_doc":
                res = await asyncio.to_thread(execute_tool, "create_doc", args, ctx)
                d = _safe_json(res)
                if d.get("doc_id"):
                    yield {"type": "doc", "id": d["doc_id"],
                           "title": d.get("title") or args.get("title", ""),
                           "draft": True, "content": args.get("content", "")}
                    convo.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(
                        {"ok": True, "doc_id": d["doc_id"], "note": "Draft doc created and shown to the player for review."})})
                else:
                    convo.append({"role": "tool", "tool_call_id": c["id"], "content": res})
                yield {"type": "tool_result", "name": "create_doc", "ok": bool(d.get("doc_id"))}
                continue

            yield {"type": "tool", "name": c["name"], "args": args}
            result = await asyncio.to_thread(execute_tool, c["name"], args, ctx)
            convo.append({"role": "tool", "tool_call_id": c["id"], "content": result})

            parsed = _safe_json(result)
            yield {"type": "tool_result", "name": c["name"], "ok": not parsed.get("error")}
            src, url = parsed.get("source"), parsed.get("url", "")
            if src and src not in seen_sources:
                seen_sources.add(src)
                yield {"type": "source", "label": src, "url": url}
            if parsed.get("asset_id"):
                # kind routes the asset in the UI: "map" assets (pinned maps) show in
                # the Map view, everything else lands in the Assets tab as an image.
                # url = the zone's interactive map, linked from the pinned image.
                yield {"type": "asset", "name": parsed["asset_id"],
                       "kind": parsed.get("asset_kind", "image"), "zone": parsed.get("zone", ""),
                       "url": parsed.get("interactive_url", "")}
            if parsed.get("map_url"):
                yield {"type": "map", "url": parsed["map_url"], "zone": parsed.get("zone", ""),
                       "focus": parsed.get("focus"), "pin": parsed.get("pin"),
                       # a CATEGORY of temp pins (pin_points_on_map)
                       "pins": parsed.get("pins"), "category": parsed.get("category", ""),
                       "icon": parsed.get("icon", "")}

    # Round budget exhausted. Don't throw the whole run away (24 rounds of
    # tool results = real tokens spent): force one final completion with tools
    # withheld, so the model writes up whatever it has gathered. A partial,
    # caveated answer beats "Reached tool-call limit" every time.
    looplog.log(chat_id, "api", "error", detail=f"hit MAX_ROUNDS={MAX_ROUNDS}")
    convo.append({
        "role": "user",
        "content": ("[system] Tool budget exhausted — no more tool calls are "
                    "possible. Answer the question NOW using only the "
                    "information already gathered above. Be direct; briefly "
                    "note anything you could not confirm."),
    })
    try:
        stream = await litellm.acompletion(
            # tools stay declared (tool-role history needs them on some
            # providers) but tool_choice="none" forbids another round; any
            # stray tool-call deltas are simply not read below.
            model=model_id, messages=convo, tools=TOOLS, tool_choice="none",
            stream=True, temperature=0.3,
            stream_options={"include_usage": True},
            **_extra_params(model_id),
        )
        async for chunk in stream:
            u = getattr(chunk, "usage", None)
            if u and (getattr(u, "prompt_tokens", 0) or getattr(u, "completion_tokens", 0)):
                tally["got_usage"] = True
                tally["in"] += getattr(u, "prompt_tokens", 0) or 0
                tally["out"] += getattr(u, "completion_tokens", 0) or 0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                tally["out_chars"] += len(delta.content)
                yield {"type": "token", "text": delta.content}
    except Exception as exc:
        yield {"type": "error", "message": _friendly_llm_error(exc)}
        return
    looplog.log(chat_id, "api", "done", rounds=rounds, forced=True)
    yield {"type": "done"}


def _safe_json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}
