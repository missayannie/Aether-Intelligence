# iOS Companion — Phase 0 handoff (for the Mac agent)

**You are a coding agent on a macOS machine.** Your job is to finish Phase 0 of the
Aether Intelligence iOS companion: take the web scaffold that already exists in
`mobile/`, wrap it as a native iOS app with Capacitor, and get it running on a
physical iPhone reaching the desktop backend.

This was scaffolded on a Windows machine, which cannot run Xcode/CocoaPods — so
the platform-agnostic parts are done and the Mac-only parts are yours.

---

## Goal & milestone

**Milestone:** the app launches on a real iPhone, you type your desktop's address,
tap **Test connection**, and it shows your PC's name — i.e. the phone reached
`GET /health` on the desktop backend over the LAN or Tailscale.

That's the whole of Phase 0. **Pairing (QR scan + token), chat, and the rest are
explicitly out of scope here** — see "Scope boundary" at the end. Do not build them.

---

## Prerequisites (verify each before starting)

| Need | Check | If missing |
|---|---|---|
| macOS + Xcode | `xcodebuild -version` | Install Xcode from the App Store, then `xcode-select --install` |
| Node ≥ 20 | `node -v` | Install via nvm/brew |
| CocoaPods | `pod --version` | `sudo gem install cocoapods` (or `brew install cocoapods`) |
| An iPhone + cable | — | Needed for real testing; the Simulator can't exercise the camera/Local-Network/Tailscale later |
| Apple account | — | Free tier signs a 7-day build to your own device; that's enough for Phase 0 |
| Desktop running **v2.0.0** | Desktop app open, **Settings → Companion devices → enabled**, then **restart the app** | The bind only opens on restart after enabling |
| Same network / Tailscale | Phone and PC on one LAN, or both on the same tailnet | Tailscale is recommended (encrypted, works off-LAN) |

**Get your desktop's address:** with companion access enabled, the desktop's
`POST /pair/start` (shown in Settings → Companion devices) lists reachable
`host:port` candidates — e.g. `desktop.tailnet.ts.net:8756` or `192.168.1.75:8756`.
For Phase 0 you just need one of those strings; you'll type it into the app.

---

## What already exists in `mobile/`

A complete Vite + React + TypeScript + Capacitor **web** scaffold. Do not rewrite it;
verify and build on it.

```
mobile/
  package.json            # deps: react 19, @capacitor/{core,ios,app,preferences} v6
  vite.config.ts          # base: "./" (Capacitor serves from file://)
  tsconfig.json
  index.html              # viewport-fit=cover for safe areas
  capacitor.config.ts     # appId com.ffxivguide.companion, webDir "dist"
  .gitignore
  src/
    main.tsx
    App.tsx               # Phase 0 = single screen
    styles.css            # Eorzean Night, phone-tuned
    lib/
      client.ts           # runtime base URL + bearer; health() + ported streamChat()
      store.ts            # connection persistence via @capacitor/preferences
    screens/
      Connect.tsx         # host entry + liveness check (the Phase 0 screen)
```

Key design notes you should preserve:
- `client.ts` sets the API base at **runtime** (not a build-time env var like the
  desktop) and attaches `Authorization: Bearer <token>` when a token exists. Phase 0
  hits only `/health`, which is an open route, so no token is needed yet.
- `client.ts` already contains a **verbatim port of the desktop's SSE-over-POST chat
  parser** (`streamChat`). It's unused in Phase 0 but is the reason we chose Capacitor —
  Phase 2 reuses it directly. Leave it in.
- `store.ts` persists the connection with `@capacitor/preferences`. **Phase 1 must move
  the token to a Keychain-backed secure-storage plugin** — the shape is one object so
  that swap touches only this file. Not your problem for Phase 0.

---

## Execution steps

Run from the repo root on the Mac.

### 1. Install & verify the web build
```bash
cd mobile
npm install
npm run build          # tsc + vite build — must succeed and emit dist/
```
If `tsc` complains, fix types before proceeding. This proves the web layer is sound
independent of iOS.

### 2. (Optional but recommended) sanity-check in a browser
```bash
npm run dev            # http://localhost:5180
```
Open it, type a reachable `host:port` for your desktop, tap **Test connection**.
Over loopback/LAN this should show your PC's name. This confirms the client works
before you add the native shell. Stop the dev server when done.

### 3. Add the iOS platform
```bash
npx cap add ios        # generates mobile/ios/ (Xcode project + Pods)
npx cap sync ios       # copies web build + native deps into the iOS project
```

### 4. Edit `mobile/ios/App/App/Info.plist` — three additions
These cannot be set on Windows and are **required** for the app to talk to a LAN
device and to open pairing links later. Add inside the top-level `<dict>`:

