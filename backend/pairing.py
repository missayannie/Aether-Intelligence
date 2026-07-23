"""Companion-device pairing — lets an iOS app drive THIS running backend.

The phone talks to the desktop's own local backend over the LAN / a Tailscale
network, so it keeps every capability the desktop has (game-client data,
subscription auth, your keys) with no cloud and no second copy of anything.

Security model (see docs — QR pairing flow):

  * The backend binds off-loopback ONLY when companion access is enabled in
    Settings; default is 127.0.0.1 (see run_backend.py). This toggle is the real
    boundary — the QR is just the handoff.
  * Pairing uses a single-use, ~120s CODE shown as a QR on the desktop. The phone
    trades the code for its OWN long-lived device TOKEN, stored here only as a
    SHA-256 hash. A photographed QR is worthless after one claim or expiry.
  * Every non-loopback request must carry a valid device token; the loopback
    desktop app is unaffected. Enforcement lives in app.py's gate middleware.

Codes live in memory; tokens are stored hashed. Revoking a device deletes its
record, so its next request 401s.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from paths import DATA_DIR

# Optional at import time: a missing segno must never stop the backend booting —
# pairing still works from the code/URI, just without the rendered QR image.
try:
    import segno
except ImportError:
    segno = None

_FILE = DATA_DIR / "companion.json"
_SETTINGS = DATA_DIR / "app_settings.json"
_LOCK = threading.Lock()

CODE_TTL_SEC = 120
DEFAULT_PORT = 8756

# code -> expiry epoch. Single-use (claiming pops it), in memory only.
_codes: dict[str, float] = {}
# device_id -> last epoch we flushed last_seen, so a chatty phone doesn't rewrite
# the file on every request.
_last_flush: dict[str, float] = {}
# host -> recent claim attempt epochs, for a light brute-force cap on /pair/claim.
_claim_hits: dict[str, list[float]] = {}
# (epoch, hosts) — host discovery shells out to tailscale, so cache it briefly.
_hosts_cache: tuple[float, list[str]] = (0.0, [])
_server_id: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        v = json.loads(_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(v, dict):
            v.setdefault("devices", [])
            return v
    except (OSError, ValueError):
        pass
    return {"server_id": "", "devices": []}


def _save(state: dict) -> None:
    _FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- identity

def server_id() -> str:
    """Stable per-install id so a paired phone can pin the desktop it trusts."""
    global _server_id
    if _server_id:
        return _server_id
    with _LOCK:
        st = _load()
        if not st.get("server_id"):
            st["server_id"] = uuid.uuid4().hex
            _save(st)
        _server_id = st["server_id"]
        return _server_id


def server_name() -> str:
    try:
        return socket.gethostname() or "Aether desktop"
    except OSError:
        return "Aether desktop"


def companion_enabled() -> bool:
    """Mirrors the Settings toggle (app_settings.json), read live so the gate
    reflects the current choice without a backend restart."""
    try:
        # utf-8-sig tolerates a stray BOM (matches the other JSON loaders here).
        s = json.loads(_SETTINGS.read_text(encoding="utf-8-sig"))
        return bool(s.get("companion_enabled"))
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------- host discovery

def _port() -> int:
    try:
        return int(os.environ.get("FFXIV_BACKEND_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def _lan_ip() -> str | None:
    """Primary outward-facing LAN IPv4. No traffic is sent — connecting a UDP
    socket just makes the OS pick the interface it would route through."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, unroutable
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _tailscale() -> tuple[str | None, str | None]:
    """(MagicDNS name, 100.x IPv4) if Tailscale is up — best-effort, short timeout."""
    def run(args: list[str]) -> str:
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=1.5)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    ip = next((ln.strip() for ln in run(["tailscale", "ip", "-4"]).splitlines()
               if ln.strip()), "")
    name = ""
    status = run(["tailscale", "status", "--json"])
    if status:
        try:
            name = (json.loads(status).get("Self", {}) or {}).get("DNSName", "").rstrip(".")
        except ValueError:
            pass
    return (name or None, ip or None)


def hosts() -> list[str]:
    """host:port candidates for the QR, most-reachable first: Tailscale name,
    Tailscale IP, then LAN IP. Cached ~10s (Tailscale discovery shells out)."""
    global _hosts_cache
    now = time.time()
    if now - _hosts_cache[0] < 10 and _hosts_cache[1]:
        return _hosts_cache[1]
    p = _port()
    out: list[str] = []
    ts_name, ts_ip = _tailscale()
    if ts_name:
        out.append(f"{ts_name}:{p}")
    if ts_ip:
        out.append(f"{ts_ip}:{p}")
    lan = _lan_ip()
    if lan:
        out.append(f"{lan}:{p}")
    _hosts_cache = (now, out)
    return out


