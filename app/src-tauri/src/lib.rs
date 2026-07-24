// Aether Intelligence — Tauri shell.
// Spawns the bundled Python backend (a PyInstaller sidecar) on startup and kills
// it when the window closes, so the user sees one app, not two processes.

use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewUrl};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

mod overlay;

// Holds the backend child process so we can kill it on exit.
struct BackendProcess(Mutex<Option<CommandChild>>);

// "Keep running in the background": closing the main window hides it to the
// system tray (overlay + backend keep working) instead of quitting. Pushed
// from the frontend's Settings on startup and on change.
struct CloseToTray(std::sync::atomic::AtomicBool);

fn kill_backend(app: &AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        if let Some(child) = state.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Run a downloaded installer and quit so it can replace us.
///
/// Guarded on purpose — this executes a binary. The path must be the file the
/// BACKEND downloaded: inside the app-data `updates` directory, named the way
/// updates.py names it, and an .exe that exists. Anything else is refused, so
/// a stray path from the frontend (or anywhere else) can't run arbitrary code.
/// `silent` maps to the NSIS /S flag; the app exits either way.
#[tauri::command]
fn install_update(app: AppHandle, path: String, silent: bool) -> Result<(), String> {
    use std::path::Path;
    let p = Path::new(&path);
    let name_ok = p
        .file_name()
        .and_then(|n| n.to_str())
        .map(|n| n.starts_with("AetherIntelligence-") && n.ends_with("-setup.exe"))
        .unwrap_or(false);
    let dir_ok = p
        .parent()
        .and_then(|d| d.file_name())
        .and_then(|d| d.to_str())
        .map(|d| d.eq_ignore_ascii_case("updates"))
        .unwrap_or(false);
    if !name_ok || !dir_ok || !p.is_file() {
        return Err("Refusing to run that path — not a downloaded installer.".into());
    }
    let mut cmd = std::process::Command::new(p);
    if silent {
        // NSIS silent install, then relaunch.
        cmd.args(["/S"]);
    }
    cmd.spawn().map_err(err_str)?;
    // Give the installer a moment to start before we release our files.
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(600));
        kill_backend(&handle);
        handle.exit(0);
    });
    Ok(())
}

#[tauri::command]
fn set_close_to_tray(app: AppHandle, enabled: bool) {
    if let Some(s) = app.try_state::<CloseToTray>() {
        s.0.store(enabled, std::sync::atomic::Ordering::Relaxed);
    }
}

// ---------------------------------------------------------------------------
// Companion firewall rule. When companion access is on, the backend binds
// off-loopback so a paired phone on the LAN can reach it — which Windows Firewall
// blocks by default. Rather than opening the firewall for EVERY install, we add
// the rule the moment the user enables companion access and remove it when they
// disable: least privilege, lifecycle tied to the feature, so anyone who never
// pairs a phone is never exposed. Scoped tight — this program only, TCP 8756,
// Private+Domain profiles (never public Wi-Fi). Needs admin, so it raises one
// UAC prompt.
#[tauri::command]
async fn set_companion_firewall(enabled: bool) -> Result<(), String> {
    #[cfg(windows)]
    {
        firewall_apply(enabled)
    }
    #[cfg(not(windows))]
    {
        let _ = enabled;
        Ok(())
    }
}

