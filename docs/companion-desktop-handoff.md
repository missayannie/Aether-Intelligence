# Companion desktop handoff — Windows side of Phase 0

**You are a coding agent on the author's Windows PC.** Your job is to get the
Aether Intelligence desktop app (v2.0.0) running with **companion access enabled**
so an already-built iPhone app can reach `GET /health` over the LAN or Tailscale.

**The iOS side is already done — do not touch it.** On the Mac, the companion app
has been built, signed, installed and launched on the physical iPhone. The only
thing standing between us and the Phase 0 milestone is the desktop end.

See `docs/ios-companion-phase-0.md` for the phone side and the scope boundary.

---

## The milestone you're unblocking

On the iPhone: type your PC's `host:port`, tap **Test connection**, and it shows
**your PC's name**. That's it. When that works, Phase 0 is verified.

You are responsible for everything on the PC that makes that request succeed.

---

## The one non-obvious thing

There are **two separate gates**, and they behave differently:

| Gate | Where | When it's read |
|---|---|---|
| The **bind** (does the server listen off-loopback at all?) | `backend/run_backend.py::_bind_host` | **At startup only** |
| The **auth gate** (is companion access on?) | `backend/pairing.py::companion_enabled` | **Live, every request** |

So flipping the Settings toggle takes effect immediately for authorization but
**the socket stays bound to `127.0.0.1` until the app restarts**. If you enable
companion access and don't restart, the phone gets connection-refused and
everything looks broken. **Restart the app after enabling. Every time.**

---

## Steps

### 1. Confirm the app is v2.0.0

`app/package.json` and `app/src-tauri/tauri.conf.json` should both read `2.0.0`.
There is **no auto-updater** — if the installed build is older, updating means
pull latest source → rebuild → reinstall (`scripts/build-installer.ps1`).

### 2. Enable companion access

In the running app: **Settings → Companion devices → enable**.

That writes `companion_enabled: true` into `app_settings.json`. Its location
depends on how the app is running:

| How it runs | `app_settings.json` lives in |
|---|---|
| Installed / packaged build | `%LOCALAPPDATA%\EorzeaAssistant\` |
| Dev mode (`scripts/start-dev.ps1`) | the **repo root** |

Prefer the Settings UI. Only hand-edit the JSON if the UI isn't reachable.

### 3. Restart the app

Fully quit and relaunch — see "The one non-obvious thing" above. Confirm the
server is now listening on all interfaces, not just loopback:

```powershell
netstat -ano | Select-String ":8756"
```

You want `0.0.0.0:8756`. If it says `127.0.0.1:8756`, the restart didn't pick up
the setting — re-check step 2 wrote to the right file.

### 4. Open the firewall — this is the step that usually bites

Windows Firewall blocks inbound 8756 by default, and it fails *silently* from the
phone's point of view. In an **admin** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Aether Intelligence companion" -Direction Inbound -Protocol TCP -LocalPort 8756 -Action Allow -Profile Private
```

Scoped to the **Private** profile deliberately — don't open this on public
networks. Which means the active network must actually *be* Private:

```powershell
Get-NetConnectionProfile
```

If `NetworkCategory` is `Public`, LAN traffic is blocked regardless of the rule.
Change it to Private for your home network.

*(If both machines are on Tailscale, the tailnet interface is its own profile —
if the LAN path is being difficult, Tailscale is the more reliable route and is
encrypted end to end.)*

### 5. Get the address to type into the phone

With companion access on, the app's **Settings → Companion devices** panel lists
reachable `host:port` candidates, most-reachable first: Tailscale name, Tailscale
IP, then LAN IP. Any one of them works for Phase 0.

Equivalently, from the PC:

```powershell
curl.exe -s http://127.0.0.1:8756/pair/devices
```

The `hosts` array in the response is the same list. Send the author one of those
strings — e.g. `desktop.tailnet.ts.net:8756` or `192.168.1.75:8756`.

### 6. Self-test before handing it back

From the PC, hit the server by its **LAN/Tailscale address, not loopback** —
loopback is never gated, so it proves nothing:

```powershell
curl.exe -s http://<the-host-from-step-5>/health
```

Expect:

```json
{"ok":true,"app":"Aether Intelligence","server_id":"…","server_name":"<your PC name>"}
```

If that returns `{"detail":"Companion access is off on this PC."}` with a 403,
the toggle isn't actually on. If it hangs or refuses, it's the bind (step 3) or
the firewall (step 4).

---

## Verification checklist

- [ ] App is v2.0.0 and running.
- [ ] Settings → Companion devices is **enabled**.
- [ ] App was **restarted after** enabling.
- [ ] `netstat` shows `0.0.0.0:8756`, not `127.0.0.1:8756`.
- [ ] Inbound firewall rule for TCP 8756 exists on the Private profile.
- [ ] `Get-NetConnectionProfile` says Private (or you're going over Tailscale).
- [ ] `curl` to the **non-loopback** address returns `ok:true` and the PC's name.
- [ ] Reported the working `host:port` back to the author.

When the last two pass, the author taps **Test connection** on the phone and
Phase 0 is done.

---

## Do NOT do these

- **Don't run `/pair/start` or mint a pairing code.** Phase 0 uses `/health` only.
  Pairing is Phase 1 and is a separate handoff.
- **Don't send a device token anywhere.** Tokens are minted on the phone's side of
  a scan; nothing in Phase 0 needs one.
- **Don't enter API keys or run `claude setup-token`.** Those are the author's to
  provide, and `/health` needs no credential.
- **Don't bind with `FFXIV_BIND_HOST=0.0.0.0` as a shortcut** instead of the
  Settings toggle. The env var is a dev override that bypasses the toggle — the
  auth gate would then reject the phone anyway, and you'd be debugging a state
  the real app never enters.
- **Don't touch `mobile/`.** That's the Mac's job and it's finished.

---

## Security note (worth stating plainly)

Enabling this binds the API to all interfaces. That is the intended design, and
it's gated: every non-loopback request needs `Authorization: Bearer <token>`, with
exactly two exceptions — `/health` (liveness only, returns the PC's name and a
random install id) and `/pair/claim` (which needs a valid, single-use, short-TTL
code). Loopback is never gated, so the desktop app itself is unaffected.

Keeping the firewall rule on the Private profile is what keeps this off coffee-shop
Wi-Fi. Turn the toggle back off when you're done testing if you'd rather not leave
it listening.
