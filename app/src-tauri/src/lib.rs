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
        // the game has focus; that's the whole point. Alt+` summons the Ask
        // pill, Alt+\ is the kill switch.
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_shortcuts(["Alt+Backquote", "Alt+Backslash"])
                .expect("overlay shortcuts parse")
                .with_handler(|app, shortcut, event| {
                    use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut, ShortcutState};
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    if shortcut == &Shortcut::new(Some(Modifiers::ALT), Code::Backquote) {
                        overlay::summon_ask(app);
                    } else if shortcut == &Shortcut::new(Some(Modifiers::ALT), Code::Backslash) {
                        overlay::toggle(app);
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
            overlay::overlay_set_capture
        ])
        .manage(BackendProcess(Mutex::new(None)))
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
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.app_handle().try_state::<BackendProcess>() {
                    if let Some(child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