#[cfg(windows)]
fn firewall_apply(enabled: bool) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const RULE: &str = "Aether Intelligence Companion";

    // Program-scope the rule to our own backend binary, by full install path.
    let backend = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("backend.exe")))
        .ok_or("Couldn't locate the backend executable.")?;
    let backend = backend.to_string_lossy().to_string();

    // A .bat keeps netsh's spaced program path / rule name out of any shell
    // quoting layer. Always delete first so re-enabling can't stack duplicates.
    let mut script = format!(
        "@echo off\r\nnetsh advfirewall firewall delete rule name=\"{RULE}\" >nul 2>&1\r\n"
    );
    if enabled {
        script.push_str(&format!(
            "netsh advfirewall firewall add rule name=\"{RULE}\" dir=in action=allow \
             program=\"{backend}\" protocol=TCP localport=8756 profile=private,domain enable=yes\r\n"
        ));
    }
    script.push_str("exit /b %errorlevel%\r\n");

    let bat = std::env::temp_dir().join(format!("aether-fw-{}.bat", std::process::id()));
    std::fs::write(&bat, script).map_err(|e| format!("Couldn't write the firewall script: {e}"))?;

    // Elevate the .bat via PowerShell RunAs (one UAC prompt). -Wait -PassThru
    // lets us read netsh's exit code; a declined UAC throws and we map it to 1223.
    let ps = format!(
        "try {{ $p = Start-Process -FilePath '{}' -Verb RunAs -Wait -PassThru \
         -WindowStyle Hidden -ErrorAction Stop; exit $p.ExitCode }} catch {{ exit 1223 }}",
        bat.to_string_lossy()
    );
    let status = std::process::Command::new("powershell")
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps.as_str()])
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    let _ = std::fs::remove_file(&bat);

    match status {
        Ok(s) if s.success() => Ok(()),
        Ok(s) if s.code() == Some(1223) => {
            Err("Administrator approval is needed to open the firewall — it was cancelled.".into())
        }
        Ok(s) => Err(format!("The firewall change failed (code {}).", s.code().unwrap_or(-1))),
        Err(e) => Err(format!("Couldn't run the firewall command: {e}")),
    }
}

// ---------------------------------------------------------------------------
// Overlay hotkeys — editable in Settings. The plugin handler compares against
// THIS state, so re-binding is just: unregister, register, swap the state.
const HK_ASK_DEFAULT: &str = "Alt+Backquote";
// Alt+Shift+`, NOT Alt+Win+`: Windows reserves several Win+Alt combos (Game
// Bar, HDR) and a swallowed ambient hotkey looked like the pill misfiring.
const HK_AMBIENT_DEFAULT: &str = "Alt+Shift+Backquote";
const HK_KILL_DEFAULT: &str = "Alt+Backslash";
const HK_DRAWER_DEFAULT: &str = "Alt+D";

struct OverlayHotkeys(Mutex<HotkeySet>);

struct HotkeySet {
    ask: tauri_plugin_global_shortcut::Shortcut,
    ambient: tauri_plugin_global_shortcut::Shortcut,
    kill: tauri_plugin_global_shortcut::Shortcut,
    drawer: tauri_plugin_global_shortcut::Shortcut,
}

impl HotkeySet {
    fn defaults() -> Self {
        Self {
            ask: HK_ASK_DEFAULT.parse().expect("default ask hotkey parses"),
            ambient: HK_AMBIENT_DEFAULT.parse().expect("default ambient hotkey parses"),
            kill: HK_KILL_DEFAULT.parse().expect("default kill hotkey parses"),
            drawer: HK_DRAWER_DEFAULT.parse().expect("default drawer hotkey parses"),
        }
    }
}

