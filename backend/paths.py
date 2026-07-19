"""Filesystem locations for app data.

Dev: the repo root, so profile/knowledge live alongside the code.
Packaged (frozen exe): a per-user, writable app-data folder, since the install
directory (e.g. Program Files) is read-only. Override either with FFXIV_DATA_DIR.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    # The "EorzeaAssistant" folder name is kept across the rename to "Aether
    # Intelligence" so existing chats/attachments aren't orphaned.
    if os.environ.get("FFXIV_DATA_DIR"):
        return Path(os.environ["FFXIV_DATA_DIR"])
    if getattr(sys, "frozen", False):  # running inside a packaged app
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "EorzeaAssistant"
        if sys.platform.startswith("win"):
            base = os.environ.get("LOCALAPPDATA")
            return (Path(base) if base else Path.home()) / "EorzeaAssistant"
        base = os.environ.get("XDG_DATA_HOME")  # linux
        return (Path(base) if base else Path.home() / ".local/share") / "EorzeaAssistant"
    return REPO_ROOT


DATA_DIR = _default_data_dir()

PROFILE_DIR = DATA_DIR / "profile"                # one dir per workspace + _index.json
LEGACY_PROFILE_PATH = PROFILE_DIR / "player.md"   # pre-workspaces single profile (migrated)
# Cross-profile context the assistant reads in EVERY workspace (server/region,
# answer style…). This replaced the old "global" workspace: that workspace's
# profile.md is migrated into this file, so the shared context survives.
SHARED_PROFILE_PATH = PROFILE_DIR / "_shared.md"
# Standing agent-behaviour preferences ("from now on, always/never …"), applied to
# every chat. Lives under profile/ so it's covered by the same gitignore rule.
PREFERENCES_PATH = PROFILE_DIR / "preferences.md"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
CHATS_DIR = DATA_DIR / "data" / "chats"          # one folder per chat: messages + assets
ASSETS_DIRNAME = "assets"
# LEGACY: the slug of the old "global" workspace. It no longer exists as a workspace —
# every chat now belongs to a character profile, and cross-profile context lives in
# SHARED_PROFILE_PATH. Kept only so the migration can find and retire the old data.
GLOBAL_SLUG = "global"

for d in (KNOWLEDGE_DIR, CHATS_DIR, PROFILE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def chat_dir(chat_id: str) -> Path:
    d = CHATS_DIR / chat_id
    (d / ASSETS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return d


# --- profile workspace paths ---
def workspace_index_path() -> Path:
    return PROFILE_DIR / "_index.json"


def profile_dir(slug: str) -> Path:
    d = PROFILE_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_md(slug: str) -> Path:
    return profile_dir(slug) / "profile.md"


def character_json(slug: str) -> Path:
    return profile_dir(slug) / "character.json"


def workspace_settings_path(slug: str) -> Path:
    return profile_dir(slug) / "settings.json"
