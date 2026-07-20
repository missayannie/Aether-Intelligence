"""FastAPI backend for Aether Intelligence.

Runs as a local server (127.0.0.1) that the Tauri frontend talks to over HTTP.
Keeping all logic here (not in Rust) means the same backend can later power a
standalone web build. Endpoints:

  GET  /health                      liveness
  GET  /models                      model catalog + which have keys + default
  GET  /keys                        which providers have a stored key
  POST /keys/{provider}             store a key (body: {api_key})
  DEL  /keys/{provider}             remove a key
  GET  /profile  / PUT /profile     read/update player.md
  GET  /chats    / POST /chats      list / create chats
  GET  /chats/{id}                  full chat (messages)
  POST /chat                        stream a response (SSE)
  GET  /chats/{id}/assets/{name}    serve a chat asset (annotated image)
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from config import MODEL_CATALOG, OTHER_SOURCES
from paths import chat_dir, CHATS_DIR, ASSETS_DIRNAME, DATA_DIR
from keys import vault
from llm import registry
from llm.dispatch import run as run_chat
import attachments
from prompts import OVERLAY_SYSTEM, build_system_prompt
import subscription
import workspaces
from sources.lodestone_character import CharacterClient, LodestoneBlocked
from annotate.annotate import annotate, spec_from_dict

_characters = CharacterClient()  # shared scraper (mirrors tools.py's shared clients)
from sources import garland as _garland  # noqa: E402  (the app's item database)

app = FastAPI(title="Aether Intelligence")

# Local desktop app: the frontend runs from a tauri:// origin or the Vite dev
# server. Allow localhost origins.
app.add_middleware(
    CORSMiddleware,
    # Local dev (Vite), Tauri dev, and packaged Tauri (tauri.localhost on Windows,
    # tauri://localhost on macOS). The server binds to 127.0.0.1 so only local
    # processes can reach it regardless.
    allow_origin_regex=(
        r"^(https?://localhost(:\d+)?|https?://127\.0\.0\.1(:\d+)?"
        r"|tauri://localhost|https?://tauri\.localhost)$"
    ),
    allow_methods=["*"], allow_headers=["*"],
)


# ---------- health / models / keys ----------
@app.on_event("startup")
async def _migrate_workspaces():
    """One-time upgrade to profile workspaces (global commons + owner-tagged chats)."""
    workspaces.ensure_migration()


@app.on_event("startup")
async def _expire_cache_on_patch():
    """Purge patch-sensitive caches when the game data has changed since last launch.

    This is what lets the TTLs stay long. Cached game facts don't rot with time, they
    rot when the game patches — so we key freshness off XIVAPI's game-data hash
    rather than a clock. Runs in the background: it's one HTTP call, but startup must
    never wait on a third party.
    """
    async def check():
        from sources import gameversion
        try:
            r = await asyncio.to_thread(gameversion.purge_if_patched)
            if r["patched"]:
                print(f"[cache] patch {r['from']} -> {r['to']}: "
                      f"dropped {r['removed']} cached entries")
        except Exception:
            pass          # unreachable XIVAPI must never block the app

    asyncio.create_task(check())


@app.on_event("startup")
async def _gameclient_init():
    """Build the local game-data index (background), and keep watching the
    client version so a patch applied WHILE the app is open refreshes content
    within minutes — no restart. The watch is two tiny file reads."""
    from sources import gameclient, gameversion

    def _boot():
        try:
            gameclient.ensure_index(background=False)
        except Exception:
            pass          # no client / bad schema -> network fallbacks serve
        try:
            from sources import supplemental
            supplemental.ensure_downloaded(background=False)
        except Exception:
            pass          # offline first run -> patch tags empty until next launch

    asyncio.create_task(asyncio.to_thread(_boot))

    async def watch():
        last = gameclient.version()
        while True:
            await asyncio.sleep(600)
            try:
                now = gameclient.version()
                if now and now != last:
                    last = now
                    print(f"[gameclient] client patched -> {now.split('+')[0]}; refreshing")
                    await asyncio.to_thread(gameversion.purge_if_patched)
            except Exception:
                pass

    asyncio.create_task(watch())


@app.on_event("startup")
async def _schedule_news_pull():
    """Warm the Lodestone news cache on launch and once a day thereafter."""
    from sources.lodestone import LodestoneClient

    async def loop():
        client = LodestoneClient()
        while True:
            try:
                await asyncio.to_thread(client.refresh)
            except Exception:
                pass
            await asyncio.sleep(24 * 3600)

    asyncio.create_task(loop())


# How old a character import may be before a launch re-scrapes it.
PROFILE_STALE_AFTER = timedelta(hours=24)
# Gap between characters during the startup sweep. The Lodestone's bot blocker
# triggers on BURSTS, and each character already costs ~15 requests (gear +
# collections) — so profiles are refreshed one at a time, spaced out, never in
# parallel.
PROFILE_REFRESH_GAP = 5.0


def _profile_is_stale(slug: str) -> bool:
    """True when this profile's character has never synced, or synced > 24h ago.

    Reads synced_at off the saved character. An import from before that field
    existed has no timestamp — treat it as stale so it gets picked up once.
    """
    existing = workspaces.get_character(slug) or {}
    stamp = existing.get("synced_at")
    if not stamp:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(stamp) > PROFILE_STALE_AFTER
    except ValueError:
        return True


@app.on_event("startup")
async def _refresh_profiles_on_start():
    """Re-import each bound character at launch, if the player enabled it.

    Runs in the BACKGROUND: never block startup on the Lodestone, which is slow and
    can be WAF-blocked outright. Only stale profiles (>24h) are touched, so
    restarting the app repeatedly doesn't hammer the site.
    """
    async def sweep():
        settings = get_settings()
        if not settings.get("refresh_profile_on_start", True):
            return
        for w in workspaces.list_workspaces():
            slug = w.get("slug") or ""
            if not slug:
                continue
            # Local gearsets first: no network, no rate limit, and it covers every job
            # rather than only the last logout. Runs even when the Lodestone refresh
            # is skipped as fresh — the file changes when you save a set, not on a
            # 24h clock.
            try:
                await asyncio.to_thread(gearsets_import, slug)
            except Exception:
                pass          # no game on this PC / format moved — Lodestone still covers it
            if not (w.get("character_id") and _profile_is_stale(slug)):
                continue
            try:
                await _bind(slug, w["character_id"])
            except Exception:
                pass          # offline / WAF / renamed character — keep the old profile
            await asyncio.sleep(PROFILE_REFRESH_GAP)

    asyncio.create_task(sweep())


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/models")
def models():
    # system_tokens matters for cost: the system prompt is re-sent on EVERY turn, and
    # it's the single biggest fixed input cost (~5k tokens). An estimate that ignores
    # it understates a short chat badly.
    try:
        system_tokens = len(build_system_prompt()) // 4
    except Exception:
        system_tokens = 0
    return {
        "models": registry.available_models(),
        "default": registry.default_model(),
        "system_tokens": system_tokens,
    }


@app.get("/usage/summary")
def usage_summary():
    """Lifetime agent-API spend, split billed vs subscription-covered and
    broken down by model × context (chat / doc threads / suggestions)."""
    import usage
    return usage.summary()


@app.get("/sources")
def sources():
    """The projects this app is built on, with their own funding pages.

    Surfaced so the Sources tab can link "Support" beside a citation. Most of these
    are volunteer community projects we use for free; if this app ever asks for a
    coffee, the people whose data it runs on should be one click away.
    """
    from config import WIKIS
    out = []
    for sid, meta in {**WIKIS, **OTHER_SOURCES}.items():
        out.append({"id": sid, "label": meta["label"],
                    "url": meta.get("url", ""), "support": meta.get("support", "")})
    return {"sources": out}


@app.get("/keys")
def list_keys():
    return {p: vault.has_key(p) for p in MODEL_CATALOG}


class KeyBody(BaseModel):
    api_key: str


@app.post("/keys/{provider}")
def set_key(provider: str, body: KeyBody):
    try:
        vault.set_key(provider, body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/keys/{provider}")
def delete_key(provider: str):
    vault.delete_key(provider)
    return {"ok": True}


# ---------- Claude subscription ----------
@app.get("/subscription/status")
def subscription_status():
    return subscription.status()


@app.get("/subscription/selftest")
async def subscription_selftest():
    from llm.agent_engine import selftest
    return await selftest()


@app.post("/subscription/token")
def set_sub_token(body: KeyBody):
    try:
        subscription.set_oauth_token(body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/subscription/token")
def delete_sub_token():
    subscription.delete_oauth_token()
    return {"ok": True}


# ---------- App-wide UI settings ----------
# Persisted in the per-user data dir (NOT the WebView's localStorage, which the
# installer wipes on reinstall), so theme/density/font/layout survive updates.
APP_SETTINGS_PATH = DATA_DIR / "app_settings.json"


@app.get("/settings")
def get_settings():
    try:
        return json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class SettingsBody(BaseModel):
    settings: dict


@app.put("/settings")
def put_settings(body: SettingsBody):
    APP_SETTINGS_PATH.write_text(
        json.dumps(body.settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True}


class ProfileBody(BaseModel):
    content: str


# ---------- profile workspaces ----------
# ---------- Shared (cross-profile) context ----------
# Replaces the old "global" workspace: one block of context read into EVERY profile's
# prompt. The old global profile's text is migrated into it (see workspaces.ensure_migration).
@app.get("/shared-profile", response_class=PlainTextResponse)
def get_shared_profile():
    return workspaces.get_shared_profile()


@app.put("/shared-profile")
def put_shared_profile(body: ProfileBody):
    workspaces.set_shared_profile(body.content)
    return {"ok": True}


@app.get("/preferences", response_class=PlainTextResponse)
def get_preferences():
    """Standing agent-behaviour preferences — editable by the player in the app."""
    return workspaces.get_preferences() or workspaces.PREFERENCES_DEFAULT


@app.put("/preferences")
def put_preferences(body: ProfileBody):
    workspaces.set_preferences(body.content)
    return {"ok": True}


@app.get("/workspaces")
def get_workspaces():
    return {"workspaces": workspaces.list_workspaces()}


class WorkspaceBody(BaseModel):
    display_name: str


@app.post("/workspaces")
def create_workspace(body: WorkspaceBody):
    if not body.display_name.strip():
        raise HTTPException(400, "A workspace needs a name.")
    return workspaces.create_workspace(body.display_name)


@app.delete("/workspaces/{slug}")
def delete_workspace(slug: str):
    try:
        workspaces.delete_workspace(slug)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/workspaces/{slug}/profile", response_class=PlainTextResponse)
def get_ws_profile(slug: str):
    return workspaces.get_profile(slug)


@app.put("/workspaces/{slug}/profile")
def put_ws_profile(slug: str, body: ProfileBody):
    workspaces.set_profile(slug, body.content)
    return {"ok": True}


@app.get("/workspaces/{slug}/character")
def get_ws_character(slug: str):
    return {"character": workspaces.get_character(slug)}


# --- character binding (Lodestone) ---
class CharSearchBody(BaseModel):
    q: str
    world: str = ""


_WAF_MSG = ("The Lodestone is currently blocking automated character lookups "
           "(Square Enix added a bot challenge). Paste a Lodestone URL to try a "
           "direct import, or fill in the profile below by hand.")


@app.post("/workspaces/{slug}/character/search")
async def character_search(slug: str, body: CharSearchBody):
    try:
        hits = await asyncio.to_thread(_characters.find, body.q, body.world)
    except LodestoneBlocked:
        raise HTTPException(503, _WAF_MSG)
    return {"results": [{"id": h.id, "name": h.name, "world": h.world} for h in hits]}


class CharBindBody(BaseModel):
    id: str = ""
    url: str = ""


def _char_id_from(body: "CharBindBody") -> str | None:
    if body.id.strip():
        return body.id.strip()
    m = re.search(r"/character/(\d+)", body.url or "")
    return m.group(1) if m else None


async def _bind(slug: str, char_id: str) -> dict:
    try:
        char = await asyncio.to_thread(_characters.character, char_id)
    except LodestoneBlocked:
        raise HTTPException(503, _WAF_MSG)
    if not char:
        raise HTTPException(404, "Couldn't fetch that character from the Lodestone.")
    # File this snapshot under the job it was worn on BEFORE rendering, so the profile
    # shows the full per-job archive and not just today's logout.
    workspaces.record_gear_from_char(slug, char)
    identity = _characters.to_profile_markdown(char, workspaces.gear_archive(slug))
    return workspaces.apply_character(slug, char, identity)


@app.post("/workspaces/{slug}/character/bind")
async def character_bind(slug: str, body: CharBindBody):
    char_id = _char_id_from(body)
    if not char_id:
        raise HTTPException(400, "Provide a Lodestone character id or URL.")
    return await _bind(slug, char_id)


@app.post("/workspaces/{slug}/gearsets/import")
def gearsets_import(slug: str):
    """Import EVERY saved gearset from the game's own GEARSET.DAT.

    This is the only source that knows gear for a job you didn't log out on — the
    Lodestone publishes just the last logout. Ids come from the file; names and item
    levels come from Garland. Nothing is fetched from the Lodestone here.
    """
    from sources import gearsets as _gearsets

    sets, used = _gearsets.read_local()
    if not sets:
        return {
            "ok": False, "found": 0, "file": str(used) if used else "",
            "note": ("No saved gearsets found on this PC. That's normal if the game "
                     "isn't installed here, or you keep no gearsets — the profile "
                     "falls back to the Lodestone's last-logout snapshot."),
        }

    imported = []
    for g in sets:
        pieces = []
        for slot, iid in g.items.items():
            if slot == "SoulCrystal":
                continue      # the job stone isn't gear and can't be upgraded
            it = _garland.item(iid)
            if not it:
                continue
            pieces.append({"slot": slot, "name": it.name, "item_level": it.item_level})
        if pieces:
            entry = workspaces.record_gear(slug, g.job, pieces, source="gearset_file")
            imported.append({"job": g.job, "set": g.name, "pieces": len(pieces),
                             "average_item_level": entry.get("average_item_level", 0)})
    return {"ok": True, "found": len(sets), "imported": imported,
            "file": str(used) if used else ""}


@app.post("/workspaces/{slug}/character/refresh")
async def character_refresh(slug: str):
    existing = workspaces.get_character(slug)
    if not existing or not existing.get("id"):
        raise HTTPException(400, "No character bound to this workspace yet.")
    return await _bind(slug, existing["id"])


# ---------- chats ----------
def _as_items(v) -> list[dict]:
    """Normalize a docs/notes field to a list of {id, content, title?, draft?, shared?}
    cards. Migrates the old single-string format (and drops malformed entries).

    This whitelists fields, so ANY new per-card flag must be added here too — it's
    the single choke point every save passes through, and a field missing from it is
    silently dropped rather than erroring.
    """
    if isinstance(v, list):
        out = []
        for it in v:
            if isinstance(it, dict) and isinstance(it.get("content"), str):
                card = {"id": it.get("id") or uuid.uuid4().hex[:8], "content": it["content"]}
                if it.get("title"):
                    card["title"] = it["title"]
                if it.get("draft"):
                    card["draft"] = True
                if it.get("shared"):        # visible from your other profiles
                    card["shared"] = True
                out.append(card)
        return out
    if isinstance(v, str) and v.strip():
        return [{"id": uuid.uuid4().hex[:8], "content": v}]
    return []


def _chat_json(chat_id: str) -> dict:
    p = chat_dir(chat_id) / "chat.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {"id": chat_id, "title": "New chat", "messages": []}
    # docs/notes are lists of {id, content} cards (migrated from old strings).
    data["docs"] = _as_items(data.get("docs"))
    data["notes"] = _as_items(data.get("notes"))
    data.setdefault("owner", workspaces.default_slug())
    # Cited sources accumulate across the chat's turns so the Sources tab survives
    # a restart (they used to live only in frontend runtime state).
    data.setdefault("sources", [])
    return data


def _save_chat(data: dict) -> None:
    # Every save is player activity (a message, a doc edit, a kept asset) —
    # stamping it here is what lets the sidebar order by RECENCY, so the chat
    # you just used surfaces on top no matter when it was created.
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p = chat_dir(data["id"]) / "chat.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/chats")
def list_chats():
    """Chats, most recently ACTIVE first.

    Ordered by updated_at (stamped on every save), so using an old chat bumps
    it to the top — recency, not creation order. Chats from before the stamp
    existed fall back to chat.json's mtime, which IS their last activity; the
    directory-ctime fallback below only covers chats missing even created_at.
    """
    out = []
    for d in CHATS_DIR.iterdir():
        f = d / "chat.json"
        if not f.exists():
            continue
        try:
            c = json.loads(f.read_text(encoding="utf-8-sig"))  # tolerate a stray BOM
        except (json.JSONDecodeError, OSError):
            continue  # skip a corrupt chat rather than 500 the whole list
        created = c.get("created_at")
        if not created:
            try:
                created = datetime.fromtimestamp(
                    d.stat().st_ctime, timezone.utc).isoformat(timespec="seconds")
            except OSError:
                created = ""
        updated = c.get("updated_at")
        if not updated:
            try:
                updated = datetime.fromtimestamp(
                    f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            except OSError:
                updated = created
        out.append({"id": c.get("id", d.name), "title": c.get("title", "New chat"),
                    "count": len(c.get("messages", [])),
                    "owner": c.get("owner", ""),
                    # "overlay" for in-game Ask-pill chats — the sidebar groups them.
                    "surface": c.get("surface", ""),
                    "created_at": created,
                    "updated_at": updated})
    out.sort(key=lambda c: c["updated_at"], reverse=True)
    return {"chats": out}


def _snippet(text: str, ql: str, width: int = 90) -> str:
    """A bit of text around the match, so a hit is recognisable at a glance."""
    i = text.lower().find(ql)
    if i < 0:
        return text[:width].strip()
    start = max(0, i - width // 3)
    out = text[start:start + width].strip().replace("\n", " ")
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


@app.get("/search")
def search(q: str, owner: str = "", scope: str = "workspace", limit: int = 80):
    """Search docs, notes and asset names across chats.

    scope="global" searches every profile. scope="workspace" searches `owner`'s chats
    PLUS anything marked `shared` anywhere — that's what sharing an item means here:
    it stays in its own chat but stays findable from your other profiles.
    """
    ql = q.strip().lower()
    if not ql:
        return {"hits": []}
    all_scope = scope == "global"
    hits: list[dict] = []

    for d in sorted(CHATS_DIR.iterdir(), reverse=True):
        f = d / "chat.json"
        if not f.exists():
            continue
        try:
            c = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue  # skip a corrupt chat rather than 500 the whole search
        chat_id = c.get("id", d.name)
        chat_owner = c.get("owner", "")
        mine = all_scope or not owner or chat_owner == owner

        for kind in ("docs", "notes"):
            for it in _as_items(c.get(kind)):
                shared = bool(it.get("shared"))
                if not (mine or shared):
                    continue
                title = (it.get("title") or "").strip()
                content = it.get("content") or ""
                if ql not in (title + "\n" + content).lower():
                    continue
                hits.append({
                    "kind": kind[:-1],           # "doc" | "note"
                    "id": it.get("id", ""), "chat_id": chat_id,
                    "chat_title": c.get("title", "New chat"), "owner": chat_owner,
                    "title": title or _snippet(content, ql, 50),
                    "snippet": _snippet(content, ql),
                    "shared": shared,
                })

        shared_assets = set(c.get("shared_assets") or [])
        adir = d / ASSETS_DIRNAME
        if adir.is_dir():
            for p in sorted(adir.iterdir()):
                if not p.is_file():
                    continue
                shared = p.name in shared_assets
                if not (mine or shared) or ql not in p.name.lower():
                    continue
                hits.append({
                    "kind": "asset", "id": p.name, "chat_id": chat_id,
                    "chat_title": c.get("title", "New chat"), "owner": chat_owner,
                    "title": p.name, "snippet": "", "shared": shared,
                })
        if len(hits) >= limit:
            break
    return {"hits": hits[:limit]}


class SharedAssetBody(BaseModel):
    shared: bool


@app.post("/chats/{chat_id}/assets/{name}/shared")
def set_asset_shared(chat_id: str, name: str, body: SharedAssetBody):
    """Mark/unmark an asset as shared. Assets are files, so the flag lives on the
    chat rather than the file itself — nothing moves on disk."""
    data = _chat_json(chat_id)
    cur = set(data.get("shared_assets") or [])
    cur.add(name) if body.shared else cur.discard(name)
    data["shared_assets"] = sorted(cur)
    _save_chat(data)
    return {"ok": True, "shared": body.shared}


class CreateChatBody(BaseModel):
    owner: str = ""   # empty -> the first/default profile (there is no global workspace)


@app.post("/chats")
def create_chat(body: CreateChatBody | None = None):
    cid = uuid.uuid4().hex[:12]
    owner = workspaces.resolve_owner(body.owner if body else "")
    # created_at is what orders the sidebar — the id is random, so it can't.
    data = {"id": cid, "title": "New chat", "owner": owner,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "messages": []}
    _save_chat(data)
    return data


@app.get("/chats/{chat_id}")
def get_chat(chat_id: str):
    return _chat_json(chat_id)


class MoveBody(BaseModel):
    owner: str


@app.post("/chats/{chat_id}/move")
def move_chat(chat_id: str, body: MoveBody):
    """Reassign a chat's owning workspace (share-to-global / move-to-profile). Just
    a one-field rewrite — assets stay in the chat folder, so no paths break."""
    data = _chat_json(chat_id)
    data["owner"] = workspaces.resolve_owner(body.owner)
    _save_chat(data)
    return {"ok": True, "owner": data["owner"]}


@app.delete("/chats/{chat_id}")
def delete_chat(chat_id: str):
    shutil.rmtree(CHATS_DIR / chat_id, ignore_errors=True)
    return {"ok": True}


class ItemsBody(BaseModel):
    items: list[dict]


@app.put("/chats/{chat_id}/notes")
def put_notes(chat_id: str, body: ItemsBody):
    data = _chat_json(chat_id)
    data["notes"] = _as_items(body.items)
    _save_chat(data)
    return {"ok": True}


@app.put("/chats/{chat_id}/docs")
def put_docs(chat_id: str, body: ItemsBody):
    data = _chat_json(chat_id)
    data["docs"] = _as_items(body.items)
    _save_chat(data)
    return {"ok": True}


class MessagesBody(BaseModel):
    messages: list[dict]


@app.put("/chats/{chat_id}/messages")
def put_messages(chat_id: str, body: MessagesBody):
    """Replace a chat's message list — used by the edit/rollback UI to truncate the
    conversation at an edited turn before re-sending."""
    data = _chat_json(chat_id)
    data["messages"] = body.messages
    _save_chat(data)
    return {"ok": True}


# ---------- attachments (files / photos / folders as context) ----------
@app.post("/chats/{chat_id}/attach")
async def attach(chat_id: str, files: list[UploadFile] = File(...)):
    for f in files:
        attachments.store(chat_id, f.filename or "file", await f.read())
    return {"attachments": attachments.listing(chat_id)}


@app.get("/chats/{chat_id}/attachments")
def get_attachments(chat_id: str):
    return {"attachments": attachments.listing(chat_id)}


@app.delete("/chats/{chat_id}/attachments/{name:path}")
def delete_attachment(chat_id: str, name: str):
    attachments.delete(chat_id, name)
    return {"attachments": attachments.listing(chat_id)}


@app.get("/chats/{chat_id}/assets/{name}")
def get_asset(chat_id: str, name: str):
    p = chat_dir(chat_id) / ASSETS_DIRNAME / name
    if not p.exists():
        raise HTTPException(404, "asset not found")
    return FileResponse(p)


# ---------- chat streaming ----------
class ChatBody(BaseModel):
    chat_id: str
    model: str
    message: str
    auth: str = "api"  # "api" (litellm) or "subscription" (Agent SDK)
    # The composer's "ignore my profile" switch — answer without the player's
    # personal profile in context (preferences still apply).
    ignore_profile: bool = False
    # "" = the normal app chat. "overlay" = the in-game Ask pill: answers render
    # on a tiny card over the game, so a compact-answer system block is added
    # and the chat is stamped for sidebar grouping (docs/overlay-spec.md §6.1).
    surface: str = ""
    # One-shot screen awareness (overlay §6.5): a data-URL JPEG of the game
    # screen, attached to THIS turn's message only — never stored on the chat,
    # so it isn't re-billed on later turns like attachments are.
    screenshot: str = ""


class AnswerBody(BaseModel):
    ask_id: str
    answer: str


@app.post("/chat/answer")
def chat_answer(body: AnswerBody):
    """Deliver the player's answer to a pending ask_user question, resuming the
    still-open chat stream that is awaiting it."""
    from llm import interactive
    return {"ok": interactive.submit(body.ask_id, body.answer)}


class SuggestBody(BaseModel):
    chat_id: str
    model: str
    auth: str = "api"


@app.post("/chat/suggestions")
async def chat_suggestions(body: SuggestBody):
    """A few short follow-up messages the player might send next (suggestion chips)."""
    from llm.suggest import followups
    data = _chat_json(body.chat_id)
    return {"suggestions": await followups(body.model, body.auth, data["messages"])}


def _annotate_handler(chat_id: str, tmp: bool = False):
    """Wire the annotate_image tool to this chat's assets folder.

    tmp=True is the AGENT's path: its output is a TEMPORARY image (tmp_ prefix)
    that renders inline in chat but never lands on the Assets shelf unless the
    player clicks "Add to Assets". The interactive editor keeps tmp=False —
    a player-made annotation is deliberate work, it belongs on the shelf.
    """
    def handler(args: dict) -> dict:
        adir = chat_dir(chat_id) / ASSETS_DIRNAME
        src = adir / args["asset_id"]
        if not src.exists():
            return {"error": f"asset '{args['asset_id']}' not found"}
        out_bytes = annotate(src.read_bytes(), spec_from_dict(args))
        out_name = f"{'tmp_' if tmp else ''}annotated_{uuid.uuid4().hex[:8]}.png"
        (adir / out_name).write_bytes(out_bytes)
        return {"ok": True, "asset_id": out_name, "title": args.get("title", "")}
    return handler


class AnnotateBody(BaseModel):
    asset_id: str
    title: str = ""
    annotations: list[dict] = []


@app.post("/chats/{chat_id}/annotate")
def annotate_endpoint(chat_id: str, body: AnnotateBody):
    """Flatten an annotation spec onto a base asset (used by the interactive editor)."""
    result = _annotate_handler(chat_id)({
        "asset_id": body.asset_id, "title": body.title, "annotations": body.annotations,
    })
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


def _save_asset(chat_id: str):
    """Image saver for AGENT tools (show_image, pin_on_map fallback).

    tmp_ prefix on purpose: an agent-fetched image is TEMPORARY — it renders
    inline in the chat (the file stays, chat history must keep working) but is
    excluded from the Assets shelf until the player promotes it via the
    "Add to Assets" hover button (/assets/{name}/keep).
    """
    def save(data: bytes, ext: str = "png") -> str:
        adir = chat_dir(chat_id) / ASSETS_DIRNAME
        adir.mkdir(parents=True, exist_ok=True)
        name = f"tmp_{uuid.uuid4().hex[:8]}.{ext}"
        (adir / name).write_bytes(data)
        return name
    return save


@app.get("/chats/{chat_id}/assets")
def list_assets(chat_id: str):
    # tmp_* are agent-fetched temporaries: visible inline in chat, but only on
    # the shelf after the player promotes them.
    adir = chat_dir(chat_id) / ASSETS_DIRNAME
    names = sorted(p.name for p in adir.glob("*")
                   if p.is_file() and not p.name.startswith("tmp_"))
    return {"assets": names}


@app.post("/chats/{chat_id}/assets/{name}/keep")
def keep_asset(chat_id: str, name: str):
    """Promote a temporary (agent-fetched) image to a real shelf asset.

    COPY, not rename: the chat's inline `asset:tmp_…` references must keep
    resolving forever, and the player owns the copy independently.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad asset name")
    adir = chat_dir(chat_id) / ASSETS_DIRNAME
    src = adir / name
    if not src.exists():
        raise HTTPException(404, "asset not found")
    if not name.startswith("tmp_"):
        return {"ok": True, "asset_id": name}   # already permanent
    dest = adir / name.removeprefix("tmp_")
    if not dest.exists():
        shutil.copyfile(src, dest)
    return {"ok": True, "asset_id": dest.name}


