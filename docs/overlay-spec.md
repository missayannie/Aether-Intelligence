# Spec: Aether Overlay

Status: **Draft for review** · Owner: (you) · Last updated: 2026-07-18
Design source: [Aether Overlay — concepts](https://claude.ai/code/artifact/f477df47-6be9-4467-8fe4-544b187e4860)

## 1. Motivation

The app today lives on a second monitor (or behind an alt-tab). The overlay puts
a thin, game-native-looking layer of it *on top of FFXIV itself*: ask a question
without leaving the game, keep a guide's checklist next to the quest list, see
node/fishing timers tick down, and summon the database with a hotkey.

The organizing idea from the design doc: **the overlay is furniture, not a
window.** It lives in the dead zones of the game's HUD, it is click-through
unless summoned, and everything it shows decays back to nothing.

## 2. Non-negotiable ground rules

These are product invariants, not preferences. Every phase must hold them.

1. **Click-through by default.** The overlay never eats a mouse click meant for
   the game. Input is captured only while a summoned surface is open.
2. **Everything fades.** Answers, toasts, and chips auto-dismiss. Steady state
   is one tiny pill — or nothing.
3. **Dead zones only.** Left edge above the chat log, right rail under the quest
   list, strip under the minimap. Never centre-screen, never over hotbars.
   (Positions are user-draggable while in "arrange mode", persisted per monitor.)
4. **Speak the game's visual language.** Translucent dark panels, gold accents,
   quest-list typography.
5. **Kill switch.** One global hotkey hides everything instantly. Optional
   per-widget quiet hours.
6. **The capture contract:** *ambient widgets never capture input; summoned
   windows always capture; nothing in between.* This is what keeps the system
   predictable.
7. **ToS posture:** the overlay is fed **only by data the app already owns**
   (agent, GarlandDB/local client, docs, Universalis, Eorzea-time math). No
   memory reading, no packet capture, no input automation — ever, in this spec.
   OCR position hints and combat-state awareness are explicitly **out of scope**
   (see §8).

## 3. Concepts (what ships)

| # | Concept | Contract | Hotkey | Phase |
|---|---|---|---|---|
| 1 | **Ask pill** — tiny pill expands to an input; agent's answer renders as a compact card (name, place, coords, spawn window) with `Open map` and `Ping me` actions | summoned (captures while expanded) | `` Alt+` `` | 1 |
| 4 | **GarlandDB drawer** — live search over the whole DB, ↑↓/Enter/Esc keyboard nav, compact detail, `Flag on map` / `Open in app` | summoned (captures while open) | `Alt+D` | 2 |
| 3 | **Passive chips & toasts** — node/fishing window countdowns, market alerts, patch-dropped toast; armed from the app or from an Ask answer | ambient (never captures) | — | 3 |
| 2 | **Guide checklist** — the open doc's checklist docked under the quest list; ticks sync both ways with the doc | ambient + hover-tick exception (see §6.4) | — | 4 |

Concept numbering follows the design doc; build order differs (pill → drawer →
chips → checklist) because the drawer reuses the pill's summon/capture plumbing
and the checklist needs two-way doc sync to feel right.

## 4. Architecture

### 4.1 One mechanism: a second Tauri window

A single additional window, label `overlay`, created at runtime from Rust (like
the existing `embed` child webview in [lib.rs](../app/src-tauri/src/lib.rs), but
a top-level window, not a child):

- transparent, undecorated, always-on-top, skip-taskbar, no shadow
- sized to cover the chosen monitor (user picks which, default = primary)
- click-through via `set_ignore_cursor_events(true)` — the **default state**
- renders the same React bundle as the app; `main.tsx` branches on the window
  label (or `?overlay=1`) to mount `<Overlay/>` instead of `<App/>`
- talks to the same backend on `127.0.0.1:8756` over HTTP like the main window

FFXIV must run in **Borderless Windowed** (the common default). True Exclusive
Fullscreen hides any overlay — detect nothing, just document it in the overlay's
settings panel ("not seeing the overlay? check your screen mode").

### 4.2 Input capture state machine

Two states, owned by Rust, toggled by Tauri commands from the overlay frontend:

```
AMBIENT   ignore_cursor_events = true,  window never focused
SUMMONED  ignore_cursor_events = false, window focused, keyboard owned
```

- `` Alt+` `` / `Alt+D` (global shortcuts, `tauri-plugin-global-shortcut`) →
  SUMMONED with the pill/drawer open.
- `Esc`, click-away, or action completion → back to AMBIENT and (on Windows)
  re-focus the game window (`SetForegroundWindow` on the previously-focused
  hwnd, captured at summon time).
- Kill switch `Alt+\` → hide the overlay window entirely (toggle).
- Arrange mode (entered from the main app's settings, not a global hotkey) →
  SUMMONED with drag handles on every widget.

### 4.3 New Rust surface (app/src-tauri)

- `overlay.rs`: `create_overlay_window(monitor)`, commands
  `overlay_set_capture(bool)`, `overlay_show/hide`, `overlay_set_monitor(id)`,
  focus-return bookkeeping.
- `Cargo.toml`: add `tauri-plugin-global-shortcut = "2"`.
- Registered shortcuts emit events (`overlay://summon-ask`,
  `overlay://summon-drawer`, `overlay://kill-switch`) to the overlay window.

