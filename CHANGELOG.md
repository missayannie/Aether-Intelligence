# Changelog

## v1.1.1

Everything below is new since **v1.0**. (v1.1 shipped a build from the same code
as v1.0 with only a README change, so this is the first release with functional
changes.)

### The in-game overlay — new

A transparent, click-through window drawn over FFXIV in Borderless Windowed
mode. It stays out of the way until summoned and never eats a click meant for
the game. Full design notes in [docs/overlay-spec.md](docs/overlay-spec.md).

- **Ask pill** (`` Alt+` ``) — ask the agent without leaving the game. Answers
  stream onto a compact card with their sources, an **Open map** button when the
  answer is a place, and **Arm chips** to keep those pins on screen. Follow-ups
  share one rolling chat, which also appears in the app's sidebar under
  *Overlay*; recent turns show in a scrollable history under the pill.
- **Screen awareness** (opt-in, off by default) — tick 📷 and each question
  carries one downscaled frame of the game, so "where are the aether currents in
  *this* map" resolves against what you're actually looking at. The frame is
  sent with that one question and never stored, and the overlay excludes itself
  from capture so it only ever sees the game.
- **Database drawer** (`Alt+D`) — keyboard-first search over the whole database:
  type, ↑↓ to walk results, Enter for a compact detail, Esc to back out. From
  there: **Flag on map**, **Open in app**, or **⏱ Watch**.
- **Passive chips** — armed watches on a draggable rail, with live spawn
  countdowns for gathering nodes (Eorzea time: 175 real seconds per ET hour).
  Arm them from an answer card, a map pin's **⏱ Watch**, a node's page, or the
  drawer; see and stop all of them in Settings.
- **Shortcuts** — `` Alt+Shift+` `` shows the layer quietly, `Alt+\` is a kill
  switch, and all four are re-bindable in Settings.
- Every widget drags where you want it and stays there, at any overlay size.

### In-app updates — new

Settings → Updates checks this repo's GitHub Releases, shows what the release
says, downloads the installer with a progress bar, and launches it. Checking on
startup is on by default (it only adds a small sidebar nudge); downloading and
installing without asking is opt-in, because it closes the app to replace
itself.

### Keep running in the background — new

Closing the app can hide it to the system tray so the overlay keeps working;
the tray icon reopens or quits it. With the setting off, closing the app now
shuts the overlay and backend down cleanly instead of leaving an orphan behind.

### Better research

- **The wiki reader sees more.** It now reads section prose and bullet lists,
  not just tables, ranked by what you asked. Reward and acquisition catalogues
  live in exactly those lists — "which animals drop Sanctuary Carapace" and
  "what mounts come from Cosmic Exploration" were unanswerable before.
- **Items with no database path fall back to the wiki.** Pasture leavings, mob
  drops and similar now pull their acquisition data from the wiki page instead
  of the agent reporting that it couldn't find out.
- **Runaway searches are capped.** A hard per-question search budget, plus a
  forced final answer when the tool-call limit is reached, so an exhausted run
  answers with what it has instead of dying with nothing after burning tokens.
- **Answers say who actually answered.** Facts read from your own game files are
  cited as the FFXIV game client rather than a community database that was only
  ever the fallback.
- Sampling temperature lowered from 0.7 to 0.3 for steadier answers.
- Enumerable questions render as tables with icons in chat rather than pointing
  you at a wiki page.

### Fixes

- **Overlay widgets could be drawn off-screen.** The overlay-size setting scales
  the layer with CSS zoom, but placement was measured in unzoomed pixels, so
  anything parked near an edge was pushed past the screen by the zoom factor —
  on a 3440-wide display the pill was being drawn at x=4088, invisible, while
  the overlay still held the mouse. Geometry is zoom-aware now and clamped on
  screen, and widgets re-place when the zoom changes.
- **Summoned surfaces no longer close when focus is lost.** Over a game the
  overlay can't hold OS focus, and blur-driven closing made the pill look like
  it never opened. Losing focus now only releases the mouse.
- The drawer closes on click-away, Esc always releases the mouse, and a crash
  inside the overlay can no longer leave the screen unclickable.
- **Doc side-thread edits appear in an open editor tab.** They used to land on
  disk while the open tab kept showing stale content — and the stale tab could
  overwrite the edit on the next keystroke.
- **The database pane refreshes properly.** Clearing the search box returns to
  the browse view immediately, and the catalogue chips stay pinned at the top
  during results.
- Tabs show a normal pointer on hover; the grab cursor appears only while
  actually dragging one.

### Housekeeping

- App version now tracks the release tags, so the updater can tell whether a
  release is newer than what's installed.
- README documents the overlay, shows real screenshots of the app, and explains
  how to publish a release the updater can find.

## v1.1

README changes only; the binary matches v1.0.

## v1.0

Initial public release.