@app.post("/chats/{chat_id}/assets")
async def upload_asset(chat_id: str, file: UploadFile = File(...), name: str = ""):
    """Save a UI-generated image (a captioned map screenshot) as a chat asset.

    Same folder and naming convention as the agent's pin_on_map assets, so it shows
    in the Assets tab and the agent can annotate or reference it like any other.
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    # The caller's name is a hint, not a path: strip to a safe stem.
    stem = re.sub(r"[^\w\- ]", "", (name or "map shot")).strip().replace(" ", "_")[:40]
    fname = f"{stem or 'map_shot'}_{uuid.uuid4().hex[:8]}.png"
    adir = chat_dir(chat_id) / ASSETS_DIRNAME
    adir.mkdir(parents=True, exist_ok=True)
    (adir / fname).write_bytes(data)
    return {"ok": True, "asset_id": fname}


# How many recent user/assistant messages the model gets verbatim. Everything
# older collapses into one compact system note: the full history is re-billed as
# input on EVERY turn, so a long chat's cost grows with its whole past — and
# turns that old almost never carry detail the recent window doesn't restate.
HISTORY_WINDOW = 10
# Condensed lines kept from before the window; anything older still is dropped
# outright (with a count, so the model knows the chat didn't start there).
HISTORY_CONDENSED_MAX = 40


def _condensed_history(messages: list[dict]) -> list[dict]:
    """The message list actually sent to the model: the last HISTORY_WINDOW
    verbatim, older turns as one deterministic system note. No summarizer call —
    a truncated line per message is cheap, instant, and can't hallucinate."""
    recent = messages[-HISTORY_WINDOW:]
    older = messages[:-HISTORY_WINDOW]
    out: list[dict] = []
    if older:
        lines = []
        dropped = max(0, len(older) - HISTORY_CONDENSED_MAX)
        if dropped:
            lines.append(f"({dropped} earlier messages omitted)")
        for m in older[-HISTORY_CONDENSED_MAX:]:
            text = " ".join(str(m.get("content", "")).split())
            if len(text) > 160:
                text = text[:160].rsplit(" ", 1)[0] + "…"
            lines.append(f"{m.get('role', 'user')}: {text}")
        out.append({"role": "system", "content":
                    "Earlier in this chat (condensed — each line is truncated, so ask "
                    "the player rather than trusting one for anything load-bearing):\n"
                    + "\n".join(lines)})
    out += [{"role": m["role"], "content": m["content"]} for m in recent]
    return out


