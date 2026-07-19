"""Engine dispatcher.

Routes a chat turn to the right engine based on the chosen model and auth mode:
  - auth == "subscription"  -> agent_engine (Claude Agent SDK, subscription)
  - otherwise               -> client       (litellm, billed API key)

Both are async generators yielding the same event dicts, so callers don't care
which ran. This is the single seam between the two auth worlds.
"""
from __future__ import annotations

from typing import AsyncIterator

from llm import client as api_engine
from llm import agent_engine
from llm.registry import provider_for_model


async def run(model_id: str, auth: str, messages: list[dict], ctx: dict | None = None) -> AsyncIterator[dict]:
    if auth == "subscription":
        if provider_for_model(model_id) != "anthropic":
            yield {"type": "error", "message": "Subscription auth is only available for Claude models."}
            return
        async for ev in agent_engine.chat(model_id, messages, ctx):
            yield ev
    else:
        async for ev in api_engine.chat(model_id, messages, ctx):
            yield ev