### 4.4 New frontend surface (app/src)

```
src/overlay/
  Overlay.tsx        root: state machine mirror, widget layout, arrange mode
  AskPill.tsx        pill + input + streaming answer → AnswerCard
  AnswerCard.tsx     compact card renderer (see §5.1)
  Drawer.tsx         DB search/browse/detail, keyboard nav (reuses api.ts fns)
  Chips.tsx          timer chips + toast stack
  Checklist.tsx      docked doc checklist
  overlayState.ts    watches, positions, hotkey wiring, backend polling
  overlay.css        game-language theme (translucent navy, gold, quest type)
```

Reuse, don't fork: `api.ts` (`dbSearch`, `dbDetail`, `streamChat`,
`searchMapMarkers`, pins), icon URLs (`/map/icon`, `/db/*`), coordinate
formatting from `GameMap.tsx` (extract shared helpers where needed rather than
importing the whole map).

### 4.5 New backend surface (backend/)

| Endpoint | Purpose |
|---|---|
| `POST /overlay/ask` | Ask-pill pipeline: runs the normal agent turn (same engines, same tools) into a dedicated overlay chat, then distills the final answer into a **card JSON** (§5.1). Streams `card`-typed SSE events so the pill can show progress. |
| `GET /overlay/watches` / `POST` / `DELETE /overlay/watches/{id}` | CRUD for armed watches (node, fish, market, custom timer). Persisted to `DATA_DIR/overlay_watches.json`. |
| `GET /overlay/timers` | Computed view: for each watch, next window start/end in ET and local time, active/inactive. Node windows come from the local client's node data (`gameclient.node_record` pop times); ET = `unix * 3600 / 175`. Polled by the overlay every ~10s. |
| `GET /overlay/checklist` | The "active doc" (most recently opened doc with checkboxes) as structured steps. `POST /overlay/checklist/tick` toggles a step — writes through the existing docs storage (`PUT /chats/{id}/docs` path) so the app view updates too. |

Market alert evaluation (phase 3, stretch): a slow poll against Universalis for
watched items, emitting a toast when price crosses a user threshold. Never
cached, low volume.

## 5. Data contracts

### 5.1 The answer card

The overlay never renders the full chat. `POST /overlay/ask` returns:

```json
{
  "title": "Cordia Sap",
  "icon": "/map/icon/...",
  "lines": ["Botanist Lv 80 · unspoiled node"],
  "place": {"zone": "amh_araeng", "label": "Amh Araeng", "x": 26.4, "y": 16.2},
  "window": {"next_start_et": "2:00", "duration_min": 120, "active": false},
  "actions": ["open_map", "ping_me"],
  "chat_id": "overlay-xxxx"
}
```

All fields optional except `title`; the card renders what it gets. The full
conversation lives in the overlay chat (visible in the app's sidebar under an
"Overlay" group), so "open in app" always has somewhere to land.

### 5.2 Watches

```json
{"id": "w1", "kind": "node", "ref": "node:2345", "label": "Cordia Sap",
 "zone": "amh_araeng", "x": 26.4, "y": 16.2,
 "windows": [{"start_et": 200, "dur_min": 120}], "notify": true}
```

`kind`: `node | fish | market | timer`. Armed from: the app (GarlandDB detail
"watch" button), an Ask card's `Ping me`, or a drawer entry.

### 5.3 Overlay layout persistence

`DATA_DIR/overlay_layout.json`: per-monitor widget positions, enabled widgets,
hotkey overrides, quiet hours. Written by the overlay, readable in app settings.

## 6. Concept-level behavior notes