@app.post("/chat")
async def chat_endpoint(body: ChatBody):
    data = _chat_json(body.chat_id)
    data["messages"].append({"role": "user", "content": body.message})
    if data.get("title") in (None, "New chat") and data["messages"]:
        data["title"] = body.message[:48]
    if body.surface == "overlay":
        data["surface"] = "overlay"

    # Flavor the chat by its OWNER'S profile, not any active switcher — opening an
    # old chat always uses the profile it was created under.
    convo = [{"role": "system", "content": build_system_prompt(
        data.get("owner", ""), include_profile=not body.ignore_profile)}]
    if body.surface == "overlay":
        convo.append({"role": "system", "content": OVERLAY_SYSTEM})
    # Inject attached-file text as context (works on both engines).
    attach_ctx = attachments.context_block(body.chat_id)
    if attach_ctx:
        convo.append({"role": "system", "content": attach_ctx})
    # Saved DOCS arrive as a TITLE LIST, not wholesale content — the docs used to
    # be injected in full on every turn, which billed every guide in the chat on
    # every question. The model pulls the one it needs via read_doc. (Notes stay
    # private — the assistant only ever sees docs.)
    docs = data.get("docs") or []
    doc_list = [d for d in docs if d.get("content")]
    if doc_list:
        listing = "\n".join(f"- [{d['id']}] {d.get('title') or '(untitled)'}" for d in doc_list)
        convo.append({"role": "system", "content":
                      "Reference docs the player has saved in this chat (id — title). "
                      "Call read_doc with an id to read one when it's relevant; treat "
                      "their content as authoritative context:\n" + listing})
    convo += _condensed_history(data["messages"])

    # Attach images as vision blocks on the API path (litellm multimodal).
    if body.auth != "subscription":
        imgs = attachments.image_data_urls(body.chat_id)
        if body.screenshot.startswith("data:image/"):
            imgs = [*imgs, {"type": "image_url", "image_url": {"url": body.screenshot}}]
        if imgs and convo and convo[-1]["role"] == "user":
            base = convo[-1]["content"]
            blocks = base if isinstance(base, list) else [{"type": "text", "text": base}]
            convo[-1] = {"role": "user", "content": [*blocks, *imgs]}

    def _create_doc(args: dict) -> dict:
        """Save agent-authored content as a DRAFT doc on this chat. Appends to the
        same `data` dict the endpoint persists at the end, so it survives the turn."""
        doc_id = uuid.uuid4().hex[:8]
        data.setdefault("docs", [])
        data["docs"].append({
            "id": doc_id, "content": args.get("content", ""),
            "title": args.get("title", ""), "draft": True,
        })
        return {"ok": True, "doc_id": doc_id, "title": args.get("title", "")}

    def _read_doc(args: dict) -> dict:
        """Hand the model ONE saved doc's content, on request. The system message
        lists only ids + titles, so this is how doc content reaches the model —
        including a doc created earlier THIS turn, since it reads live `data`."""
        want = (args.get("doc_id") or "").strip()
        for d in data.get("docs") or []:
            if d.get("id") == want:
                return {"ok": True, "doc_id": want, "title": d.get("title", ""),
                        "content": d.get("content", "")}
        return {"ok": False,
                "note": "No saved doc with that id in this chat.",
                "docs": [{"id": d.get("id"), "title": d.get("title", "")}
                         for d in data.get("docs") or []]}

    def _import_character(args: dict) -> dict:
        """Bind a Lodestone character to THIS chat's workspace (conversational path)."""
        owner = workspaces.resolve_owner(data.get("owner"))
        q = (args.get("query") or "").strip()
        url_m = re.search(r"/character/(\d+)", q)
        char_id = url_m.group(1) if url_m else (q if q.isdigit() else None)
        try:
            if char_id:
                char = _characters.character(char_id)
                if not char:
                    return {"ok": False, "note": "Couldn't fetch that character."}
                res = workspaces.apply_character(owner, char, _characters.to_profile_markdown(char))
                return {"ok": True, "bound": res}
            hits = _characters.find(q, args.get("world", ""))
        except LodestoneBlocked:
            return {"ok": False, "note": _WAF_MSG}
        if not hits:
            return {"ok": False, "note": f"No Lodestone character found for '{q}'."}
        if len(hits) == 1:
            char = _characters.character(hits[0].id)
            if char:
                res = workspaces.apply_character(owner, char, _characters.to_profile_markdown(char))
                return {"ok": True, "bound": res}
        return {"ok": False, "candidates": [
            {"id": h.id, "name": h.name, "world": h.world} for h in hits[:6]],
            "note": "Multiple matches — ask the player which id to use, then call again."}

    def _record_gear(args: dict) -> dict:
        """Save a job's gear into THIS chat's profile (from a screenshot, or told).

        Filed under the owning workspace, not the active switcher — the same rule the
        system prompt follows, so a chat always reads and writes one profile.
        """
        owner = workspaces.resolve_owner(data.get("owner"))
        job = (args.get("job") or "").strip()
        src = args.get("source") or "player"
        pieces = [
            {"slot": (p.get("slot") or "").strip(),
             "name": (p.get("name") or "").strip(),
             "item_level": int(p.get("item_level") or 0)}
            for p in (args.get("pieces") or [])
            if (p.get("name") or "").strip()
        ]
        if not job or not pieces:
            return {"ok": False, "note": "Need a job and at least one readable piece."}
        if src not in ("screenshot", "player"):
            src = "player"      # only a real scrape may claim 'lodestone'
        entry = workspaces.record_gear(owner, job, pieces, source=src)
        return {"ok": True, "job": job, "pieces": len(pieces),
                "average_item_level": entry.get("average_item_level", 0),
                "note": ("Saved. It's in their profile from now on, labelled with "
                         "where it came from and when.")}

    ctx = {
        "chat_id": body.chat_id,     # correlates the agent-loop flight recorder
        "engine": body.auth,
        "annotate_handler": _annotate_handler(body.chat_id, tmp=True),
        "save_asset": _save_asset(body.chat_id),
        "create_doc": _create_doc,
        "read_doc": _read_doc,
        "import_character": _import_character,
        "record_gear": _record_gear,
    }
    # Subscription-path image vision (Agent SDK, Anthropic-format blocks).
    if body.auth == "subscription":
        sub_imgs = attachments.image_blocks_anthropic(body.chat_id)
        if body.screenshot.startswith("data:image/"):
            head, _, b64 = body.screenshot.partition(",")
            media = head[5:head.index(";")] if ";" in head else "image/jpeg"
            sub_imgs = [*sub_imgs, {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": b64},
            }]
        if sub_imgs:
            ctx["images"] = sub_imgs

    async def event_stream():
        answer_parts: list[str] = []
        try:
            async for ev in run_chat(body.model, body.auth, convo, ctx):
                if ev["type"] == "token":
                    answer_parts.append(ev["text"])
                elif ev["type"] == "tool" and body.surface == "overlay":
                    # Anything said before a tool call is the model thinking
                    # out loud ("Let me try a different search:"). On the
                    # overlay that preamble is noise — it ran together with
                    # the answer on the card AND in the pill's history,
                    # because the stored message kept every token. Drop it and
                    # keep only what follows the last tool call.
                    answer_parts.clear()
                elif ev["type"] == "source" and ev.get("label"):
                    # Record cited sources on the chat (deduped) so the Sources tab is
                    # restored on reload instead of being lost with the runtime state.
                    srcs = data.setdefault("sources", [])
                    if not any(s.get("label") == ev["label"] for s in srcs):
                        srcs.append({"label": ev["label"], "url": ev.get("url", "")})
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            # In a `finally`, not after the loop: the Stop button aborts the
            # request, which CANCELS this generator mid-stream — the partial
            # answer must survive to disk, and the cancellation also propagates
            # into the engine (litellm stream / Agent SDK task) and stops the
            # actual model run, not just the pipe.
            from llm import looplog
            text = "".join(answer_parts)
            if text:
                data["messages"].append({"role": "assistant", "content": text})
                _save_chat(data)
            looplog.log(body.chat_id, body.auth, "stream_closed",
                        chars=len(text))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------- Overlay checklist (the in-game guide widget) ----------
