# Spec: Profile Workspaces

Status: **Draft for review** · Owner: (you) · Last updated: 2026-07-15

## 1. Motivation

Today the app has a single, global player profile (`profile/player.md`) injected
into every chat ([prompts.py](../backend/prompts.py), [app.py:310](../backend/app.py)).
We want the app to support **multiple characters**, each as a self-contained
**workspace** you switch between, with the whole UI re-rendering to the active
workspace. Binding a character (via Lodestone link or search) auto-fills that
workspace's profile so chats in it are tailored to that character.

## 2. Concepts

- **Workspace** — a named container the UI switches between. Two kinds:
  - **Global** — the shared commons. Not character-specific. Always exists, can't
    be deleted. Every profile can read-through to global content.
  - **Profile** — bound to exactly one FFXIV character. Has its own profile text,
    character binding, and settings.
- **Owner** — every chat carries `owner = "global"` or `owner = "<profile-slug>"`.
  This single tag drives switching, visibility, and share/move. Nothing moves on
  disk when reassigned — only the tag changes.
- **Character binding** — a profile's link to a real Lodestone character. Setting
  it auto-fills the profile's `player.md` (editable afterward; re-runnable to refresh).

### Ownable unit = the chat (important)

In the current data model, **docs, notes, assets, and attachments are not
standalone objects** — they hang off a chat (`chat.json` fields + the chat's
`assets/` folder, see [app.py:204-244](../backend/app.py)). So for v1 the
ownable unit is the **chat**, and its docs/notes/assets/attachments **travel with
it** when shared or moved.

> **Deferred:** moving a doc/note/asset *independently* of its chat (as the
> product vision suggested) requires promoting those to first-class entities with
> their own `owner`. That's a schema change; see §9. v1 moves them with the chat.

## 3. What "switch" changes

Switching the active workspace re-renders the UI to that workspace:

| Surface | Behavior on switch |
|---|---|
| Chat list | Shows chats owned by the active workspace. A **Global** section is always reachable (read-through). |
| Profile editor | Loads the active workspace's `player.md`. Global's is a neutral/general profile (no character). |
| Character settings | Shows the active workspace's bound character (empty for global). |
| Docs / Notes / Assets tabs | Scoped to the currently-open chat, as today — unchanged. |

**What flavors a chat is its owner's profile, not the active switcher.** Opening
an old WHM chat always uses the WHM profile even if you've since switched to your
crafter. New chats inherit the active workspace's owner at creation. This is the
clean resolution of retroactive re-flavoring.

## 4. Data model & storage

```
DATA_DIR/
  profile/
    _index.json                 # [{slug, display_name, character_id, kind}]
    global/
      profile.md                # neutral general profile (no character)
      settings.json
    <slug>/                     # one dir per profile workspace, e.g. whm-aria-cactuar
      profile.md                # was player.md; identity auto-filled from character
      character.json            # bound-character snapshot (from the scraper)
      settings.json
  data/chats/<id>/chat.json     # + "owner": "global" | "<slug>"
  knowledge/                    # SHARED across all workspaces (unchanged)
    characters/<id>.json        # scraper cache (shared)
```

- `_index.json` is the workspace registry (order, display names, which character).
- `character.json` is the `Character` dataclass from
  [lodestone_character.py](../backend/sources/lodestone_character.py), serialized.
- `knowledge/` stays global — scraped news, prices, and character caches are shared;
  no reason to duplicate per workspace.

### Chat schema change

`chat.json` gains one field:

```jsonc
{ "id": "...", "title": "...", "owner": "global", "messages": [...], "notes": "", "docs": "" }
```

Absent `owner` ⇒ treated as `"global"` (covers pre-migration chats).

## 5. Behavior rules

- **Visibility:** in profile P, the chat list shows `owner == P` plus a reachable
  Global section. In global, the list shows `owner == "global"`.
- **Read-through:** from any profile, the assistant may reference global docs/notes
  and shared knowledge when answering. Global cannot read a profile's private chats.
- **Single home (move semantics):** every item has exactly one owner.
  - *Share to global* (from a profile): set `owner = "global"`.
  - *Move to profile* (from global): set `owner = "<slug>"` via a profile-picker dropdown.
  - Both are a one-field rewrite; assets stay in the chat folder, so no path breaks.
- **New chat:** created with `owner = <active workspace>`.
- **Delete a profile:** its chats are reassigned to global (never silently deleted);
  the profile dir is removed. Global is undeletable.

## 6. Character binding flow

In a profile's **Settings → Bind character**:

