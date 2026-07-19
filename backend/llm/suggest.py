"""Follow-up suggestion generator.

After a reply, propose a few short things the PLAYER might want to say next
(Claude-style suggestion chips). One cheap, non-streaming, tool-free model call.
Works on both engines; returns a small list of <=5-word strings (never raises).
"""
from __future__ import annotations

import json
import os
from typing import List

from llm.registry import provider_for_model
from keys import vault

_SYS = (
    "You propose what the USER might say next in a chat with a Final Fantasy XIV "
    "assistant. Given the conversation, output ONLY a JSON array of exactly 3 short "
    "follow-up messages the user could send next — each 5 words or fewer, written "
    "in the user's voice (e.g. \"Yes, continue\", \"Show me on the map\", "
    "\"What gear should I get?\"). Output the JSON array and nothing else."
)
_ASK = "Suggest 3 follow-ups I might send next. JSON array of short strings only."

# Chips are three five-word throwaway strings — never worth the chat model. The
# subscription path is Anthropic-only, so the cheapest Claude is hardcoded.
_SUB_MODEL = "claude-haiku-4-5"


def _cheapest(model: str) -> str:
    """Cheapest same-provider model for this call. Same PROVIDER because that's
    the API key we know exists; cheapest because suggestion chips need no
    reasoning depth. Priced via the registry (i.e. litellm — what actually
    bills), falling back to the chat's own model when no price is known."""
    from config import MODEL_CATALOG
    from llm.registry import _pricing

    provider = provider_for_model(model)
    if not provider:
        return model
    best, best_cost = model, float("inf")
    for m in MODEL_CATALOG.get(provider, {}).get("models", []):
        p = _pricing(m["id"])
        cost = p["input_cost_per_token"] + p["output_cost_per_token"]
        if 0 < cost < best_cost:
            best, best_cost = m["id"], cost
    return best


def _parse(text: str) -> List[str]:
    text = (text or "").strip()
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(text[i : j + 1])
    except (json.JSONDecodeError, TypeError):
        return []
    out: List[str] = []
    for s in arr:
        s = " ".join(str(s).split()[:5]).strip()
        if s and s not in out:
            out.append(s)
    return out[:3]


async def followups(model: str, auth: str, messages: list[dict]) -> list[str]:
    convo = [m for m in messages if m.get("role") in ("user", "assistant")][-6:]
    if not convo:
        return []
    try:
        if auth == "subscription":
            return await _sub(_SUB_MODEL, convo)
        return await _api(_cheapest(model), convo)
    except Exception:  # noqa: BLE001 — suggestions are best-effort
        return []


async def _api(model: str, convo: list[dict]) -> list[str]:
    import litellm

    litellm.drop_params = True  # gpt-5 family rejects temperature != 1

    provider = provider_for_model(model)
    if not provider or not vault.has_key(provider):
        return []
    vault.load_into_env(provider)
    msgs = [{"role": "system", "content": _SYS}, *convo, {"role": "user", "content": _ASK}]
    resp = await litellm.acompletion(
        model=model, messages=msgs, stream=False, temperature=0.4, max_tokens=120
    )
    try:
        import usage as usage_ledger
        u = getattr(resp, "usage", None)
        if u:
            cin, cout = litellm.cost_per_token(
                model=model, prompt_tokens=u.prompt_tokens or 0,
                completion_tokens=u.completion_tokens or 0)
            usage_ledger.record(
                context="suggestions", model=model, auth="api",
                input_tokens=u.prompt_tokens or 0,
                output_tokens=u.completion_tokens or 0,
                cost_usd=(cin or 0) + (cout or 0), estimated=False)
    except Exception:
        pass
    return _parse(resp.choices[0].message.content or "")


async def _sub(model: str, convo: list[dict]) -> list[str]:
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock
    from subscription import get_oauth_token, claude_cli_path, full_env_path
    from paths import DATA_DIR

    token, cli = get_oauth_token(), claude_cli_path()
    if not token or not cli:
        return []
    transcript = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in convo)
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "ANTHROPIC_API_KEY": "",
           "PATH": full_env_path()}
    opts = ClaudeAgentOptions(
        system_prompt=_SYS, model=model or _SUB_MODEL, cli_path=cli, env=env,
        cwd=str(DATA_DIR), setting_sources=[], max_turns=1, load_timeout_ms=60000,
    )
    text = ""
    async for m in query(prompt=f"{transcript}\n\n{_ASK}", options=opts):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    text += b.text
    return _parse(text)