class ChecklistPinBody(BaseModel):
    chat_id: str
    doc_id: str


class ChecklistToggleBody(BaseModel):
    index: int


def _pinned_doc() -> tuple[dict, dict] | tuple[None, None]:
    """(chat data, doc) for the doc pinned to the overlay, or (None, None)."""
    import overlay_checklist
    p = overlay_checklist.pinned()
    if not p.get("chat_id") or not p.get("doc_id"):
        return None, None
    try:
        data = _chat_json(p["chat_id"])
    except Exception:
        return None, None
    doc = next((d for d in (data.get("docs") or []) if d.get("id") == p["doc_id"]), None)
    return (data, doc) if doc else (None, None)


@app.get("/overlay/checklist")
def overlay_checklist_get():
    import overlay_checklist
    data, doc = _pinned_doc()
    if not doc:
        return {"pinned": False, "steps": []}
    return {
        "pinned": True,
        "chat_id": data.get("id", ""),
        "doc_id": doc.get("id", ""),
        "title": (doc.get("title") or "").strip() or "Checklist",
        "steps": overlay_checklist.steps(doc.get("content") or ""),
    }


@app.post("/overlay/checklist")
def overlay_checklist_pin(body: ChecklistPinBody):
    import overlay_checklist
    return {"ok": True, "pinned": overlay_checklist.pin(body.chat_id, body.doc_id)}