1. User pastes a **Lodestone URL** *or* enters **name + home world**.
2. URL → parse the ID directly. Name+world → `CharacterClient.find()` returns
   candidates; user picks and **confirms** the right one.
3. `CharacterClient.character(id)` scrapes the profile + `/class_job/` pages.
4. `to_profile_markdown()` renders the identity block; it's merged into `profile.md`
   under a machine-owned `## Identity (imported from the Lodestone)` heading,
   leaving the user's Goals/Playstyle/Preferences untouched (per the earlier
   "auto-fill, user can edit" decision).
5. `character.json` is saved; the workspace's `_index.json` entry records the ID.
6. A **Refresh** button re-runs steps 3-4 (e.g. after leveling a new job).

Region note: the scraper base is `na.` today; EU/JP/OCE characters live on
`eu.`/`jp.` subdomains — derive the base from the pasted URL, or add a region
selector to the search form.

## 7. Backend changes

- **[paths.py](../backend/paths.py):** replace the single `PROFILE_PATH` with
  `PROFILE_DIR = DATA_DIR / "profile"`, plus helpers `profile_dir(slug)`,
  `profile_md(slug)`, `workspace_index()`.
- **[prompts.py](../backend/prompts.py):** `load_profile()` → `load_profile(slug)`;
  `build_system_prompt()` → `build_system_prompt(slug)` reading that workspace's
  `profile.md` (global → its neutral profile).
- **[app.py](../backend/app.py):**
  - `/chat` ([app.py:310](../backend/app.py)) looks up the chat's `owner` and calls
    `build_system_prompt(owner)`.
  - `POST /chats` accepts `{owner}`; `list_chats` returns `owner`; add
    `POST /chats/{id}/move {owner}`.
  - New workspace endpoints: `GET /workspaces`, `POST /workspaces`,
    `DELETE /workspaces/{slug}`, `GET/PUT /workspaces/{slug}/profile`.
  - New binding endpoints: `POST /workspaces/{slug}/character/search {q, world}`,
    `POST /workspaces/{slug}/character/bind {url|id}`,
    `POST /workspaces/{slug}/character/refresh`.
  - Replace the existing global `/profile` GET/PUT with the per-workspace variants
    (or keep `/profile` as an alias for the active workspace).
- **New source:** promote [lodestone_character.py](../backend/sources/lodestone_character.py)
  from sketch to a wired client; register an `import_character` tool in
  [tools.py](../backend/llm/tools.py) so the assistant can also bind/refresh
  conversationally.

## 8. Frontend changes ([app/src](../app/src))

- **Workspace switcher** — a sidebar/dropdown control listing global + profiles;
  selecting one sets active-workspace state and refetches the scoped chat list.
- **Active-workspace state** — held in the frontend (e.g. a context/store),
  persisted to localStorage so it survives reload; passed as `owner` on new chats.
- **Settings page per workspace** — profile editor (reuse the
  [AnnotationEditor](../app/src/AnnotationEditor.tsx) pattern) + the Bind-character
  UI (link field, search box, results list, confirm, refresh).
- **Share/Move controls** — on a chat: "Share to global" (in a profile) /
  "Move to profile…" with a profile-picker dropdown (in global).
- **[api.ts](../app/src/api.ts)** — add the workspace/binding/move calls.

## 9. Migration

One-time, on first launch after upgrade:

1. Create `profile/global/profile.md` from the existing `profile/player.md`
   (content preserved → no behavior change for current chats).
2. Ensure every existing `chat.json` has `owner` (default `"global"`).
3. Write `_index.json` with the single `global` entry.

Existing chats keep working, now bucketed as global; the user creates
character-bound profiles and reorganizes via move going forward. Reversible: the
global profile is just the old player.md.

## 10. Phasing

1. **Data + migration:** owner tag, `_index.json`, per-workspace dirs, owner-aware
   `build_system_prompt`. (Backend only; UI still shows "everything" = global.)
2. **Switcher + scoped chat list.** The core switching UX.
3. **Character binding:** wire the scraper, settings page, search/confirm/refresh.
4. **Share/Move** actions + profile-picker.
5. **(Deferred)** standalone movable docs/notes/assets (§2), per-workspace knowledge
   isolation if ever wanted.

## 11. Open questions

- Does global get its own editable neutral profile, or literally no profile block?
  (Spec assumes a neutral, editable one.)
- Should the assistant's read-through to global include *global chats' messages*,
  or only their docs/notes? (Spec assumes docs/notes + shared knowledge, not raw
  chat transcripts.)
- Independent doc/note/asset ownership (§9 deferred) — wanted for v2, or never?