### 6.1 Ask pill (Concept 1)
- Steady state: 28px pill, top-left dead zone. `` Alt+` `` expands; Esc collapses.
- While the agent runs, the pill shows a subtle progress shimmer; the card
  slides in on completion and **auto-dismisses after 20s** unless hovered.
- `Open map`: raises the main app window on its monitor, switches to the Map
  tab, drops the temp pin (existing `jumpToPin` flow). `Ping me`: creates a
  watch from the card's `window` + `place`.
- Voice input is **out of v1** (the design doc says "type or speak" — speak is
  deferred; note it in §9).

### 6.2 DB drawer (Concept 4)
- `Alt+D` summons; input focused immediately; results stream as you type
  (existing `/db/search`). ↑↓ walks, Enter opens compact detail, Esc backs out
  one level then dismisses.
- Search ranking favors play-time kinds (nodes, vendors, duties) — pass an
  `intent=play` hint to `/db/search` that boosts those kinds; detail view is
  the existing detail payload with a compact template.
- `Flag on map` echoes a `(x, y)` coordinate string to the clipboard (so the
  user can paste a call-out in chat themselves — we never inject input).

### 6.3 Chips & toasts (Concept 3)
- Chips render under the minimap dead zone, one per active watch, showing
  `label · mm:ss` to window open (or "open now · mm:ss left"). Stale chips
  (window passed, notify fired) fade out.
- Toasts: watch fired, market threshold crossed, patch detected (the backend
  already watches the client version). Max 1 toast on screen; 8s decay.

### 6.4 Checklist (Concept 2)
- Shows the active doc's checkbox steps, current step highlighted (first
  unticked). Docked right rail under the quest list.
- Tick interaction is the one deliberate exception to "ambient never captures":
  a **held modifier** (default `Alt`) makes the checklist hit-testable; a plain
  mouse never sees it. This keeps rule 1 honest (no un-summoned click ever
  lands on the overlay without the user holding the modifier).
- Ticks write through to the doc; doc edits (agent reorders the plan) push to
  the widget on the next poll (~5s).

## 7. Settings & packaging

- Overlay master toggle + monitor picker + hotkey editor + widget toggles live
  in the main app's Settings. Overlay off by default until the user enables it.
- No new processes: same backend sidecar, same NSIS bundle. The overlay window
  adds nothing to `tauri.conf.json` windows (created at runtime).
- `scripts/build-installer.ps1` unchanged.

## 8. Explicitly out of scope (this spec)

| Capability | Why deferred |
|---|---|
| "You're 210m from the pin" distance hints | needs OCR of the minimap coordinate readout — finicky, revisit after phase 3 |
| Auto-hide in combat, auto-advance quest steps | needs game state (memory/packets) — a separate, clearly-optional decision with its own ToS review |
| Screenshot-to-agent ("what am I looking at?") | clean (reads pixels the user already sees) but not core loop; candidate for phase 5 |
| Voice input on the Ask pill | speech capture + STT provider choice; defer |
| Linux/macOS overlay | Windows-first; the game is Windows-first |

ToS stance (from the design doc, adopted as policy): Square Enix nominally
prohibits all third-party tools and in practice tolerates overlays that don't
automate gameplay or read process memory. A draw-on-top window fed by our own
data sits at the tolerated end. **Never automate an input; never show anything
a streamer would get flagged for.**

## 9. Open questions

1. Does `set_ignore_cursor_events` + always-on-top + transparency behave over a
   borderless-fullscreen game on all three of: single monitor, mixed-DPI dual
   monitor, 3440×1440 ultrawide? (Phase 0 spike answers this before anything
   else is built.)
2. Focus return: is `SetForegroundWindow` reliable enough after Esc, or do we
   need the attach-thread-input trick?
3. Card distillation: dedicated cheap-model pass vs. asking the main agent turn
   to end with a structured card block? (Cost vs. latency trade — decide in
   phase 1 with the flight recorder on.)
4. GPU cost of a fullscreen transparent WebView2 while the game renders —
   measure FPS impact in the spike; if bad, shrink the window to widget
   bounding boxes instead of full-monitor.
5. Where does the "active doc" pointer live — most recently opened in app, or
   an explicit "pin to overlay" button on the doc? (Leaning explicit.)

---

# Implementation plan

Phases are sequential; each ends with a verifiable milestone. File paths are
where work lands, matching current repo layout.

## Phase 0 — plumbing spike (the go/no-go)

**Goal:** prove the mechanism on this machine over the real game.

- [x] Add `tauri-plugin-global-shortcut` to `app/src-tauri/Cargo.toml`.
- [x] `app/src-tauri/src/overlay.rs`: create transparent/undecorated/
      always-on-top/skip-taskbar window sized to a monitor; wire
      `overlay_set_capture`, show/hide, kill-switch shortcut.
- [x] `app/src/main.tsx`: branch on `?overlay=1` / window label → mount a stub
      `<Overlay/>` (pill expands to input on Alt+`; Enter echoes a decaying
      card; Esc/blur releases capture; one fake chip). Web preview of the stub:
      `http://localhost:1420/?overlay=1`.