@app.delete("/overlay/checklist")
def overlay_checklist_unpin():
    import overlay_checklist
    overlay_checklist.unpin()
    return {"ok": True}


@app.post("/overlay/checklist/toggle")
def overlay_checklist_toggle(body: ChecklistToggleBody):
    """Tick a step from the overlay — writes through to the doc itself, so the
    app's editor and the in-game widget always show the same state."""
    import overlay_checklist
    data, doc = _pinned_doc()
    if not doc:
        raise HTTPException(404, "No doc is pinned to the overlay.")
    doc["content"] = overlay_checklist.toggle(doc.get("content") or "", body.index)
    _save_chat(data)
    return {"ok": True, "steps": overlay_checklist.steps(doc["content"])}


# ---------- In-app updates (GitHub Releases) ----------
@app.get("/update/check")
def update_check(current: str = ""):
    """The newest published release, compared against the running app."""
    import updates
    try:
        rel = updates.latest_release()
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach GitHub: {exc}") from exc
    if not rel:
        return {"found": False, "current": current}
    return {"found": True, "current": current,
            "newer": updates.is_newer(rel.version, current) if current else False,
            **updates.as_dict(rel)}


@app.post("/update/download")
def update_download():
    """Start fetching the newest installer; poll /update/status for progress."""
    import updates
    try:
        rel = updates.latest_release()
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach GitHub: {exc}") from exc
    if not rel:
        raise HTTPException(404, "No published release with an installer.")
    updates.download_async(rel)
    return {"ok": True, "version": rel.version}


