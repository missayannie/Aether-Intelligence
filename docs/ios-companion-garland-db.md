# iOS Companion — GarlandDB browser (design)

Adds a **Database** tab to the companion: browse and search the same GarlandDB
data the desktop's DB tab shows, from the phone.

Status: design approved, implemented on the Mac session. Unlike Phases 0–3 (app
logic scaffolded on Windows, Mac builds), this feature is written phone-side —
the remaining companion work is iPhone UI.

---

## Why this is phone-side only

The desktop backend already ships the entire API, and every route sits behind the
companion token gate the phone already holds. **No backend change.**

| Route | Returns |
|---|---|
| `GET /db/search?q=&kind=&limit=` | hits across all types, or one kind; `kind=all` costs no extra request (Garland's search returns every type at once) |
| `GET /db/browse?kind=` | `{kind, label, groups:[{label, count, rows:[{id, name, sub, icon?}]}]}` — grouped **server-side**, so the client just renders |
| `GET /db/detail?kind=&id=` | one record of any non-item kind |
| `GET /db/item?id=` | one item: stats as data, `jobCategories`, upgrade/downgrade chain |

Browsable kinds (`gdb.BROWSE_KINDS`, 13): item, patch, action, status,
achievement, instance, quest, fate, leve, node, fishing, npc, mob.
Searchable kinds (`garland.LINKABLE`, 9): item, instance, npc, quest,
achievement, mob, fate, node, leve.

---

## The one real constraint

`gdb._browse_items()` builds **every item in the game** — ~80 XIVAPI pages of 500,
roughly 40,000 rows grouped by `ItemUICategory`. The desktop absorbs that over
loopback; a phone pulling it over Tailscale will not, and rendering 40k rows in a
WKWebView is worse.

**Resolution — collapsed groups.** `browse()` already returns rows nested under
groups with counts, so the list renders **group headers only**, expanding one
group's rows on tap. Items shows ~100 category headers instead of 40k rows; no
kind ever renders its full row set at once. This needs no backend change and
matches Garland's own grouping.

The Items fetch is still large once, so its response is cached (see below) with a
visible building state on first open. Every other kind is small enough to be
effectively instant.

---

## Navigation

`App.tsx` gains a bottom tab bar: **Ask** and **Database**. Both tabs stay mounted
so state survives switching — a half-typed question and a scroll position both
persist.

Unchanged: `Offline` still preempts the whole app when the desktop is unreachable
(neither tab works without it), and `Pair` still precedes everything when there's
no token.

The Database tab is a three-level stack:

1. **Kinds** — grid of the 13 kinds using `BROWSE_LABEL`, with a search field on top.
2. **List** — `/db/browse?kind=`, rendered as collapsible groups with counts.
3. **Record** — `/db/item?id=` or `/db/detail?kind=&id=`.

Back pops one level; cross-reference taps push a new record.

## Search

One field, always at the top of the Database tab.

- **Empty query** → the kind grid.
- **At the kind grid** → searches all types (`kind=all`); results are a flat list,
  each row tagged with its type, since results span types.
- **Inside a kind** → scopes to that kind (`kind=<current>`).

Debounced 250 ms and abortable — a new keystroke cancels the in-flight request, so
fast typing doesn't queue stale responses. Search covers 9 of the 13 kinds
(`LINKABLE`); the 4 browse-only kinds (patch, action, status, fishing) show a
"browse only" note rather than silently returning nothing.

## Record view

- **Items**: icon, name, item level, stats table, `jobCategories`, upgrade/downgrade chain.
- **Other kinds**: that doc's own fields.

Garland docs ship `partials` — the names behind every referenced id — so
cross-references (rewards, quest chains, fish lists) resolve to real names without
extra requests. Those render as tappable rows that push another record view.

## Caching

`lib/db.ts` owns a small cache so re-opening a kind is instant:

- Browse responses cache **in memory** for the session, keyed by kind.
- The **Items** browse additionally persists to `@capacitor/preferences`, since
  it's the only expensive one. Game data is patch-stable (the backend TTL is 7
  days and keys freshness off the game-data hash, not a clock), so a stored copy
  is safe between patches. A **Refresh** control on the Items list clears it.
- Record fetches are not cached — they're small and always current.

## Errors

List and record fetches fail into an **inline retry** inside the tab, not the full
Offline screen. Offline stays reserved for "desktop unreachable", which `App.tsx`
already owns and which preempts both tabs anyway.

---

## Files

New:
- `src/lib/db.ts` — the four typed calls, types, and the cache.
- `src/screens/Database.tsx` — search field + kind grid.
- `src/screens/DbList.tsx` — collapsible grouped rows.
- `src/screens/DbRecord.tsx` — record + cross-references.
- `src/components/TabBar.tsx` — Ask | Database.

Changed:
- `src/App.tsx` — tab routing, both tabs mounted.
- `src/styles.css` — tab bar, kind grid, group rows, record layout.

No new npm dependency. No Info.plist change. No new native plugin — so the Mac
loop stays `npm run build && npx cap sync ios`, and signing is untouched.

---

## Out of scope

- **Backend pagination** for `/db/browse`. Collapsed groups make it unnecessary;
  if item browsing by category ever needs true paging, that's a Windows-side
  handoff against a shipped v2.0.0 backend.
- **Map previews** for nodes/fishing spots. The desktop draws these; the phone
  shows coordinates as text. Its own feature.
- **Offline DB access.** Everything here needs the desktop reachable, same as chat.