- [ ] Manual verification over FFXIV in borderless windowed: click-through
      holds everywhere; `Alt+\` toggles; capture toggle types into an
      input; game regains focus on Esc; check GPU/FPS impact; test on the
      ultrawide.
- **Milestone:** screenshot of the stub pill over the game + notes on Q1/Q2/Q4.
  If the spike fails on fundamentals, stop and revisit (e.g. widget-sized
  windows instead of full-monitor).

## Phase 1 — the Ask pill

**Goal:** "where do I find Cordia Sap?" answered in-game, card → map/watch.

- [ ] Backend: `POST /overlay/ask` (new `backend/overlay.py` router) — run the
      existing agent pipeline into an `overlay-` chat, distill to card JSON
      (§5.1), SSE stream (`status`, `card`, `error` events). Respect the
      anti-flail rule; cap turns lower than the app (play-time answers).
- [ ] Backend: overlay chats appear in `/chats` under an "Overlay" group flag.
- [ ] Frontend: `AskPill.tsx` + `AnswerCard.tsx` + summon wiring (`` Alt+` ``),
      auto-dismiss, hover-to-hold.
- [ ] `Open map` action: Tauri command to raise/focus the main window + emit an
      event the main window handles (switch to Map tab, `jumpToPin`).
- [ ] `Ping me` action: `POST /overlay/watches` from the card payload (chip UI
      itself lands in phase 3; until then it just confirms "armed").
- [ ] Settings: overlay enable toggle + monitor picker in the main app.
- **Milestone:** end-to-end over the game; flight-recorder trace attached for
  latency; decide Q3 (distillation approach) from real numbers.

## Phase 2 — the GarlandDB drawer

**Goal:** `Alt+D` → type → Enter → compact detail → flag/open, all keyboard.

- [ ] `Drawer.tsx`: search-as-you-type over `/db/search`, ↑↓/Enter/Esc state
      machine, compact detail template over `/db/detail` + `/db/item` (icons
      via existing local icon URLs).
- [ ] `intent=play` ranking hint in `backend/sources/gdb.py` search path
      (boost nodes/vendors/duties; keep default app behavior unchanged).
- [ ] `Flag on map` (clipboard coord echo + main-window map jump) and
      `Open in app` (raise + deep-link to the GarlandDB tab detail).
- [ ] Watch button on node/fish detail rows → `POST /overlay/watches`.
- **Milestone:** locate a vendor and flag a node without touching the mouse.

## Phase 3 — chips & toasts

**Goal:** ambient timers that are right and quiet.

- [ ] Backend: `GET /overlay/timers` — ET math + window computation from
      `gameclient` node data; unit-check against known unspoiled nodes.
- [ ] Watches CRUD + persistence (`overlay_watches.json`); arm points in the
      app (GarlandDB detail) and from Ask cards / drawer (already wired).
- [ ] `Chips.tsx`: countdown chips, active-window state, fade-on-stale; toast
      stack (watch fired, patch detected — reuse the existing version watcher).
- [ ] Stretch: market threshold watches via Universalis slow poll.
- **Milestone:** arm two node watches, observe a full window cycle (chip counts
  down → "open now" → toast → fades) against in-game truth.

## Phase 4 — the guide checklist

**Goal:** the active doc's steps living under the quest list, two-way sync.

- [ ] "Pin to overlay" affordance on docs in the app (resolves Q5).
- [ ] Backend: `GET /overlay/checklist` + `POST /overlay/checklist/tick`
      writing through existing doc storage.
- [ ] `Checklist.tsx`: quest-list-styled steps, current-step highlight,
      Alt-held hit-testing (§6.4), poll for agent-side doc edits.
- [ ] Arrange mode for all widgets (drag + persist `overlay_layout.json`).
- **Milestone:** tick steps in-game while the same doc visibly updates in the
  app, and vice versa (agent reorders plan → widget updates).

## Later / shelf (not scheduled)

Screenshot-to-agent hotkey · OCR distance hints · combat awareness (separate
ToS decision) · voice input · quiet hours UI.
