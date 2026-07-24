# iOS Companion — Phase 1 handoff (for the Mac agent)

**You are a coding agent on a macOS machine.** Phase 0 is done and verified —
the phone reaches the desktop's `/health` over Tailscale. Phase 1 makes the phone
**pair**: scan the desktop's QR, trade the code for a device token, store that
token in the **iOS Keychain**, and thereafter authenticate every request.

The pairing *logic* is already written and **verified end-to-end on the web
target** (against the real desktop backend, exercising the non-loopback token
gate over the Tailscale address). Your job is the two native pieces the web
can't do — the **camera scanner** and **Keychain storage** — plus the rebuild.

Read `docs/ios-companion-phase-0.md` first if you haven't; this builds on it.

---

## Milestone

On the iPhone: tap **Scan QR code**, point it at the desktop's pairing QR
(Settings → Companion devices → **Pair a device**), and the app lands on the
**Paired** screen showing your PC's name and "N models available". Force-quit and
relaunch → it opens straight to Paired (token restored from the Keychain).

---

## What already exists (verified on web — do not rewrite)

- **`src/lib/pairing.ts`** — `parsePairUri()` (parses `aether://pair?…`),
  `claim()` (POSTs `/pair/claim`, tries each host in order), and `claimFromUri()`
  (parse → claim → persist → point the client at the desktop). Tested against the
  live backend: a real code was claimed over `100.67.4.104:8756`, the token
  authorized `/models` through the gate, and the device appeared in the desktop's
  paired list.
- **`src/screens/Pair.tsx`** — the pairing screen: a **Scan QR code** button
  (`scanQr()` is a stub for you to fill) and a paste-the-link fallback that already
  works.
- **`src/screens/Paired.tsx`** — post-pairing status; its "N models available" is a
  real authed call, so it doubles as proof the token works.
- **`src/App.tsx`** — restores a stored pairing on launch, and has an
  `@capacitor/app` `appUrlOpen` handler so opening `aether://pair?…` from the iOS
  Camera also pairs. (The `aether://` scheme was registered in Phase 0's Info.plist.)
- **`src/lib/client.ts`** — already attaches `Authorization: Bearer <token>` once
  `setConnection(base, token)` has run.
- **`src/lib/store.ts`** — persistence + a stable per-install `getDeviceId()`.
  **Currently the token lives in `@capacitor/preferences` (NOT the Keychain)** —
  that's the one thing you must fix (below).

---

## Your work

### 1. Camera scanner

```bash
cd mobile
npm install @capacitor-mlkit/barcode-scanning
npx cap sync ios
```

Fill in the stub in `src/screens/Pair.tsx` — replace `scanQr()` with:

```ts
import { BarcodeScanner } from "@capacitor-mlkit/barcode-scanning";

async function scanQr(): Promise<string | null> {
  const perm = await BarcodeScanner.requestPermissions();
  if (perm.camera !== "granted" && perm.camera !== "limited") return null;
  const { barcodes } = await BarcodeScanner.scan();
  return barcodes[0]?.rawValue ?? null;   // the aether://pair?… text
}
```

The returned text goes straight into `claimFromUri` (the existing paste path) — no
other change needed. Add to `mobile/ios/App/App/Info.plist`:

```xml
<key>NSCameraUsageDescription</key>
<string>Aether Companion scans the pairing QR shown by the app on your computer.</string>
```

### 2. Move the token to the Keychain

```bash
npm install @aparajita/capacitor-secure-storage
npx cap sync ios
```

Create `src/lib/secure.ts`:

```ts
import { SecureStorage } from "@aparajita/capacitor-secure-storage";
export const setSecret = (k: string, v: string) => SecureStorage.set(k, v);
export const getSecret = (k: string) => SecureStorage.get(k) as Promise<string | null>;
export const removeSecret = (k: string) => SecureStorage.remove(k);
```

Then in `src/lib/store.ts`, keep `host`/`serverId`/`serverName` in Preferences but
route the **token** through `secure.ts`: on `saveConnection` write the token with
`setSecret("aether.token", c.token)` and store the rest without it; on
`loadConnection` read the token back with `getSecret` and merge it in; on
`clearConnection` call `removeSecret`. The `Connection` shape stays the same to the
rest of the app — only this file changes. (`@aparajita/capacitor-secure-storage`
falls back to web storage in a browser, so `npm run dev` still works.)

### 3. Rebuild & run

```bash
npm run build && npx cap sync ios && npx cap open ios
```
Run on the iPhone from Xcode (Team already set in Phase 0).

---

## Verification

- [ ] `npm run build` succeeds after both plugin additions.
- [ ] On device: **Scan QR code** opens the camera, scanning the desktop QR pairs.
- [ ] Paired screen shows the PC name + "N models available".
- [ ] The device appears under desktop **Settings → Companion devices**.
- [ ] Force-quit + relaunch → opens straight to Paired (token from Keychain).
- [ ] **Forget this desktop** → next launch shows the Pair screen again; the device
      is gone from the desktop list.
- [ ] (Optional) Confirm the token is in the Keychain, not Preferences — the app
      still pairs after clearing app Preferences but not after a Keychain wipe.

---

## Notes & gotchas

- **`@capacitor-mlkit/barcode-scanning`** pulls in Google ML Kit pods (sizeable) and
  needs iOS 15+. If `pod install` complains, `cd mobile/ios/App && pod install --repo-update`.
- **The plaintext-HTTP / ATS fix from Phase 0 still applies** — `NSAllowsArbitraryLoads`
  must be in Info.plist, or the claim call to a `100.x` Tailscale host is blocked the
  same way `/health` was.
- **Don't change the desktop backend** — `/pair/claim` and the gate are done and
  shipped in v2.0.0. Phase 1 is entirely phone-side.
- **The pairing code is single-use and expires in 120s.** If a scan fails, the user
  re-generates on the desktop; the app already surfaces that error.

---

## Scope boundary — Phase 2 (not now)

Once pairing works on device, the next handoff is **Ask (chat)**: a mobile chat
screen over `streamChat` in `client.ts` (already ported from the desktop). Do not
build it here. Report back when the pairing milestone passes.
