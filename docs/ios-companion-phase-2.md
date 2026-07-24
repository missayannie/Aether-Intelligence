# iOS Companion — Phase 2 handoff (for the Mac agent)

**Phase 2 = Ask (chat).** The good news: **no native work.** No new plugin, no
Info.plist change, no signing change. The chat screen is written and **verified
end-to-end on the web target** against the live v2.0.0 backend — a real question
streamed a real answer (with game-client data and source chips) over the Tailscale
address with the device token. Your job is just to build it onto the device.

Prereq: Phase 1 must be done (the app pairs and stores a Keychain token), since
chat runs authenticated.

---

## Milestone

On the iPhone, from the Paired screen: tap **Ask a question**, type one, and the
answer **streams in**. Source chips appear under it. Tapping **‹** returns to the
Paired home.

---

## What already exists (verified — don't rewrite)

- **`src/screens/Chat.tsx`** — the chat UI: message list, streaming assistant
  bubble, source chips, an input with Send/Stop (abortable).
- **`src/lib/client.ts`** — `createChat()`, `defaultModel()`, and the ported
  `streamChat()` (SSE-over-POST). Requests carry the bearer token.
- **`src/screens/Paired.tsx`** — now has an **Ask a question** button.
- **`src/App.tsx`** — routes Paired ⇄ Chat (a simple `home | chat` view).

The chat uses the desktop's **default model** (`/models` → `default`) and creates
a fresh chat via `POST /chats` on first send.

---

## Your work — the recurring loop only

```bash
cd mobile
git pull
npm run build
npx cap sync ios
npx cap open ios      # build & run on the iPhone
```

Then on device: pair (if not already) → **Ask a question** → confirm answers
stream and source chips render.

---

## Verification

- [ ] `npm run build` succeeds.
- [ ] On device: a question streams an answer token-by-token.
- [ ] Source chips (e.g. "FFXIV game client", "Garland Tools") appear under answers.
- [ ] **Stop** (■) halts a streaming answer; **Send** (↑) works again after.
- [ ] **‹** returns to Paired; the desktop's `/chats` shows the new conversation.

---

## Known follow-up (optional, not required to pass)

- **Markdown rendering.** The desktop returns markdown (links, bold, `map:` refs).
  The bubble currently shows it as **plain text**, so a link reads as
  `[Cordia Sap](https://…)`. The answer is fully legible and the source chips are
  already tappable, so this is a polish item — add a lightweight markdown renderer
  (or `react-markdown`, as the desktop uses) whenever you want richer answers. Best
  handled on the Windows side; flag it back rather than building it here.

---

## Scope boundary — Phase 3 next

After chat works on device: **Phase 3 — polish** (reconnect/roaming states,
re-pair/forget flow, background→foreground restore). Mostly Windows-scaffolded JS;
the Mac tests the device-only behaviors. Don't build it here.