/// Re-bind the three overlay shortcuts. Rejects unparseable or duplicate
/// combos with a human-readable error the Settings UI shows verbatim; on any
/// failure the previous bindings stay registered.
#[tauri::command]
fn set_overlay_hotkeys(
    app: AppHandle,
    ask: String,
    ambient: String,
    kill: String,
    drawer: String,
) -> Result<(), String> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut};
    let a: Shortcut = ask.parse().map_err(|_| format!("Can't parse \"{ask}\""))?;
    let b: Shortcut = ambient.parse().map_err(|_| format!("Can't parse \"{ambient}\""))?;
    let k: Shortcut = kill.parse().map_err(|_| format!("Can't parse \"{kill}\""))?;
    let d: Shortcut = drawer.parse().map_err(|_| format!("Can't parse \"{drawer}\""))?;
    let all = [a, b, k, d];
    for i in 0..all.len() {
        for j in (i + 1)..all.len() {
            if all[i] == all[j] {
                return Err("Each shortcut must be different.".into());
            }
        }
    }
    let gs = app.global_shortcut();
    let _ = gs.unregister_all();
    let register_all = || -> Result<(), String> {
        gs.register(a).map_err(|e| format!("\"{ask}\": {e}"))?;
        gs.register(b).map_err(|e| format!("\"{ambient}\": {e}"))?;
        gs.register(k).map_err(|e| format!("\"{kill}\": {e}"))?;
        gs.register(d).map_err(|e| format!("\"{drawer}\": {e}"))?;
        Ok(())
    };
    if let Err(e) = register_all() {
        // Roll back to whatever was bound before, so the overlay never ends
        // up hotkey-less because one combo was taken by another app.
        let _ = gs.unregister_all();
        if let Some(hk) = app.try_state::<OverlayHotkeys>() {
            let set = hk.0.lock().unwrap();
            let _ = gs.register(set.ask);
            let _ = gs.register(set.ambient);
            let _ = gs.register(set.kill);
            let _ = gs.register(set.drawer);
        }
        return Err(e);
    }
    if let Some(hk) = app.try_state::<OverlayHotkeys>() {
        *hk.0.lock().unwrap() = HotkeySet { ask: a, ambient: b, kill: k, drawer: d };
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Embedded browser pane.
//
// The right pane's "browser" tab is a REAL second WebView2 view (Chromium),
// not an iframe — most sites (google.com included) refuse to be framed, and a
// native webview also gets its own cookies/history like a browser tab should.
// It's created lazily as a CHILD webview of the main window, positioned by the
// frontend over the pane's content area, and shown/hidden as the tab
// activates. Because its content is a remote URL it has NO IPC access — it is
// just a viewer and can't reach into the app.
const EMBED_LABEL: &str = "embed";

// Keep everything inside the pane: pages that target a new tab/window would
// otherwise pop a bare unstyled native window. Same-view navigation instead.
// Also the adblocker's COSMETIC half: hide the empty slots and ad iframes the
// request-level blocker (attach_adblock) leaves behind.
const EMBED_INIT: &str = r#"
(() => {
  window.open = (u) => { if (u) location.href = u; return null; };
  document.addEventListener("click", (e) => {
    const t = e.target;
    const a = t && t.closest ? t.closest("a[target=_blank]") : null;
    if (a) a.target = "_self";
  }, true);
  // Deferred: this script runs at document CREATION, before <html> exists —
  // an immediate appendChild(documentElement) would throw and skip the CSS.
  const addStyle = () => {
    const style = document.createElement("style");
    style.textContent =
      "ins.adsbygoogle,[id^='google_ads_'],[id^='div-gpt-ad']," +
      "iframe[src*='doubleclick.'],iframe[src*='googlesyndication.']," +
      "[data-ad-slot],[data-ad-client]{display:none!important}";
    (document.head || document.documentElement).appendChild(style);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", addStyle);
  } else {
    addStyle();
  }
})();
"#;

// The request-level blocklist: classic ad/tracker networks, matched by host
// suffix. Deliberately conservative — CDNs and tag managers that sites break
// without are NOT here; the goal is "no banners", not perfect purity.
#[cfg(windows)]
const AD_HOSTS: &[&str] = &[
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "google-analytics.com", "adnxs.com",
    "adsafeprotected.com", "adsrvr.org", "amazon-adsystem.com",
    "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "pubmatic.com", "rubiconproject.com", "openx.net", "moatads.com",
    "scorecardresearch.com", "quantserve.com", "smartadserver.com",
    "indexww.com", "casalemedia.com", "33across.com", "yieldmo.com",
    "sharethrough.com", "bidswitch.net", "teads.tv", "zedo.com",
    "mgid.com", "revcontent.com", "popads.net", "propellerads.com",
    "exoclick.com", "media.net", "adform.net", "yieldlab.net",
    "connect.facebook.net",
];

#[cfg(windows)]
fn is_ad_url(url: &str) -> bool {
    // Cheap host extraction — enough for a blocklist; a parse failure means allow.
    let rest = match url.split_once("://") {
        Some((_, r)) => r,
        None => return false,
    };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = host.split('@').last().unwrap_or(host);
    let host = host.split(':').next().unwrap_or(host);
    AD_HOSTS.iter().any(|d| host == *d || host.ends_with(&format!(".{d}")))
}

/// Block ad/tracker requests inside the embedded browser. WebView2-specific:
/// subscribe to WebResourceRequested and answer blocklisted hosts with an empty
/// 403 before the request leaves the machine.
#[cfg(windows)]
fn attach_adblock(wv: &tauri::Webview) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        ICoreWebView2_2, COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL,
    };
    use webview2_com::{take_pwstr, WebResourceRequestedEventHandler};
    use windows::core::{Interface, HSTRING, PWSTR};

    let _ = wv.with_webview(|pw| unsafe {
        let controller = pw.controller();
        let core = match controller.CoreWebView2() {
            Ok(c) => c,
            Err(_) => return,
        };
        if core
            .AddWebResourceRequestedFilter(&HSTRING::from("*"), COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL)
            .is_err()
        {
            return;
        }
        let env = match core.cast::<ICoreWebView2_2>().and_then(|c2| c2.Environment()) {
            Ok(e) => e,
            Err(_) => return,
        };
        let handler = WebResourceRequestedEventHandler::create(Box::new(move |_sender, args| {
            let Some(args) = args else { return Ok(()) };
            let request = args.Request()?;
            let mut uri = PWSTR::null();
            request.Uri(&mut uri)?;
            let url = take_pwstr(uri);
            if is_ad_url(&url) {
                let response = env.CreateWebResourceResponse(
                    None,
                    403,
                    &HSTRING::from("Blocked"),
                    &HSTRING::from("Content-Type: text/plain"),
                )?;
                args.SetResponse(&response)?;
            }
            Ok(())
        }));
        // The registration token is an out-param we never need again — the
        // subscription lives as long as the webview does.
        let mut token: i64 = 0;
        let _ = core.add_WebResourceRequested(&handler, &mut token);
    });
}

