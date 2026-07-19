"""Interactive clarifying-questions plumbing.

Lets the agent pause mid-run to ask the player a question and wait for the answer,
Claude-style. The engine emits an `ask` event (question + option buttons) and then
awaits a Future; the frontend POSTs the answer to /chat/answer, which resolves the
Future so the same streaming turn resumes exactly where it left off.

Both engines share this: the API loop yields the ask event directly; the Agent-SDK
path emits it through its event queue from the `ask_user` MCP tool. The registry is
keyed by a short ask id and lives for the life of the process (single uvicorn loop).
"""
from __future__ import annotations

import asyncio
import uuid

# ask_id -> Future that resolves to the user's answer string.
_pending: dict[str, asyncio.Future] = {}

# How long to wait for an answer before the agent proceeds on its own.
ANSWER_TIMEOUT_S = 900


def create() -> tuple[str, asyncio.Future]:
    """Register a new pending question. Returns (ask_id, future)."""
    ask_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[ask_id] = fut
    return ask_id, fut


def submit(ask_id: str, answer: str) -> bool:
    """Resolve a pending question with the user's answer. Returns False if unknown."""
    fut = _pending.pop(ask_id, None)
    if fut is None or fut.done():
        return False
    fut.get_loop().call_soon_threadsafe(fut.set_result, answer)
    return True


def discard(ask_id: str) -> None:
    _pending.pop(ask_id, None)


async def wait(ask_id: str, fut: asyncio.Future, timeout: float = ANSWER_TIMEOUT_S) -> str:
    """Await the answer, with a safety timeout so a closed tab never hangs the agent."""
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        discard(ask_id)
        return "(no answer received — proceed using your best judgment)"
    except asyncio.CancelledError:
        discard(ask_id)
        raise
