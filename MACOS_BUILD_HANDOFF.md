# macOS Build Handoff — Aether Intelligence

Hand this to an agent/developer on a **Mac**. Goal: build (or **update**) the macOS
`.app`/`.dmg` installer. Tauri + PyInstaller can't cross-compile from Windows, so
the Mac build must happen on a Mac. The code is cross-platform; your job is to build
+ verify it on macOS and fix any Mac-specific snags.

There is **no auto-updater** in the app — updating = pull latest source + rebuild +
reinstall.

---

## 0. UPDATING an existing build (read first if you built before)

If you already built the app on this Mac before this revision, do this to update:

1. **Get the latest source.** The Windows author keeps it mirrored to a Google
   Drive folder (source-only). Refresh your local copy from there (or `git pull`
   if a repo was set up). **Do not build inside the Drive folder** — copy to a
   local path first (e.g. `~/Projects/ffxiv-guide`).
2. **Reinstall Python deps** — several were added since the first build
   (claude-agent-sdk, psutil, curl_cffi, pypdf, python-multipart, beautifulsoup4):
   ```bash
   cd ffxiv-guide/backend && ./.venv/bin/pip install -r requirements.txt
   cd ../app && npm install
   ```
3. **Rebuild:** `cd .. && bash scripts/build-installer.sh` → new `.app` + `.dmg`.
4. Reinstall the new `.app`. **User data (chats, keychain token) persists** — it
   lives in `~/Library/Application Support/EorzeaAssistant` + the macOS Keychain,
   not the app bundle. No re-setup.

**What changed since a pre-update build (so you know what to expect):** the Claude
subscription CLI fix; simultaneous background chats; bottom-composer with model
picker + context meter + dictation; **file/photo/folder attachments** (needs
`pypdf` + `python_multipart` — hidden-imports are already in `build-backend.sh`);
Notes tab + editable Docs; **dockable tabs** (drag tabs to a bottom panel);
Lodestone news + SqEnix community forum; 5 FFXIV themes. All are in the source you
just pulled — the build picks them up automatically.

**Signing:** see `SIGNING.md` for macOS Developer ID + notarization (unsigned still
works for the user via right-click → Open).

---

## 1. What this app is

**Aether Intelligence** — a cross-platform desktop AI assistant for Final Fantasy XIV.

- **Frontend/shell:** Tauri v2 (Rust) + React/TypeScript in `app/`. Three-pane UI
  (chat history | chat | reference panel with Map/Assets/Docs/Sources tabs).
- **Backend:** Python FastAPI in `backend/`, runs locally on `127.0.0.1:8756`.
  Bundled into the app as a **PyInstaller sidecar**; the Rust shell spawns it on
  launch and kills it on exit, so the user sees one app.
- **Features:** multi-provider LLM chat (Claude/GPT/Gemini/Grok, BYO API key) OR a
  Claude Pro/Max **subscription** path via the Claude Agent SDK; live tools for
  wikis, market prices (Universalis), Lodestone news, exact map pins, an embedded
  interactive map (A Realm Remapped), and an interactive image-annotation editor;
  5 FFXIV themes.

## 2. Prerequisites (install on the Mac)

```bash
brew install rust node python@3.12
# Tauri deps on macOS are otherwise built-in (WKWebView). Xcode CLT if prompted:
xcode-select --install   # if not already installed
```

## 3. Get the code onto the Mac

The project currently lives on the author's Windows PC at `C:\Users\cryst\Projects\
ffxiv-guide` and is mirrored (source only) to their Google Drive folder
`.../Documents/Projects/ffxiv-guide`. Pull it however you have it (Drive folder or
a git remote if one now exists). **Do NOT build inside a Google Drive / cloud-synced
folder** — it corrupts `node_modules` and Rust `target/`. Copy to a local path
first (e.g. `~/Projects/ffxiv-guide`).

## 4. Build (one command, after setup)

```bash
cd ffxiv-guide/backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install pyinstaller

cd ../app && npm install

cd .. && bash scripts/build-installer.sh
```

