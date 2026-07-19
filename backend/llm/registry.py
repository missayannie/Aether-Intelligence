"""Model registry: what the UI can offer, and via which auth.

Each Anthropic model can run two ways — the Pro/Max subscription (if a token is
stored) or a billed API key (if a key is stored). Other providers are API-key
only. The registry surfaces the available auth modes per model so the picker can
show them and default sensibly (subscription preferred — free at point of use).
"""
from __future__ import annotations

from config import MODEL_CATALOG
from keys import vault
from subscription import has_oauth_token


def _auth_options(provider: str) -> list[str]:
    opts: list[str] = []
    if provider == "anthropic" and has_oauth_token():
        opts.append("subscription")
    if vault.has_key(provider):
        opts.append("api")
    return opts


def _pricing(model_id: str) -> dict:
    """Per-token prices for a model, straight from litellm.

    Read from litellm rather than hardcoded here BECAUSE litellm is what actually
    bills the request — a table of our own would drift from the real charge the first
    time a price changed. Missing entries return zeros, and the UI treats a zero
    price as "unknown" rather than as "free".
    """
    try:
        import litellm

        key = model_id.split("/", 1)[1] if "/" in model_id else model_id
        d = litellm.model_cost.get(key) or litellm.model_cost.get(model_id) or {}
        return {
            "input_cost_per_token": d.get("input_cost_per_token") or 0.0,
            "output_cost_per_token": d.get("output_cost_per_token") or 0.0,
        }
    except Exception:
        return {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0}


def available_models() -> list[dict]:
    out = []
    for provider, meta in MODEL_CATALOG.items():
        auths = _auth_options(provider)
        for m in meta["models"]:
            out.append({
                "provider": provider,
                "provider_label": meta["label"],
                "id": m["id"],
                "label": m["label"],
                "tool_use": m["tool_use"],
                "recommended": m.get("recommended", False),
                "available": bool(auths),
                "auth_options": auths,
                "default_auth": auths[0] if auths else None,  # subscription first
                **_pricing(m["id"]),
            })
    return out


def default_model() -> dict | None:
    """Recommended available model as {id, auth}. Prefers subscription."""
    models = available_models()
    for m in models:
        if m["available"] and m["recommended"]:
            return {"id": m["id"], "auth": m["default_auth"]}
    for m in models:
        if m["available"]:
            return {"id": m["id"], "auth": m["default_auth"]}
    return None


def provider_for_model(model_id: str) -> str | None:
    for provider, meta in MODEL_CATALOG.items():
        if any(m["id"] == model_id for m in meta["models"]):
            return provider
    return None