@app.get("/update/status")
def update_status():
    import updates
    return updates.state()


# ---------- Overlay watches (the passive chips' data) ----------
class WatchBody(BaseModel):
    kind: str = "pin"        # "pin" | "pinset" | "node" (timed)
    label: str = ""
    zone: str = ""
    x: float = 0
    y: float = 0
    icon: str = ""
    category: str = ""       # pinset: the category name shown on the chip
    pins: list[dict] = []    # pinset: [{x, y, label}] in 2048 map space
    ref: str = ""            # node: the gathering-point id — backend enriches
    windows: list[dict] = [] # timed: [{start_et: hour, dur_min: ET minutes}]
    # The raw map payload the chip re-opens on click (same shape the card's
    # "Open map" uses), so a chip restores exactly what the answer pinned.
    place: dict = {}


@app.get("/overlay/watches")
def overlay_watches():
    import overlay_watches
    return {"watches": overlay_watches.list_watches()}


@app.post("/overlay/watches")
def overlay_watches_add(body: WatchBody):
    import overlay_watches
    w = body.model_dump()
    # A node watch arrives as just {kind, ref}: the backend owns the lookup so
    # every arm point (db detail, agent card) stays a one-liner. Spawn windows
    # come from the local client's pop-time table; coords are GAME coords, so
    # the place pin says space:"game" and the app converts on open.
    if w["kind"] == "node" and w.get("ref"):
        from sources import gameclient
        n = gameclient.node_record(w["ref"], items=False) or {}
        if n.get("spawn_times"):
            w["windows"] = [{"start_et": h, "dur_min": n.get("uptime_minutes") or 60}
                            for h in n["spawn_times"]]
        w["zone"] = w.get("zone") or n.get("zone") or ""
        w["label"] = w.get("label") or n.get("name") or "Gathering node"
        if n.get("x") and n.get("y"):
            w["x"], w["y"] = n["x"], n["y"]
            w.setdefault("place", {})
            if not w["place"]:
                w["place"] = {"zone": w["zone"],
                              "pin": {"x": n["x"], "y": n["y"],
                                      "label": w["label"], "space": "game"}}
    return {"ok": True, "watch": overlay_watches.add(w)}


@app.delete("/overlay/watches/{watch_id}")
def overlay_watches_remove(watch_id: str):
    import overlay_watches
    return {"ok": overlay_watches.remove(watch_id)}


@app.get("/overlay/timers")
def overlay_timers():
    """Real-clock open/close instants for every timed watch.

    Eorzea time runs 3600/175 × real (1 ET hour = 175 real seconds). For each
    watch the earliest window wins: active ones report closes_at, upcoming
    ones opens_at + closes_at — all unix seconds, so the overlay just ticks
    down locally between polls.
    """
    import time as _time

    import overlay_watches

    now = _time.time()
    et_day = (now * 3600 / 175) % 86400   # ET seconds since ET midnight
    out = []
    for w in overlay_watches.list_watches():
        wins = w.get("windows") or []
        if not wins:
            out.append({"id": w["id"], "timed": False})
            continue
        best = None
        for win in wins:
            start = (int(win.get("start_et") or 0) % 24) * 3600
            dur = max(1, int(win.get("dur_min") or 60)) * 60   # ET seconds
            since = (et_day - start) % 86400
            if since < dur:  # open right now
                cand = {"active": True, "opens_at": now,
                        "closes_at": now + (dur - since) * 175 / 3600}
            else:
                to_open = (start - et_day) % 86400
                opens = now + to_open * 175 / 3600
                cand = {"active": False, "opens_at": opens,
                        "closes_at": opens + dur * 175 / 3600}
            if best is None or (cand["active"] and not best["active"]) \
               or (cand["active"] == best["active"] and cand["opens_at"] < best["opens_at"]):
                best = cand
        out.append({"id": w["id"], "timed": True, **best})
    return {"now": now, "timers": out}


# ---------- Database browser (right-panel tab) ----------
# Backed by Garland Tools' JSON API. Rendered natively rather than iframed: it's a
# hash-routed SPA, so there's no server-rendered page to embed even if we wanted one
# — and parsing JSON beats scraping either way.
@app.get("/db/search")
def db_search(q: str, kind: str = "item", limit: int = 20):
    # kind="all" (and anything unrecognised) means NO type filter — Garland's search
    # returns every type in one request, so this costs nothing extra.
    kind = kind if kind in _garland.LINKABLE else ""
    hits = _garland.search(q, kind=kind, limit=limit)
    return {
        "kind": kind or "any",
        "hits": [{"name": h.name, "url": h.url, "id": h.id, "type": h.type,
                  "item_level": h.item_level, "icon": h.icon} for h in hits],
    }


@app.get("/map/zones")
def map_zones():
    """Drawable zones grouped by region, in the in-game picker's order.

    `complete` is false when a flaky fetch truncated the underlying index — the
    UI must NOT cache an incomplete list for the session, or zones silently
    disappear from the picker until restart (Yak T'el, once).
    """
    from sources import gamemap
    regs = gamemap.regions()
    return {"regions": regs, "complete": gamemap.index_complete()}


# ---------- custom map pins ----------
# Storage lives in sources/mappins.py because the AGENT writes pins too
# (pin_on_map): one store means an agent pin behaves exactly like a player pin.
class PinBody(BaseModel):
    zone: str
    x: float
    y: float
    label: str = ""
    color: str = ""
    kind: str = ""   # groups the pin under its own toolbar toggle (e.g. "gathering")
    icon: str = ""   # named game symbol (sources/icons.py) drawn instead of the dot


class PinPatch(BaseModel):
    zone: str
    label: str | None = None
    color: str | None = None


@app.get("/map/markers/search")
def map_markers_search(q: str):
    """Named game markers matching q across all zones — the map bar's search.
    Only named layers (areas, aetherytes, cities, dungeons, landmarks); the
    anonymous "other" service icons are excluded by design."""
    from sources import gamemap
    return {"markers": gamemap.search_markers(q)}


@app.get("/map/pins/all")
def map_pins_all():
    """Every saved pin, keyed by zone — feeds the map bar's pin search, which
    jumps across zones and so can't work from one zone's slice."""
    from sources import mappins
    return {"zones": mappins.load()}


