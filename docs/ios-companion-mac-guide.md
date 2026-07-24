# iOS Companion — the Mac side (everything that needs a Mac)

One place that lists **everything only the Mac can do**, across all phases. The
per-phase detail lives in the `ios-companion-phase-*.md` handoffs; this is the
consolidated "what's my job" view for the macOS machine.

## The dividing line

- **Windows / this repo produces:** all the web + TypeScript app code (React UI,
  `client.ts`, `store.ts`, `pairing.ts`, the screens) and the desktop backend it
  talks to. This is written and verified on the web target on Windows.
- **The Mac produces:** the signed native iOS app on a physical iPhone. It pulls
  the code via git and owns everything Xcode/CocoaPods/device-only.

The Mac **never writes app logic** — if you find yourself designing a feature,
that belongs on the Windows side. The Mac's job is native config, signing,
building, and device testing.

---

## What only the Mac can do (the complete list)

1. **Generate the native iOS project** — `npx cap add ios` (creates `mobile/ios/`).
2. **CocoaPods** — `pod install` (pulls native plugin pods). macOS-only.
3. **Edit `Info.plist`** — the native permission/scheme keys. (The file only
   exists after `cap add ios`, so effectively Mac-only.)
4. **Code signing** — set the Team (your Apple ID) in Xcode; manage the
   provisioning profile. On a free account, **re-sign every 7 days**.
5. **Build & run on the iPhone** — Xcode ⌘R to the device.
6. **Device-only testing** — the camera scanner, the Keychain, the iOS Local
   Network permission prompt, and real off-Wi-Fi (cellular + Tailscale) behavior.
   None of these can be exercised in a browser or the Simulator.

Everything else — the UI, the pairing logic, the streaming client — is written on
Windows and arrives via `git pull`.

---

## One-time setup (prerequisites)

| Need | Check | Install |
|---|---|---|
| Xcode + CLT | `xcodebuild -version` | App Store, then `xcode-select --install` |
| Node ≥ 20 | `node -v` | nvm / brew |
| CocoaPods | `pod --version` | `sudo gem install cocoapods` or `brew install cocoapods` |
| Apple account | — | Free tier works (7-day signing) |
| iPhone + cable | — | Real device required for camera/Local-Network/Tailscale |

The Mac does **not** need Tailscale for building — only the phone needs it (to
reach the desktop). The desktop (Windows) must be running v2.0.0 with companion
access enabled + restarted, and the firewall rule in place (all done).

---

## The recurring loop (every time the Windows side changes code)

```bash
cd mobile
git pull                 # get the latest app code from the Windows side
npm install              # only when a phase adds a new plugin
npm run build            # compile the web app -> dist/
npx cap sync ios         # copy the build + native deps into the iOS project
npx cap open ios         # then build & run on the iPhone from Xcode
```

That loop is the entirety of the Mac's involvement in most phases.

---

## Native config, all in one place

Everything that goes in `mobile/ios/App/App/Info.plist`:

| Key | Value | Added in | Why |
|---|---|---|---|
| `NSAppTransportSecurity` → `NSAllowsArbitraryLoads` | `true` | Phase 0 | Allow cleartext http to LAN/Tailscale (`100.x` isn't covered by `NSAllowsLocalNetworking`). **Keep it.** |
| `NSLocalNetworkUsageDescription` | a string | Phase 0 | iOS 14+ LAN-access prompt |
| `CFBundleURLTypes` → scheme `aether` | — | Phase 0 | Opens the app from a scanned `aether://pair` link |
| `NSCameraUsageDescription` | a string | Phase 1 | The QR scanner |

Native plugins that trigger `pod install`:

| Plugin | Added in |
|---|---|
| `@capacitor/ios` | Phase 0 |
| `@capacitor-mlkit/barcode-scanning` | Phase 1 |
| `@aparajita/capacitor-secure-storage` | Phase 1 |

---

## Per-phase Mac work

### Phase 0 — DONE ✅
`cap add ios`, the three Info.plist keys, signing, run on device. Verified: the
phone reaches `/health` over Tailscale.

### Phase 1 — the current Mac task 🔶
Full detail: `docs/ios-companion-phase-1.md`. Native bits only:
- `npm install @capacitor-mlkit/barcode-scanning` → fill in `scanQr()` in
  `Pair.tsx` (~5 lines, provided) → add `NSCameraUsageDescription`.
- `npm install @aparajita/capacitor-secure-storage` → add `secure.ts` → route the
  token through the Keychain in `store.ts` (one-file change, provided).
- `cap sync`, rebuild, and test on device: scan the desktop QR → paired; relaunch
  → still paired; **Forget** → back to the Pair screen.

### Phase 2 — Ask (chat) ⬜
The chat screen + logic will be **scaffolded on Windows** (the SSE stream parser
is already ported into `client.ts`). No new native plugin, no Info.plist change.
Mac work = just the recurring loop, then test streamed answers on the phone.

### Phase 3 — Polish ⬜
Reconnect/roaming, re-pair, background-return — mostly JavaScript, scaffolded on
Windows. Mac work = the recurring loop, plus device testing of the things only a
real phone shows: background→foreground restore and behavior off Wi-Fi (cellular
+ Tailscale).

---

## What the Mac does NOT do

- Write UI or app logic (Windows side; pulled via git).
- Touch the desktop backend (done + shipped in v2.0.0).
- Generate the pairing QR (the desktop does that).
- Manage pairing endpoints or tokens server-side (backend, done).

If a task doesn't require Xcode, CocoaPods, code signing, or the physical device,
it isn't the Mac's job — flag it back to the Windows side.
