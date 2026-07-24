# iOS Companion — Phase 3 handoff (for the Mac agent)

**Phase 3 = Polish.** Reconnect/roaming, an Offline state, markdown answers, and
background-return re-probing. All the logic is written and **verified on the web
target**; the Mac's job is the recurring build loop plus testing the two things
only a real phone shows (background restore, real network roaming).

**No new native plugins, no Info.plist changes.** (One JS dependency was added —
`react-markdown` + `remark-gfm` — pure JS, picked up by `npm install`.)

---

## What shipped (verified on web)

- **Markdown answers** — the chat renders the desktop's markdown: real `http` links
  are tappable (open in the browser); the desktop's non-navigable refs (e.g. `map:…`)
  render as plain emphasized text. No more raw `[label](url)`.
- **Roaming reconnect** — pairing now stores **every** address from the QR, not just
  the one that answered. On launch (and when the app returns to the foreground) it
  probes them in order — last-good first — so the phone uses the LAN address at home
  and the Tailscale one away, automatically. Verified: paired through a dead host to a
  live one, and the working host is remembered.
- **Offline state** — if the desktop can't be reached (asleep / off-network), the app
  shows a clear **"Can't reach your desktop"** screen with **Retry** and **Forget**,
  keeping the pairing. Verified: dead host → Offline; **Retry** after the desktop
  returns → back to Paired. Each health probe is bounded to 4s so this fails fast
  instead of hanging.
- **Background restore** — an `@capacitor/app` `appStateChange` listener re-probes the
  connection when the app is foregrounded. (Only fires on device.)

Files: `screens/Chat.tsx` (markdown), `screens/Offline.tsx` (new), `App.tsx`
(reconnect/roaming/foreground + splash), `lib/client.ts` (`reconnect`, bounded
`health`), `lib/store.ts` + `lib/pairing.ts` (host list). `screens/Connect.tsx` (the
retired Phase 0 manual-entry screen) was removed — pairing supersedes it.

---

## Your work — the recurring loop

```bash
cd mobile
git pull
npm install          # picks up react-markdown + remark-gfm
npm run build
npx cap sync ios
npx cap open ios
```

---

## Verification (on device)

- [ ] `npm run build` succeeds.
- [ ] Answers render as **formatted markdown** — links tappable, lists/bold shown.
- [ ] Kill the desktop app (or take the phone off-network) → the companion shows the
      **Offline** screen; bring the desktop back → **Retry** reconnects.
- [ ] **Roaming:** pair at home on Wi-Fi, then leave (cellular) — the app reconnects
      over Tailscale on its own (this is the device-only test the web can't do).
- [ ] Background the app for a while, then reopen → it re-probes and is ready (no
      manual retry needed when the desktop is reachable).

---

## Optional remaining polish (not required)

- **Mid-session drops.** Reconnect currently runs on launch/foreground; a desktop that
  drops *during* an open chat surfaces as a send error, not the Offline screen. Fine
  for now; could promote to the Offline flow later.
- **Answer extras.** The desktop also emits map/asset events the phone ignores; a
  future pass could add a lightweight map preview. Windows-side work.

Phase 3 is the last of the core MVP arc. Beyond it is the optional "Later" bucket
(Eorzea DB browser, maps, character profile, chat history) — each its own handoff.