pub(crate) fn err_str<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

/// Position + show the embedded browser, creating it on first use.
/// x/y/w/h are PHYSICAL pixels (CSS px × devicePixelRatio, computed by the
/// frontend) relative to the window's client area. Physical on purpose: letting
/// Tauri convert "logical" values re-applies the display scale and lands the
/// view offset/missized on any monitor above 100% scaling.
/// `url` is only passed when the caller wants a navigation (first open).
#[tauri::command]
async fn browser_show(
    app: AppHandle,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    url: Option<String>,
) -> Result<(), String> {
    let pos = PhysicalPosition::new(x.round() as i32, y.round() as i32);
    let size = PhysicalSize::new(w.round().max(1.0) as u32, h.round().max(1.0) as u32);
    if let Some(wv) = app.webviews().get(EMBED_LABEL) {
        wv.set_position(pos).map_err(err_str)?;
        wv.set_size(size).map_err(err_str)?;
        wv.show().map_err(err_str)?;
        // `url` is deliberately IGNORED here: placement must never re-navigate
        // an existing view. Navigation of a live browser goes only through
        // browser_navigate — otherwise a frontend remount (dev hot-reload, or
        // a crashed pane) yanks the user's page out from under them.
        return Ok(());
    }
    let main = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let start = url.unwrap_or_else(|| "https://www.google.com".to_string());
    let handle = app.clone();
    let builder = tauri::webview::WebviewBuilder::new(
        EMBED_LABEL,
        WebviewUrl::External(start.parse().map_err(err_str)?),
    )
    .initialization_script(EMBED_INIT)
    // Every main-frame navigation is reported to the app so the URL bar can
    // follow along (typed, clicked, or redirected — all land here).
    .on_navigation(move |u| {
        let _ = handle.emit_to("main", "embed-nav", u.to_string());
        true
    });
    let created = main
        .as_ref()
        .window()
        .add_child(builder, pos, size)
        .map_err(err_str)?;
    #[cfg(windows)]
    attach_adblock(&created);
    #[cfg(not(windows))]
    let _ = created;
    Ok(())
}

#[tauri::command]
async fn browser_hide(app: AppHandle) -> Result<(), String> {
    if let Some(wv) = app.webviews().get(EMBED_LABEL) {
        wv.hide().map_err(err_str)?;
    }
    Ok(())
}

