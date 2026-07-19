# Code signing

The installer is currently **unsigned**, so Windows SmartScreen warns ("Windows
protected your PC" → *More info → Run anyway*) and macOS Gatekeeper needs a
right-click → Open. Signing removes those. **You must buy a certificate** — it
can't be scripted or shared — but the build is already wired so signing is one
step once you have one.

## Windows — pick a certificate

| Option | Cost | Removes SmartScreen warning? | Notes |
|---|---|---|---|
| **EV code-signing cert** | ~$300–700/yr | **Yes, immediately** | Requires a hardware token (USB) or cloud HSM. Best if you want zero warnings now. |
| **OV code-signing cert** | ~$200–400/yr | Eventually (reputation builds) | Now also requires a hardware token/HSM (CA/B rules). Warnings persist until enough installs. |
| **Azure Trusted Signing** | ~$10/mo | Yes (good reputation) | Microsoft cloud signing, no hardware token. Cheapest modern path; needs eligibility (individual or org). |
| Self-signed | free | **No** | Only removes "unknown publisher" for cert you manually trust. Not for distribution. |

Recommendation: **Azure Trusted Signing** if you qualify (cheapest, no token), else an **EV cert** for instant clean installs.

## Windows — enable signing (after you have a cert)

**A cert in the Windows cert store (EV/OV via token):**
1. Install the cert (the token vendor's tool, or import the `.pfx`).
2. Find its SHA1 thumbprint: PowerShell `Get-ChildItem Cert:\CurrentUser\My | Format-List Subject, Thumbprint`.
3. Add it to `app/src-tauri/tauri.conf.json` under `bundle.windows`:
   ```json
   "certificateThumbprint": "YOURTHUMBPRINTNOSPACES"
   ```
4. Rebuild: `pwsh -File scripts\build-installer.ps1`. Tauri signs the `.exe`/`.msi` automatically (timestamp + sha256 are already configured).

**Azure Trusted Signing** — instead of a thumbprint, add a `signCommand` in
`bundle.windows` that invokes `signtool` with the Trusted Signing dlib
(`Azure.CodeSigning.Dlib`). See Microsoft's Trusted Signing + Tauri docs for the
exact `signtool sign /v /debug /dlib ... /dmdf ...` command; drop it into
`"signCommand": "..."` and rebuild.

## macOS — separate (on the Mac)

Needs an **Apple Developer ID** ($99/yr). Set these env vars before
`bash scripts/build-installer.sh`, and Tauri signs + you notarize:
```
export APPLE_SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export APPLE_ID="you@example.com"
export APPLE_PASSWORD="app-specific-password"
export APPLE_TEAM_ID="TEAMID"
```
Without these it builds unsigned (Gatekeeper right-click → Open still works for you).

## What I can't do

I can't purchase a certificate or handle its private key/credentials — those are
yours. Everything else (config, timestamp, digest, the build) is done, so it's
literally: get cert → add one line (or the signCommand) → rebuild.
