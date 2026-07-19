"""API-key vault backed by the OS keychain.

Keys are stored in Windows Credential Manager / macOS Keychain via `keyring` —
never in a plaintext file, never logged, never sent anywhere except the provider
they belong to. The rest of the app only ever asks "do we have a key for X?"
(via has_key) and, at call time, loads it into the environment for litellm.
"""
from __future__ import annotations

import logging
import os

import keyring
import keyring.errors

from config import MODEL_CATALOG

log = logging.getLogger(__name__)

# Keychain namespace. Intentionally kept as the original name across the app
# rename to "Aether Intelligence" — changing it would orphan already-stored API
# keys and the subscription token.
SERVICE = "EorzeaAssistant"

# Message shown when the OS keychain backend can't be reached — usually a packaging
# gap (missing keyring/win32ctypes/pywin32 native deps) on some machines.
_NO_BACKEND = (
    "Secure storage (the OS keychain) isn't available on this system, so the key "
    "couldn't be saved. This is usually a missing dependency in the app build — "
    "please report it."
)


def _write(name: str, value: str) -> None:
    """Store a secret, turning any keychain-backend failure into a clear, catchable
    ValueError (the API layer returns it as a 400 with guidance) instead of a raw 500."""
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.KeyringError as e:
        log.warning("keyring set failed for %s: %s", name, e)
        raise ValueError(f"{_NO_BACKEND} ({type(e).__name__}: {e})") from e
    except Exception as e:  # e.g. a native import error from a broken backend
        log.warning("keyring set errored for %s: %s", name, e)
        raise ValueError(f"{_NO_BACKEND} ({type(e).__name__}: {e})") from e


def _read(name: str) -> str | None:
    """Read a secret; return None (rather than raising) if the backend is unavailable,
    so the app degrades to 'no key set' instead of crashing on every read."""
    try:
        return keyring.get_password(SERVICE, name)
    except Exception as e:  # noqa: BLE001 — any backend failure means "no key"
        log.warning("keyring read failed for %s: %s", name, e)
        return None


def set_key(provider: str, api_key: str) -> None:
    """Store (or replace) a provider's API key in the OS keychain."""
    _validate(provider)
    if not api_key or not api_key.strip():
        raise ValueError("Refusing to store an empty API key.")
    _write(provider, api_key.strip())


def get_key(provider: str) -> str | None:
    """Read a provider's key from the keychain (or None if unset/unavailable)."""
    _validate(provider)
    return _read(provider)


def delete_key(provider: str) -> None:
    """Remove a provider's key from the keychain."""
    _validate(provider)
    try:
        keyring.delete_password(SERVICE, provider)
    except keyring.errors.PasswordDeleteError:
        pass  # already absent — idempotent
    except Exception as e:  # noqa: BLE001 — backend unavailable; nothing to remove
        log.warning("keyring delete failed for %s: %s", provider, e)


def has_key(provider: str) -> bool:
    return get_key(provider) is not None


def load_into_env(provider: str) -> None:
    """Set the provider's env var (e.g. ANTHROPIC_API_KEY) for the duration of a
    call. litellm reads keys from the environment; we populate it just-in-time so
    the secret never lives on disk."""
    key = get_key(provider)
    if key:
        os.environ[MODEL_CATALOG[provider]["env_key"]] = key


def _validate(provider: str) -> None:
    if provider not in MODEL_CATALOG:
        raise ValueError(f"Unknown provider '{provider}'. Known: {list(MODEL_CATALOG)}")


# --- Generic credential storage (for non-provider secrets, e.g. the Claude
# subscription OAuth token). Same keychain, arbitrary credential id. ---
def set_credential(credential_id: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError("Refusing to store an empty credential.")
    _write(credential_id, value.strip())


def get_credential(credential_id: str) -> str | None:
    return _read(credential_id)


def delete_credential(credential_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, credential_id)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as e:  # noqa: BLE001 — backend unavailable; nothing to remove
        log.warning("keyring delete failed for %s: %s", credential_id, e)