#[tauri::command]
async fn browser_navigate(app: AppHandle, url: String) -> Result<(), String> {
    let wv = app
        .webviews()
        .get(EMBED_LABEL)
        .cloned()
        .ok_or("browser not open")?;
    wv.navigate(url.parse().map_err(err_str)?).map_err(err_str)?;
    Ok(())
}

/// dir: "back" | "forward" | anything else reloads.
#[tauri::command]
async fn browser_history(app: AppHandle, dir: String) -> Result<(), String> {
    let wv = app
        .webviews()
        .get(EMBED_LABEL)
        .cloned()
        .ok_or("browser not open")?;
    let js = match dir.as_str() {
        "back" => "history.back()",
        "forward" => "history.forward()",
        _ => "location.reload()",
    };
    wv.eval(js).map_err(err_str)?;
    Ok(())
}

#[tauri::command]
async fn browser_url(app: AppHandle) -> Result<String, String> {
    let wv = app
        .webviews()
        .get(EMBED_LABEL)
        .cloned()
        .ok_or("browser not open")?;
    Ok(wv.url().map_err(err_str)?.to_string())
}

// Windows: hide the icon from the native title bar WITHOUT losing the taskbar icon.
// The title bar shows the window's *small* icon; the taskbar/Alt-Tab use the *large*
// icon. So we set a fully-transparent small icon (title bar shows nothing) and leave
// the large icon untouched (taskbar keeps the app mark). The in-app header keeps its
// own mark. No-op on non-Windows.
//
// ICON_SMALL = 0, ICON_BIG = 1 for WM_SETICON's wParam.
#[cfg(windows)]
fn strip_titlebar_icon_raw(raw: isize) {
    use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
    use windows::Win32::UI::WindowsAndMessaging::{CreateIcon, SendMessageW, WM_SETICON};
    let hwnd = HWND(raw as *mut core::ffi::c_void);
    unsafe {
        // A 16x16 monochrome icon that is fully transparent: AND-mask all 1s (keep
        // background everywhere), XOR-mask all 0s. 16*16 bits = 32 bytes per plane.
        let and_mask = [0xFFu8; 32];
        let xor_mask = [0x00u8; 32];
        if let Ok(hicon) = CreateIcon(None, 16, 16, 1, 1, and_mask.as_ptr(), xor_mask.as_ptr()) {
            // Set only the small icon (title bar); leave the large icon (taskbar) as is.
            let _ = SendMessageW(hwnd, WM_SETICON, Some(WPARAM(0)), Some(LPARAM(hicon.0 as isize)));
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        // Overlay hotkeys (docs/overlay-spec.md §4.2). Global — they work while
        // the game has focus; that's the whole point. Defaults: Alt+` summons
        // the Ask pill, Alt+Win+` shows the overlay layer without the pill,
        // Alt+\ is the kill switch — all re-bindable in Settings
        // (set_overlay_hotkeys); the handler reads the CURRENT bindings.
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    use tauri_plugin_global_shortcut::ShortcutState;
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    let Some(hk) = app.try_state::<OverlayHotkeys>() else { return };
                    let (ask, ambient, kill, drawer) = {
                        let set = hk.0.lock().unwrap();
                        (set.ask, set.ambient, set.kill, set.drawer)
                    };
                    if shortcut == &ask {
                        overlay::summon_ask(app);
                    } else if shortcut == &ambient {
                        overlay::show_ambient(app);
                    } else if shortcut == &kill {
                        overlay::toggle(app);
                    } else if shortcut == &drawer {
                        overlay::summon_drawer(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            browser_show,
            browser_hide,
            browser_navigate,
            browser_history,
            browser_url,
            overlay::overlay_set_capture,
            overlay::overlay_open_map,
            overlay::overlay_capture_screen,
            overlay::overlay_click_at,
            overlay::overlay_open_db,
            set_close_to_tray,
            set_companion_firewall,
            set_overlay_hotkeys,
            install_update
        ])
        .manage(BackendProcess(Mutex::new(None)))
        .manage(CloseToTray(std::sync::atomic::AtomicBool::new(false)))
        .manage(OverlayHotkeys(Mutex::new(HotkeySet::defaults())))
        .setup(|app| {
            // Launch the bundled backend sidecar (binaries/backend-<triple>).
            // Pass our PID so the backend can exit if we die unexpectedly.
            let sidecar = app
                .shell()
                .sidecar("backend")?
                .env("FFXIV_PARENT_PID", std::process::id().to_string());
            let (mut rx, child) = sidecar.spawn()?;
            app.state::<BackendProcess>()
                .0
                .lock()
                .unwrap()
                .replace(child);

            // Drain the sidecar's output so its pipe never blocks; log stderr.
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stderr(line) = event {
                        eprintln!("[backend] {}", String::from_utf8_lossy(&line));
                    }
                }
            });

            // Hide the icon from the native window title bar (Windows only). Apply
            // now and again shortly after — Tauri sets the window icon at show time,
            // which can land after setup and re-show the icon.
            #[cfg(windows)]
            if let Some(win) = app.get_webview_window("main") {
                if let Ok(h) = win.hwnd() {
                    let raw = h.0 as isize;
                    strip_titlebar_icon_raw(raw);
                    std::thread::spawn(move || {
                        std::thread::sleep(std::time::Duration::from_millis(800));
                        strip_titlebar_icon_raw(raw);
                    });
                }
            }

            // Register the default overlay hotkeys. The frontend re-binds from
            // saved Settings moments later (set_overlay_hotkeys); a combo
            // already taken by another app just logs — the app must not crash
            // over a shortcut.
            {
                use tauri_plugin_global_shortcut::GlobalShortcutExt;
                let set = HotkeySet::defaults();
                for sc in [set.ask, set.ambient, set.kill, set.drawer] {
                    if let Err(e) = app.global_shortcut().register(sc) {
                        eprintln!("[overlay] hotkey register failed: {e}");
                    }
                }
            }

            // Keep the overlay tied to the game: hide it if FFXIV quits while
            // it's up. The summon paths already refuse to open it without the
            // game (overlay::may_summon); this covers the other direction.
            overlay::start_ffxiv_watcher(app.handle());

            // Build the overlay window now (hidden) so the FIRST hotkey lands
            // on a warm window instead of racing a cold create — the fix for
            // "the first summon won't let me type". It stays hidden and
            // click-through until actually summoned.
            overlay::prewarm(app.handle());

            // System tray: always present, so the app is reachable while the
            // main window is hidden (the "keep running in background" setting)
            // and killable if anything ever wedges.
            {
                use tauri::menu::{Menu, MenuItem};
                use tauri::tray::TrayIconBuilder;
                let open_i =
                    MenuItem::with_id(app, "open", "Open Aether Intelligence", true, None::<&str>)?;
                let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&open_i, &quit_i])?;
                let mut tray = TrayIconBuilder::new()
                    .menu(&menu)
                    .show_menu_on_left_click(true)
                    .tooltip("Aether Intelligence")
                    .on_menu_event(|app, event| match event.id.as_ref() {
                        "open" => show_main(app),
                        "quit" => {
                            kill_backend(app);
                            app.exit(0);
                        }
                        _ => {}
                    });
                if let Some(icon) = app.default_window_icon() {
                    tray = tray.icon(icon.clone());
                }
                tray.build(app)?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            let app = window.app_handle();
            match event {
                // Main window close: hide to tray when the setting is on
                // (overlay + backend keep working), otherwise let it close.
                tauri::WindowEvent::CloseRequested { api, .. }
                    if window.label() == "main" =>
                {
                    let to_tray = app
                        .try_state::<CloseToTray>()
                        .map(|s| s.0.load(std::sync::atomic::Ordering::Relaxed))
                        .unwrap_or(false);
                    if to_tray {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
                // Main window actually gone: take the whole app down CLEANLY —
                // backend, overlay window, process. (Previously the overlay
                // kept an orphaned, backend-less process alive with no way to
                // reopen the app.)
                tauri::WindowEvent::Destroyed if window.label() == "main" => {
                    kill_backend(app);
                    if let Some(ov) = app.get_webview_window(overlay::OVERLAY_LABEL) {
                        let _ = ov.close();
                    }
                    app.exit(0);
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
