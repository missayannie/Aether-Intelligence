"""Claude subscription support.

Lets the app run Claude via the user's Pro/Max subscription instead of a billed
API key. The user runs `claude setup-token` once (generates a ~1-year OAuth
token), pastes it into settings; we store it in the OS keychain and hand it to
the Claude Agent SDK via CLAUDE_CODE_OAUTH_TOKEN.

The Agent SDK runs on the Claude Code CLI runtime, so we also locate that binary
(it isn't always on PATH — the desktop app bundles it elsewhere).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from keys import vault

# Credential id under which the OAuth token is stored in the keychain.
OAUTH_CREDENTIAL = "anthropic_sub"
SETUP_COMMAND = "claude setup-token"


def get_oauth_token() -> str | None:
    return vault.get_credential(OAUTH_CREDENTIAL)


def set_oauth_token(token: str) -> None:
    vault.set_credential(OAUTH_CREDENTIAL, token)


def delete_oauth_token() -> None:
    vault.delete_credential(OAUTH_CREDENTIAL)


def has_oauth_token() -> bool:
    return get_oauth_token() is not None


def _native_windows_cli() -> str | None:
    """The native Claude Code CLI executable on Windows.

    The `claude` command on Windows is a chain of wrappers:
    `claude.cmd` -> cmd.exe -> `claude-launcher.ps1` (PowerShell) -> the real
    `claude.exe`. Each shell layer re-escapes arguments, which MANGLES the JSON
    passed to `--mcp-config` (SDK MCP servers) — the CLI then reads the corrupted
    value as a file path ("MCP config file not found"), and the SDK init hangs
    with "Control request timeout: initialize". Spawning the native .exe directly
    (via CreateProcess, no shell) passes arguments through intact.

    The launcher resolves the exe under %APPDATA%\\Claude\\claude-code\\<version>;
    pick the newest version there.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "Claude" / "claude-code"
    if not base.is_dir():
        return None

    def _ver_key(p: Path):
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (0,)

    versioned = sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name[:1].isdigit()),
        key=_ver_key,
    )
    for d in reversed(versioned):
        exe = d / "claude.exe"
        if exe.exists():
            return str(exe)
    return None


def claude_cli_path() -> str | None:
    """Locate the Claude Code CLI the Agent SDK runs on.

    On Windows, prefer the native `claude.exe` (bypassing the .cmd/.ps1 wrappers
    that mangle JSON args — see _native_windows_cli). Elsewhere, POSIX shell
    launchers pass args through cleanly, so PATH + known locations are fine.
    Returns an absolute path, or None if not found.
    """
    if sys.platform.startswith("win"):
        native = _native_windows_cli()
        if native:
            return native

    # Prefer the Claude Code CLI on PATH — but NEVER the desktop GUI app
    # (…/AnthropicClaude/claude.exe). Driving the GUI as a CLI hangs the Agent
    # SDK with "Control request timeout: initialize".
    on_path = shutil.which("claude")
    if on_path and not _is_desktop_gui(on_path):
        return on_path

    home = Path.home()
    candidates = [
        home / "AppData/Local/ClaudeCodeBin/claude.cmd",   # Windows Claude Code CLI (wrapper)
        home / "AppData/Roaming/npm/claude.cmd",           # npm global (win)
        home / ".claude/local/claude",                     # user-local
        Path("/opt/homebrew/bin/claude"),                  # macOS (Apple Silicon brew)
        Path("/usr/local/bin/claude"),                     # macOS/Linux
        home / ".local/bin/claude",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _is_desktop_gui(path: str) -> bool:
    """The Claude *desktop app* is not the CLI; spawning it hangs the SDK."""
    return "anthropicclaude" in path.replace("\\", "/").lower() or "Claude.app" in path


def full_env_path() -> str:
    """The full persistent PATH from the Windows registry, merged with the current
    PATH. The Claude CLI launcher is a .cmd -> .ps1 -> node chain, so it needs
    PowerShell + Node on PATH. A packaged app often inherits a stale PATH (missing
    Node/ClaudeCodeBin added after the last login), which makes the Agent SDK hang
    with "Control request timeout: initialize". Rebuilding PATH from the registry
    guarantees those are present. On non-Windows, returns the current PATH.
    """
    if not sys.platform.startswith("win"):
        return os.environ.get("PATH", "")
    import winreg

    parts: list[str] = []
    for root, sub in [
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ]:
        try:
            with winreg.OpenKey(root, sub) as k:
                val, _ = winreg.QueryValueEx(k, "Path")
                parts.append(os.path.expandvars(val))
        except OSError:
            pass
    parts.append(os.environ.get("PATH", ""))

    seen: set[str] = set()
    out: list[str] = []
    for p in ";".join(parts).split(";"):
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ";".join(out)


def status() -> dict:
    """Report readiness of the subscription path for the UI."""
    cli = claude_cli_path()
    return {
        "cli_found": cli is not None,
        "cli_path": cli,
        "token_set": has_oauth_token(),
        "ready": cli is not None and has_oauth_token(),
        "setup_command": SETUP_COMMAND,
    }