```xml
<!-- 1. Register the aether:// scheme so scanning the desktop's pairing QR opens
     the app (used in Phase 1; harmless to add now). -->
<key>CFBundleURLTypes</key>
<array>
  <dict>
    <key>CFBundleURLName</key>
    <string>com.ffxivguide.companion</string>
    <key>CFBundleURLSchemes</key>
    <array><string>aether</string></array>
  </dict>
</array>

<!-- 2. Allow plaintext HTTP to local/LAN addresses (Tailscale encrypts at the
     network layer; bare LAN is plain http). -->
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key>
  <true/>
</dict>

<!-- 3. Local Network usage string — iOS 14+ prompts the user; without this key
     the app is rejected/blocked from LAN access. -->
<key>NSLocalNetworkUsageDescription</key>
<string>Aether Companion connects to the Aether Intelligence app running on your computer over your local network.</string>
```

Then re-sync:
```bash
npx cap sync ios
```

### 5. Open in Xcode and run on your iPhone
```bash
npx cap open ios
```
In Xcode:
1. Select the **App** target → **Signing & Capabilities** → set your **Team**
   (your Apple ID; enable "Automatically manage signing"). Change the bundle
   identifier if `com.ffxivguide.companion` collides.
2. Plug in the iPhone, select it as the run destination.
3. **Run** (⌘R). First run: on the phone, trust the developer profile under
   Settings → General → VPN & Device Management.

### 6. Verify the milestone on device
- The app opens to the **Connect** screen.
- Type your desktop's `host:port` (e.g. `192.168.1.75:8756` or the Tailscale name).
- Tap **Test connection** → it should show your PC's **server name** and a
  "Reachable" pill. That's Phase 0 done.

---

## Verification checklist

- [ ] `npm run build` succeeds on the Mac.
- [ ] `npx cap add ios` + `npx cap sync ios` complete without errors.
- [ ] The three Info.plist keys are present.
- [ ] The app builds and launches on a physical iPhone.
- [ ] With the desktop app open (companion enabled + restarted) and both devices on
      the same LAN/tailnet, **Test connection** shows the desktop's name.
- [ ] Force-quit and relaunch the app → it remembers the host and auto-tests
      (proves `store.ts` persistence works on device).

If the last two pass, report success and stop. Do not start Phase 1.

---

## Troubleshooting

- **"Can't reach it" on device but works in the desktop browser.** The phone is
  hitting a different network path. Confirm the phone is on the same Wi-Fi (not
  cellular) or that both devices are on the same tailnet; try the Tailscale name
  instead of the LAN IP. Also confirm the desktop was **restarted** after enabling
  companion access (the bind only opens on restart).
- **iOS "Local Network" prompt never appears / connection silently fails.** Ensure
  `NSLocalNetworkUsageDescription` and `NSAllowsLocalNetworking` are in Info.plist
  and you re-ran `npx cap sync ios`.
- **App loads blank / white.** Check `webDir` is `dist` and `npm run build` ran
  before `cap sync`. `base: "./"` in vite.config.ts is required (relative asset URLs).
- **CocoaPods errors on `cap add ios`.** `cd mobile/ios/App && pod install --repo-update`.
- **Signing errors.** Set a Team in Xcode; free Apple accounts work but the build
  expires after 7 days (re-run from Xcode to refresh).

---

## The desktop API contract (already shipped, v2.0.0)

Phase 0 uses only the first row. The rest is context for later phases — **do not
implement them now.**

| Route | Method | Phase | Notes |
|---|---|---|---|
| `/health` | GET | **0** | Open route (no token). Returns `{ ok, app, server_id, server_name }`. |
| `/pair/start` | POST | (desktop UI) | Desktop mints the code/QR — not called by the phone. |
| `/pair/claim` | POST | 1 | Phone trades a scanned code for a device token. |
| `/chat` | POST | 2 | SSE-over-POST stream; `streamChat` in `client.ts` already parses it. |

Auth model: loopback (the desktop app) is never gated; every **non-loopback**
request needs `Authorization: Bearer <token>` once companion access is on. `/health`
and `/pair/claim` are the only open non-loopback routes.

---

## Scope boundary — do NOT build these in Phase 0

Deferred to later phases (each is its own handoff when Phase 0 is verified):
- **Phase 1 — Pairing:** QR scanner (add `@capacitor-mlkit/barcode-scanning` or
  `@capacitor-community/barcode-scanner`), deep-link handling for `aether://pair`
  via `@capacitor/app`, `POST /pair/claim`, and moving the token into **Keychain**
  secure storage.
- **Phase 2 — Ask:** the chat screen over the existing `streamChat`.
- **Phase 3 — Polish:** reconnect/roaming states, re-pair/forget, background return.

Keep Phase 0 to the single Connect screen and the `/health` check. Report back when
the on-device milestone passes.