# --------------------------------------------------------------- pairing codes

def _sweep_codes() -> None:
    now = time.time()
    for c in [c for c, exp in _codes.items() if exp < now]:
        _codes.pop(c, None)


def start() -> dict:
    """Desktop-side: mint a single-use code and the QR payload to render."""
    _sweep_codes()
    code = secrets.token_urlsafe(24)
    exp = time.time() + CODE_TTL_SEC
    _codes[code] = exp
    host_list = hosts()
    payload = {
        "v": 1,
        "name": server_name(),
        "sid": server_id(),
        "hosts": host_list,
        "code": code,
        "exp": int(exp),
    }
    qs = (
        f"v=1&name={quote(payload['name'])}&sid={payload['sid']}"
        f"&hosts={quote(','.join(host_list))}&code={code}&exp={int(exp)}"
    )
    uri = f"aether://pair?{qs}"   # deep link the iOS camera can also open
    return {
        "code": code,
        "expires_in": CODE_TTL_SEC,
        "hosts": host_list,
        "payload": payload,
        "uri": uri,
        "qr": _qr_data_uri(uri),   # SVG data URI, "" if segno is unavailable
    }


def _qr_data_uri(text: str) -> str:
    """An SVG data URI of `text` as a QR, or "" if segno isn't available. Dark
    modules on a white field with a quiet zone — high contrast so it scans on any
    app theme background."""
    if segno is None:
        return ""
    try:
        qr = segno.make(text, error="m")
        return qr.svg_data_uri(scale=5, border=3, dark="#0b1220", light="#ffffff")
    except Exception:
        return ""


class PairingError(Exception):
    pass


def allow_claim(host: str, limit: int = 10, window: float = 60.0) -> bool:
    """Light per-host cap on claim attempts — codes are single-use and 192-bit,
    so this is only defense in depth."""
    now = time.time()
    hits = [t for t in _claim_hits.get(host, []) if now - t < window]
    hits.append(now)
    _claim_hits[host] = hits
    return len(hits) <= limit


def claim(code: str, device_name: str, device_id: str) -> dict:
    """Phone-side: trade a valid code for a per-device token."""
    _sweep_codes()
    # Single-use: the code is gone whether or not it turns out to be valid-timed.
    exp = _codes.pop(code, None)
    if exp is None or exp < time.time():
        raise PairingError("This pairing code is invalid or has expired.")
    token = secrets.token_urlsafe(32)
    dev = {
        "id": device_id or uuid.uuid4().hex[:12],
        "name": (device_name or "Companion device").strip()[:60],
        "token_hash": _hash_token(token),
        "created_at": _now_iso(),
        "last_seen": _now_iso(),
    }
    with _LOCK:
        st = _load()
        # Re-pairing the same device replaces its token rather than duplicating it.
        st["devices"] = [d for d in st["devices"] if d.get("id") != dev["id"]]
        st["devices"].append(dev)
        _save(st)
    return {
        "token": token,
        "server_id": server_id(),
        "server_name": server_name(),
        "device_id": dev["id"],
    }


def verify_token(token: str) -> dict | None:
    """Return the device (minus its hash) for a valid token, stamping last_seen
    at most once per 30s; else None. Constant-time compare against each hash."""
    if not token:
        return None
    h = _hash_token(token)
    now = time.time()
    with _LOCK:
        st = _load()
        for d in st["devices"]:
            if hmac.compare_digest(d.get("token_hash", ""), h):
                dev_id = d.get("id", "")
                if now - _last_flush.get(dev_id, 0) > 30:
                    d["last_seen"] = _now_iso()
                    _save(st)
                    _last_flush[dev_id] = now
                return {k: v for k, v in d.items() if k != "token_hash"}
    return None


def list_devices() -> list[dict]:
    with _LOCK:
        st = _load()
        return [{k: v for k, v in d.items() if k != "token_hash"} for d in st["devices"]]


def revoke(device_id: str) -> bool:
    with _LOCK:
        st = _load()
        kept = [d for d in st["devices"] if d.get("id") != device_id]
        if len(kept) == len(st["devices"]):
            return False
        st["devices"] = kept
        _save(st)
    _last_flush.pop(device_id, None)
    return True