@app.get("/map/pins")
def map_pins(zone: str):
    from sources import mappins
    return {"pins": mappins.for_zone(zone)}


@app.post("/map/pins")
def add_map_pin(body: PinBody):
    from sources import mappins
    return mappins.add(body.zone, body.x, body.y, body.label,
                       body.color or mappins.DEFAULT_COLOR, body.kind, body.icon)


@app.patch("/map/pins/{pin_id}")
def update_map_pin(pin_id: str, body: PinPatch):
    from sources import mappins
    pin = mappins.update(body.zone, pin_id, body.label, body.color)
    if not pin:
        raise HTTPException(404, "pin not found")
    return pin


@app.delete("/map/pins/{pin_id}")
def delete_map_pin(pin_id: str, zone: str):
    from sources import mappins
    if not mappins.remove(zone, pin_id):
        raise HTTPException(404, "pin not found")
    return {"ok": True}


@app.get("/npc/photo")
def npc_photo(name: str):
    """An NPC's portrait: the community wiki's infobox screenshot (the real
    in-game look), proxied and disk-cached so the volunteer wiki isn't
    hotlinked on every page view. 404 when the wiki has no image — the
    caller (gdb._d_npc) only points here after confirming one exists."""
    from sources import cache
    from llm.tools import _wiki

    def _load() -> bytes:
        hit = _wiki.lookup("consolegames", name)
        if not hit or not hit.image_url:
            return b""
        s = _wiki._client
        r = s.get(hit.image_url, timeout=30)
        return r.content if r.status_code == 200 and r.content else b""

    try:
        img = cache.fetch("wiki", f"npcphoto:{name.lower()}", 30 * 24 * 3600, _load)
    except Exception:
        img = b""
    if not img:
        raise HTTPException(404, f"No wiki portrait for '{name}'.")
    mt = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
    return Response(content=img, media_type=mt,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/map/texture")
def map_texture(zone: str):
    """A zone's map texture, from the local disk cache (fetched once per patch).

    This is what makes the map fast after first view: the ~2.5MB image comes off
    disk instead of Garland, and still works offline. Cache-Control lets the webview
    skip even the localhost round trip on repeat opens within a session.
    """
    # gamemap.map_texture, not garland's: it falls back to XIVAPI's map asset for
    # the few zones Garland has no file for (Ul'dah - Steps of Thal).
    from sources import gamemap
    tex = gamemap.map_texture(zone)
    if not tex or not tex.get("image"):
        raise HTTPException(404, f"No map texture for '{zone}'.")
    # Garland serves PNG; the XIVAPI fallback serves JPEG — read the magic bytes
    # rather than promising the wrong type.
    mt = "image/jpeg" if tex["image"][:2] == b"\xff\xd8" else "image/png"
    return Response(content=tex["image"], media_type=mt,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/map/icon")
def map_icon(id: int):
    """A map-marker icon PNG, from the local disk cache."""
    from sources import gamemap
    png = gamemap.icon_png(id)
    if not png:
        raise HTTPException(404, f"No icon {id}.")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icons")
def icons_catalog():
    """The agent's named icon vocabulary (see sources/icons.py)."""
    from sources import icons
    return {"icons": icons.catalog()}


@app.get("/icons/by-name/{name}")
def icon_by_name(name: str):
    """One named icon as PNG — what `icon:<name>` markdown resolves to."""
    from sources import gamemap, icons
    canon = icons.canonical(name)
    # Locally-drawn markers (the white star) — no game icon behind them.
    local = icons.local_png(canon)
    if local:
        return Response(content=local, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    iid = icons.icon_id(name)
    if not iid:
        raise HTTPException(404, f"No icon named '{name}'.")
    png = gamemap.icon_png(iid)
    if not png:
        raise HTTPException(404, f"Icon '{name}' unavailable.")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/map/zone")
def map_zone(zone: str, node_id: str = ""):
    """The in-game map for a zone: texture + the game's own markers.

    Rebuilt from the game's data (Garland texture + XIVAPI MapMarker) rather than any
    map site, so it covers every zone including Dawntrail — which is exactly where
    A Realm Remapped has nothing.

    node_id pins one gathering node. Its coords are in-game flag coords, so they're
    converted into the same 2048 marker space the UI positions everything in.
    """
    from sources import gamemap
    from sources.maps import coord_to_pixel
    z = gamemap.zone(zone)
    if not z:
        raise HTTPException(404, f"No map data for '{zone}'.")
    if node_id:
        n = _garland.node(node_id)
        if n and (n.get("x") or n.get("y")):
            from sources.icons import NODE_TYPE_ICON
            sf = z.get("size_factor", 100)
            px = coord_to_pixel(n["x"], sf, gamemap.TEX)
            py = coord_to_pixel(n["y"], sf, gamemap.TEX)
            # Hand the node to the UI as `node`, NOT as an extra marker: the
            # frontend draws it as the temporary pin, and a marker at the same
            # spot rendered the same label twice (two elements, offset anchors).
            z["node"] = {
                "x": px, "y": py, "label": n["name"], "kind": "gathering",
                "icon": (NODE_TYPE_ICON.get(str(n.get("type") or "").strip().lower())
                         or NODE_TYPE_ICON.get(n.get("type_id")) or "mining"),
                "detail": _node_detail(n),
            }
            z["focus"] = {"x": px, "y": py}
    return z


def _node_detail(n: dict) -> str:
    """One-line node summary for the map tooltip: when it's up and what it needs."""
    bits = [f"Lv {n['level']}"]
    if n.get("type"):
        bits.append(f"{n['type']}{' ' + '★' * n['stars'] if n.get('stars') else ''}")
    if n.get("spawn_times"):
        hrs = ", ".join(f"{h:02d}:00" for h in n["spawn_times"])
        bits.append(f"up at {hrs} ET for {n.get('uptime_minutes', 0)}m")
    if n.get("folklore"):
        bits.append(f"needs {n['folklore']}")
    return " · ".join(bits)


@app.get("/db/browse")
def db_browse(kind: str):
    """Garland-style Browse: one kind's records, grouped like Garland's own UI."""
    from sources import gdb
    if kind not in gdb.BROWSE_KINDS:
        raise HTTPException(400, f"Unknown browse kind '{kind}'.")
    out = gdb.browse(kind)
    out["label"] = gdb.BROWSE_LABEL[kind]
    return out


@app.get("/db/detail")
def db_detail(kind: str, id: str):
    """One record of any non-item kind (items go through /db/item)."""
    from sources import gdb
    if kind == "item":
        return db_item(id=id)
    return gdb.detail(kind, id)


@app.get("/db/item")
def db_item(url: str = "", id: str = ""):
    """One item's record. Accepts a Garland id, or a /db/#item/<id> url.

    The url regex is STRICTLY #item/<id> — the old numeric fallback swallowed
    instance/quest URLs and rendered whatever ITEM shared the number ("A Chorus
    Slime" the duty came back as a necklace). Non-item URLs belong to /db/detail.
    """
    ident = id
    if not ident and url:
        m = re.search(r"#item/(\d+)", url)
        ident = m.group(1) if m else ""
    if not ident:
        raise HTTPException(400, "Need a Garland item id.")
    res = _garland.item(ident)
    if not res:
        return {"found": False, "url": url or _garland.web_url("item", ident)}

    # Live market price, from Universalis. Deliberately NOT cached (the whole point of
    # a price is that it's current) and only fetched for tradeable items — an
    # untradable item has no market and Universalis 404s on it, which would read as a
    # failure rather than "this can't be sold".
    market = None
    if res.tradeable:
        try:
            market = _universalis_for_ui(res.name)
        except Exception:
            market = None

    return {
        "found": True, "source": res.source, "name": res.name, "url": res.url,
        "category": res.jobs, "item_level": str(res.item_level or ""),
        "description": res.description, "details": res.details, "icon": res.icon,
        "patch": res.patch, "materia_slots": res.sockets,
        "attributes": res.attributes,
        "upgrades": res.upgrades, "downgrades": res.downgrades,
        # Sources & Uses — how you actually get one, and what it feeds. Node zones are
        # resolved so the UI can link each to the rebuilt in-game map.
        "sell_price": res.sell_price, "tradeable": res.tradeable,
        "nodes": _garland.nodes_detail(res.nodes), "ventures": res.ventures,
        "ingredient_of": res.ingredient_of, "vendors": res.vendors,
        "market": market,
        "comments": [],   # Garland has no player comments — see sources/garland.py
    }


def _universalis_for_ui(item_name: str) -> dict | None:
    """Cheapest current listing for the Database tab's Marketboard row."""
    from llm.tools import _universalis

    res = _universalis.get_price(item_name, "Aether")
    if not res:
        return None
    cheapest = min(
        (l for l in (res.listings or []) if l.get("price_per_unit")),
        key=lambda x: x["price_per_unit"], default=None,
    )
    return {
        "world_or_dc": res.world_or_dc,
        "lowest": res.min_price or (cheapest or {}).get("price_per_unit", 0),
        "average": round(res.avg_price or 0),
        "world": (cheapest or {}).get("world_name", ""),
        "listings": len(res.listings or []),
    }


# ---------- Doc subchat: chat with the agent from inside a doc ----------
class DocEditBody(BaseModel):
    chat_id: str
    kind: str = "docs"        # "docs" | "notes"
    doc_id: str
    instruction: str
    model: str
    auth: str = "api"


DOC_EDIT_SYSTEM = """The player is chatting with you from inside ONE open document. This is a side thread
about that document — not the main conversation.

- For a TARGETED change (a row, a cell, a heading, a sentence), call edit_doc with the
  exact old_text and its replacement — the rest of the doc stays untouched and you
  don't resend it. This covers almost every request in this thread.
- Only when restructuring most of the document, call update_doc with the COMPLETE new
  markdown. Either way, do NOT print the document into the chat; the tools are how it
  gets saved.
- Then reply in ONE or TWO sentences saying what you changed. That reply is all they
  see in the thread — keep it short and specific ("Added a Notes column and filled it
  from the patch notes."), never a recap of the whole doc.
- If they ask a QUESTION about the doc rather than for a change, just answer it.
  Don't call update_doc.
- If the request is unclear or would lose information, ask instead of guessing. A doc
  they curated is worse off half-rewritten than left alone.
- Look facts up with your tools before adding them. Never invent an item level or url.

The document's current text follows.
"""


def _subchat_key(kind: str, doc_id: str) -> str:
    return f"{kind}:{doc_id}"


def _apply_doc_edit(text: str, args: dict) -> tuple[str | None, dict]:
    """Pure diff-apply for the edit_doc tool: (new_text, result).

    new_text is None when nothing changed — the result then says exactly why
    (not found / ambiguous / bad occurrence), so the model can retry with a
    longer snippet instead of silently patching the wrong spot. A wrong-place
    edit corrupts a doc the player curated, which is worse than a retry."""
    old = args.get("old_text") or ""
    new = args.get("new_text") or ""
    if not old:
        return None, {"ok": False, "note": "old_text is required — the exact text to replace."}
    count = text.count(old)
    if count == 0:
        return None, {"ok": False,
                      "note": ("old_text not found in the document. It must match the "
                               "saved markdown EXACTLY (whitespace included) — re-read "
                               "the doc and retry with a verbatim snippet.")}
    try:
        occurrence = int(args.get("occurrence") or 0)
    except (TypeError, ValueError):
        occurrence = 0
    if count > 1 and not occurrence:
        return None, {"ok": False,
                      "note": (f"old_text appears {count} times — pass occurrence "
                               f"(1-{count}), or include more surrounding text so the "
                               "match is unique.")}
    if occurrence:
        if not 1 <= occurrence <= count:
            return None, {"ok": False,
                          "note": f"occurrence must be 1-{count}; old_text appears {count} times."}
        idx = -1
        for _ in range(occurrence):
            idx = text.find(old, idx + len(old) if idx >= 0 else 0)
        patched = text[:idx] + new + text[idx + len(old):]
    else:
        patched = text.replace(old, new)
    if not patched.strip():
        return None, {"ok": False, "note": "Edit refused — it would empty the document."}
    return patched, {"ok": True, "note": "Saved. Now tell them in 1-2 sentences what changed."}


@app.get("/chats/{chat_id}/subchat")
def get_subchat(chat_id: str, kind: str, doc_id: str):
    """One doc's thread, so reopening its chat bubble shows the context again."""
    data = _chat_json(chat_id)
    return {"messages": (data.get("subchats") or {}).get(_subchat_key(kind, doc_id)) or []}


@app.delete("/chats/{chat_id}/subchat")
def clear_subchat(chat_id: str, kind: str, doc_id: str):
    data = _chat_json(chat_id)
    data.setdefault("subchats", {}).pop(_subchat_key(kind, doc_id), None)
    _save_chat(data)
    return {"ok": True}


@app.post("/docs/edit")
async def docs_edit(body: DocEditBody):
    """One turn of a doc's side thread.

    Deliberately NOT /chat: the turn is about one document, its history lives in that
    doc's own subchat rather than the main transcript, and the document changes via
    update_doc rather than by being printed into the reply.
    """
    data = _chat_json(body.chat_id)
    items = data.get(body.kind) or []
    item = next((d for d in items if d.get("id") == body.doc_id), None)
    if item is None:
        raise HTTPException(404, "That doc is no longer in this chat.")

    key = _subchat_key(body.kind, body.doc_id)
    thread = (data.setdefault("subchats", {})).setdefault(key, [])

    convo = [
        {"role": "system", "content": build_system_prompt(data.get("owner", ""))},
        {"role": "system", "content": DOC_EDIT_SYSTEM},
        {"role": "system", "content":
            "--- OPEN DOCUMENT ---\n" + (item.get("content") or "")
            + "\n--- END DOCUMENT ---"},
    ]
    convo += [{"role": m["role"], "content": m["content"]} for m in thread]
    convo.append({"role": "user", "content": body.instruction})

    edited = {"content": ""}

    def _update_doc(args: dict) -> dict:
        content = _strip_fence((args.get("content") or "").strip())
        if not content:
            return {"ok": False, "note": "Empty document refused — nothing changed."}
        edited["content"] = content
        item["content"] = content
        item.pop("draft", None)     # an edited doc is no longer an unread draft
        return {"ok": True, "note": "Saved. Now tell them in 1-2 sentences what changed."}

    def _edit_doc(args: dict) -> dict:
        """The targeted-change path: patch one snippet in place instead of having
        the model resend the whole document (see _apply_doc_edit)."""
        content, result = _apply_doc_edit(item.get("content") or "", args)
        if content is None:
            return result
        edited["content"] = content
        item["content"] = content
        item.pop("draft", None)     # an edited doc is no longer an unread draft
        return result

    ctx = {
        "chat_id": body.chat_id,     # flight recorder: doc-thread turns were "?"
        "engine": body.auth,
        "annotate_handler": _annotate_handler(body.chat_id, tmp=True),
        "save_asset": _save_asset(body.chat_id),
        "update_doc": _update_doc,
        "edit_doc": _edit_doc,
        # No create_doc: this thread edits THIS doc, it doesn't spawn new ones.
        "usage_context": "doc",   # the usage ledger separates doc-thread spend
    }

    async def event_stream():
        parts = []
        async for ev in run_chat(body.model, body.auth, convo, ctx):
            if ev["type"] == "token":
                parts.append(ev["text"])
            yield f"data: {json.dumps(ev)}\n\n"
        reply = "".join(parts).strip()
        thread.append({"role": "user", "content": body.instruction})
        thread.append({"role": "assistant", "content": reply or "(no reply)"})
        _save_chat(data)
        if edited["content"]:
            yield f"data: {json.dumps({'type': 'doc_edited', 'content': edited['content']})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _strip_fence(text: str) -> str:
    """Drop a ```markdown fence if the model wrapped the whole document in one.

    Instructed not to, but a fenced document saved verbatim would show the player
    literal backticks, so tolerate it rather than corrupt their file.
    """
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.S)
    return m.group(1) if m else text
