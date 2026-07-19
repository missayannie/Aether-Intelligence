// Aether Overlay — the in-game layer (docs/overlay-spec.md).
//
// One extra top-level window: transparent, undecorated, always-on-top,
// skip-taskbar, sized to the primary monitor, and CLICK-THROUGH by default
// (set_ignore_cursor_events). It renders the same React bundle as the app —
// main.tsx branches on ?overlay=1 / the window label — and talks to the same
// backend. FFXIV must be in Borderless Windowed for any overlay to show.
//
// The capture contract: ambient widgets never take input; summoned surfaces
// (the Ask pill) flip ignore_cursor_events off and take focus; on release the
// previously-focused window (the game) gets focus back.
//
// Global shortcuts (registered in lib.rs):
//   Alt+`  summon the Ask pill (creates the window on first use)
//   Alt+\  kill switch — hide/show the whole overlay

use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

pub const OVERLAY_LABEL: &str = "overlay";

// The hwnd that had focus when capture began — almost always the game. Raw
// isize because HWND itself isn't Send.
#[cfg(windows)]
static PREV_FOREGROUND: std::sync::Mutex<Option<isize>> = std::sync::Mutex::new(None);

#[cfg(windows)]
fn remember_foreground(app: &AppHandle) {
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;
    let overlay_hwnd = app
        .get_webview_window(OVERLAY_LABEL)
        .and_then(|w| w.hwnd().ok())
        .map(|h| h.0 as isize);
    let fg = unsafe { GetForegroundWindow() };
    if !fg.0.is_null() {
        let raw = fg.0 as isize;
        if Some(raw) != overlay_hwnd {
            *PREV_FOREGROUND.lock().unwrap() = Some(raw);
        }
    }
}

#[cfg(windows)]
fn restore_foreground() {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::WindowsAndMessaging::SetForegroundWindow;
    if let Some(raw) = PREV_FOREGROUND.lock().unwrap().take() {
        unsafe {
            let _ = SetForegroundWindow(HWND(raw as *mut core::ffi::c_void));
        }
    }
}

/// Create the overlay window if it doesn't exist yet. Built hidden, then
/// positioned/sized in PHYSICAL pixels from the monitor (the builder's
/// position/inner_size are logical and would land wrong above 100% scaling —
/// same lesson as browser_show in lib.rs), then shown.
fn ensure_window(app: &AppHandle) -> Result<WebviewWindow, String> {
    if let Some(w) = app.get_webview_window(OVERLAY_LABEL) {
        return Ok(w);
    }
    let monitor = app
        .primary_monitor()
        .map_err(crate::err_str)?
        .ok_or("no monitor found")?;
    let w = WebviewWindowBuilder::new(
        app,
        OVERLAY_LABEL,
        WebviewUrl::App("index.html?overlay=1".into()),
    )
    .title("Aether Overlay")
    .transparent(true)
    .decorations(false)
    .shadow(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .focused(false)
    .visible(false)
    .build()
    .map_err(crate::err_str)?;
    w.set_position(*monitor.position()).map_err(crate::err_str)?;
    w.set_size(*monitor.size()).map_err(crate::err_str)?;
    w.set_ignore_cursor_events(true).map_err(crate::err_str)?;
    w.show().map_err(crate::err_str)?;
    Ok(w)
}

/// Alt+` — make sure the overlay exists and is visible, then tell it to open
/// the Ask pill. On first creation the frontend self-summons on mount (the
/// window only ever comes into being through this hotkey), so a lost event
/// during page load doesn't matter.
pub fn summon_ask(app: &AppHandle) {
    match ensure_window(app) {
        Ok(w) => {
            let _ = w.show();
            let _ = app.emit_to(OVERLAY_LABEL, "overlay://summon-ask", ());
        }
        Err(e) => eprintln!("[overlay] create failed: {e}"),
    }
}

/// Alt+\ — the kill switch. Hiding always releases capture first so the game
/// never stays input-starved behind an invisible window.
pub fn toggle(app: &AppHandle) {
    let Some(w) = app.get_webview_window(OVERLAY_LABEL) else {
        return; // never opened this session — nothing to kill
    };
    if w.is_visible().unwrap_or(false) {
        let _ = w.set_ignore_cursor_events(true);
        #[cfg(windows)]
        restore_foreground();
        let _ = w.hide();
    } else {
        let _ = w.show();
    }
}

/// Frontend-driven side of the capture contract. capture=true when a summoned
/// surface opens (pill expanded), false the moment it closes.
#[tauri::command]
pub async fn overlay_set_capture(app: AppHandle, capture: bool) -> Result<(), String> {
    let w = app
        .get_webview_window(OVERLAY_LABEL)
        .ok_or("overlay not open")?;
    if capture {
        #[cfg(windows)]
        remember_foreground(&app);
        w.set_ignore_cursor_events(false).map_err(crate::err_str)?;
        w.show().map_err(crate::err_str)?;
        w.set_focus().map_err(crate::err_str)?;
    } else {
        w.set_ignore_cursor_events(true).map_err(crate::err_str)?;
        #[cfg(windows)]
        restore_foreground();
    }
    Ok(())
}