`scripts/build-installer.sh` (already written) does three steps:
1. `scripts/build-backend.sh` → PyInstaller compiles `backend/` into
   `backend/dist/backend` (a single Unix binary).
2. Copies it to `app/src-tauri/binaries/backend-<target-triple>` where the triple
   is from `rustc -Vv | awk '/^host:/{print $2}'` (e.g. `aarch64-apple-darwin` on
   Apple Silicon, `x86_64-apple-darwin` on Intel). **Tauri requires this exact
   naming** for `externalBin`.
3. `npm run tauri build` → outputs to
   `app/src-tauri/target/release/bundle/` (`macos/` app + `dmg/`).

## 5. Verify (do this — the Windows build had a real bug here)

1. Launch the built app (`open app/src-tauri/target/release/bundle/macos/Aether\ Intelligence.app`).
2. Confirm the backend auto-started: `curl http://127.0.0.1:8756/health` → `{"ok":true}`.
3. Quit the app. Confirm **no orphaned backend process**:
   `pgrep -fl backend` should return nothing.
   - There is a watchdog for this: `backend/run_backend.py` reads `FFXIV_PARENT_PID`
     (set by the Rust shell in `app/src-tauri/src/lib.rs`) and self-exits when the
     parent dies. If you see orphans, that's the thing to check.
4. Open **Settings → API keys & models**. It should say whether Claude Code is
   detected (macOS path check is in `backend/subscription.py::claude_cli_path`).

## 6. Architecture map (key files)

```
app/src-tauri/src/lib.rs      Rust shell: spawns backend sidecar, passes FFXIV_PARENT_PID, kills on window close
app/src-tauri/tauri.conf.json bundle.externalBin = ["binaries/backend"]; productName/window config
app/src-tauri/capabilities/default.json   shell:allow-execute for the sidecar
app/src/App.tsx               main three-pane UI, theme system, model picker, settings
app/src/AnnotationEditor.tsx  interactive annotation editor (SVG overlay)
app/src/api.ts                talks to backend at http://127.0.0.1:8756
backend/run_backend.py        PyInstaller entry; uvicorn + parent-PID watchdog
backend/app.py                FastAPI routes
backend/llm/dispatch.py       routes to API engine (litellm) or subscription engine (Agent SDK)
backend/sources/*.py          wiki, universalis, maps, realmremapped, lodestone clients
backend/paths.py              data dir: ~/Library/Application Support/EorzeaAssistant on macOS
scripts/build-*.sh            the macOS build scripts (this handoff's subject)
scripts/build-*.ps1           Windows equivalents (reference only)
```

## 7. Gotchas / lessons from the Windows build

- **PyInstaller onefile = bootloader + worker.** Killing the tracked process can
  orphan the worker — hence the parent-PID watchdog (`run_backend.py` + psutil).
  Verify shutdown on Mac too.
- **keyring backend:** Windows used `keyring.backends.Windows`; the Mac script adds
  `--hidden-import keyring.backends.macOS`. If keychain access fails at runtime,
  that's the first thing to check.
- **curl_cffi** is used for sites behind Cloudflare (Gamer Escape, Lodestone, A
  Realm Remapped). It bundles a native lib — confirm `--collect-all curl_cffi`
  actually pulled the dylib (the server failing to start on import = a clue).
- **litellm** needs `--collect-all litellm` (model-cost data + providers). Already
  in the script.
- If `tauri build` complains it can't find the sidecar, the triple in the filename
  doesn't match `rustc`'s host triple — re-check step 4.2.
- **Gatekeeper:** the unsigned `.app` needs right-click → Open the first time, or
  sign + notarize with an Apple Developer ID for real distribution.

## 8. Credentials (important)

The app needs a credential to actually chat, but **you (the agent) must not enter
API keys or run `claude setup-token`** — those are the user's to provide. Building
and the launch/shutdown verification do NOT require a credential. Leave the live
chat test to the user.

## 9. If something breaks

The build scripts are correct in principle but unverified on macOS (first Mac
build). Likely tweak points: a PyInstaller hidden-import the Mac needs, or the
sidecar triple. Capture the exact error and report back — the Windows build hit
the same class of issues and they were quick fixes.
