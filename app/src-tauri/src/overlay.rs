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
fn ensure_window(app: &AppHandle, boot: &str) -> Result<WebviewWindow, String> {
    if let Some(w) = app.get_webview_window(OVERLAY_LABEL) {
        return Ok(w);
    }
    let monitor = app
        .primary_monitor()
        .map_err(crate::err_str)?
        .ok_or("no monitor found")?;
    // `boot` ("summon=1" / "drawer=1") makes the page open that surface on
    // mount — used when the window is being created BY that hotkey, since the
    // runtime channels below can't reach a page that hasn't loaded yet.
    let url = if boot.is_empty() {
        "index.html?overlay=1".to_string()
    } else {
        format!("index.html?overlay=1&{boot}")
    };
    let w = WebviewWindowBuilder::new(app, OVERLAY_LABEL, WebviewUrl::App(url.into()))
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
    // Exclude the overlay from ALL screen capture — our own screenshot-to-agent
    // grab (§6.5) must show the game, not our pill; a streamer's capture
    // software skips it for free too.
    #[cfg(windows)]
    if let Ok(h) = w.hwnd() {
        use windows::Win32::Foundation::HWND;
        use windows::Win32::UI::WindowsAndMessaging::{
            SetWindowDisplayAffinity, WDA_EXCLUDEFROMCAPTURE,
        };
        let hwnd = HWND(h.0 as isize as *mut core::ffi::c_void);
        unsafe {
            let _ = SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE);
        }
    }
    w.show().map_err(crate::err_str)?;
    Ok(w)
}

/// Screen awareness (§6.5): one JPEG data-URL of the primary monitor, sized
/// for a vision model (~1600px wide, q70). GDI BitBlt sees borderless-windowed
/// games — the only mode the overlay supports anyway. Runs on a blocking
/// thread; a 3440×1440 grab + resize + encode is ~100-200ms.
#[tauri::command]
pub async fn overlay_capture_screen() -> Result<String, String> {
    #[cfg(windows)]
    {
        tauri::async_runtime::spawn_blocking(capture_screen_jpeg)
            .await
            .map_err(crate::err_str)?
    }
    #[cfg(not(windows))]
    Err("screen capture is Windows-only".to_string())
}

#[cfg(windows)]
fn capture_screen_jpeg() -> Result<String, String> {
    use base64::Engine as _;
    use image::codecs::jpeg::JpegEncoder;
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
        DIB_RGB_COLORS, SRCCOPY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

    let (w, h, mut bgra) = unsafe {
        let w = GetSystemMetrics(SM_CXSCREEN);
        let h = GetSystemMetrics(SM_CYSCREEN);
        if w <= 0 || h <= 0 {
            return Err("no screen metrics".into());
        }
        let screen = GetDC(None);
        let mem = CreateCompatibleDC(Some(screen));
        let bmp = CreateCompatibleBitmap(screen, w, h);
        let old = SelectObject(mem, bmp.into());
        let blit = BitBlt(mem, 0, 0, w, h, Some(screen), 0, 0, SRCCOPY);
        let mut info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h, // top-down rows
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut buf = vec![0u8; (w as usize) * (h as usize) * 4];
        let got = GetDIBits(
            mem,
            bmp,
            0,
            h as u32,
            Some(buf.as_mut_ptr() as *mut core::ffi::c_void),
            &mut info,
            DIB_RGB_COLORS,
        );
        SelectObject(mem, old);
        let _ = DeleteObject(bmp.into());
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
        if blit.is_err() || got == 0 {
            return Err("screen grab failed".into());
        }
        (w as u32, h as u32, buf)
    };

    // BGRA → RGB in place, then hand image an RGB buffer.
    for px in bgra.chunks_exact_mut(4) {
        px.swap(0, 2);
    }
    let rgb: Vec<u8> = bgra
        .chunks_exact(4)
        .flat_map(|p| [p[0], p[1], p[2]])
        .collect();
    let img = image::RgbImage::from_raw(w, h, rgb).ok_or("bad capture buffer")?;
    let img = if w > 1600 {
        let nh = (h as f32 * 1600.0 / w as f32) as u32;
        image::imageops::resize(&img, 1600, nh.max(1), image::imageops::FilterType::Triangle)
    } else {
        img
    };
    let mut jpeg = Vec::new();
    JpegEncoder::new_with_quality(&mut jpeg, 70)
        .encode_image(&img)
        .map_err(crate::err_str)?;
    Ok(format!(
        "data:image/jpeg;base64,{}",
        base64::engine::general_purpose::STANDARD.encode(&jpeg)
    ))
}

/// Alt+` — make sure the overlay exists and is visible, then open the Ask
/// pill. If this hotkey CREATES the window, the ?summon=1 URL opens the pill
/// on mount; for an existing window it signals the page over two independent
/// channels — the event system (normal path) AND a direct eval into the page
/// (works even if the event listener was never attached), because a stranded
/// hotkey is the one failure the user can't see past.
pub fn summon_ask(app: &AppHandle) {
    match ensure_window(app, "summon=1") {
        Ok(w) => {
            let _ = w.show();
            let _ = app.emit("overlay://summon-ask", ());
            let _ = w.eval("window.__aetherOverlaySummon && window.__aetherOverlaySummon()");
            // One delayed retry: covers a window whose page was still loading
            // when the instant channels fired (the "hotkey shows the ambient
            // layer but no pill" failure). Idempotent — expanding twice is a
            // no-op.
            let w2 = w.clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(700));
                let _ = w2.eval("window.__aetherOverlaySummon && window.__aetherOverlaySummon()");
            });
        }
        Err(e) => eprintln!("[overlay] create failed: {e}"),
    }
}

/// Alt+D — summon the database DRAWER (concept 4): search the whole database
/// without leaving the game. Same dual-channel delivery as the pill.
pub fn summon_drawer(app: &AppHandle) {
    match ensure_window(app, "drawer=1") {
        Ok(w) => {
            let _ = w.show();
            let _ = app.emit("overlay://summon-drawer", ());
            let _ = w.eval("window.__aetherOverlayDrawer && window.__aetherOverlayDrawer()");
            let w2 = w.clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(700));
                let _ = w2.eval("window.__aetherOverlayDrawer && window.__aetherOverlayDrawer()");
            });
        }
        Err(e) => eprintln!("[overlay] create failed: {e}"),
    }
}

/// Alt+Win+` — show the overlay LAYER (ambient widgets, click-through) without
/// opening the pill. The quiet way in.
pub fn show_ambient(app: &AppHandle) {
    match ensure_window(app, "") {
        Ok(w) => {
            let _ = w.show();
        }
        Err(e) => eprintln!("[overlay] create failed: {e}"),
    }
}

/// Drawer action "Open in app": raise the main window on this database record.
#[tauri::command]
pub async fn overlay_open_db(app: AppHandle, kind: String, id: String) -> Result<(), String> {
    let main = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let _ = main.unminimize();
    main.show().map_err(crate::err_str)?;
    main.set_focus().map_err(crate::err_str)?;
    app.emit_to("main", "overlay://open-db", serde_json::json!({ "kind": kind, "id": id }))
        .map_err(crate::err_str)?;
    Ok(())
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

/// Card action "Open map": raise the main app and hand it the map payload
/// (same shape as the chat stream's `map` event — zone/focus/pin). The main
/// window owns the map machinery; it listens for this event (App.tsx).
#[tauri::command]
pub async fn overlay_open_map(app: AppHandle, payload: serde_json::Value) -> Result<(), String> {
    let main = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let _ = main.unminimize();
    main.show().map_err(crate::err_str)?;
    main.set_focus().map_err(crate::err_str)?;
    app.emit_to("main", "overlay://open-map", payload)
        .map_err(crate::err_str)?;
    Ok(())
}

/// Windows won't always let a background process seize the foreground — over
/// a fullscreen game, set_focus can silently lose and keystrokes stay in the
/// game while the pill sits open ("I have to click before I can type"). The
/// classic remedy: attach our input queue to the foreground thread for the
/// duration of SetForegroundWindow, so the request comes "from" the thread
/// that owns the foreground.
#[cfg(windows)]
fn force_foreground(w: &WebviewWindow) {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        keybd_event, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP, VK_MENU,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowThreadProcessId, SetForegroundWindow,
    };
    let Ok(h) = w.hwnd() else { return };
    let hwnd = HWND(h.0 as isize as *mut core::ffi::c_void);
    unsafe {
        let fg = GetForegroundWindow();
        if fg.0.is_null() || fg == hwnd {
            let _ = SetForegroundWindow(hwnd);
            return;
        }
        // Two classic remedies at once, because a game holding the foreground
        // lock defeats either alone on some setups:
        // 1. attach our input queue to the foreground thread,
        // 2. a synthetic ALT press — a window processing "user input" is
        //    allowed to take the foreground (ALT is physically held during
        //    the Alt+` hotkey anyway, so this is a no-op for the player).
        let fg_thread = GetWindowThreadProcessId(fg, None);
        let cur = GetCurrentThreadId();
        let attached = AttachThreadInput(cur, fg_thread, true);
        keybd_event(VK_MENU.0 as u8, 0, KEYBD_EVENT_FLAGS(0), 0);
        let _ = SetForegroundWindow(hwnd);
        keybd_event(VK_MENU.0 as u8, 0, KEYEVENTF_KEYUP, 0);
        if attached.as_bool() {
            let _ = AttachThreadInput(cur, fg_thread, false);
        }
    }
}

/// LAST-RESORT focus: synthesize a real mouse click at the given PHYSICAL
/// screen coordinates — the centre of the pill's input — then put the cursor
/// back where it was. "I have to click in the text box" is the one action
/// Windows never refuses focus for, so when the polite grabs (set_focus +
/// AttachThreadInput + synthetic ALT) demonstrably failed, the frontend asks
/// for the click to be made on its behalf. The overlay has cursor capture at
/// that moment, so the click lands on OUR window — the game never sees it
/// (the never-automate-the-game rule holds).
#[tauri::command]
pub async fn overlay_click_at(x: f64, y: f64) -> Result<(), String> {
    #[cfg(windows)]
    unsafe {
        use windows::Win32::Foundation::POINT;
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            SendInput, INPUT, INPUT_0, INPUT_MOUSE, MOUSEEVENTF_ABSOLUTE,
            MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MOVE, MOUSEINPUT,
        };
        use windows::Win32::UI::WindowsAndMessaging::{
            GetCursorPos, GetSystemMetrics, SetCursorPos, SM_CXSCREEN, SM_CYSCREEN,
        };
        let mut orig = POINT::default();
        let _ = GetCursorPos(&mut orig);
        let sw = GetSystemMetrics(SM_CXSCREEN) as f64;
        let sh = GetSystemMetrics(SM_CYSCREEN) as f64;
        if sw <= 0.0 || sh <= 0.0 {
            return Err("no screen metrics".into());
        }
        let nx = (x / sw * 65535.0) as i32;
        let ny = (y / sh * 65535.0) as i32;
        let mk = |flags| INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT {
                    dx: nx, dy: ny, mouseData: 0,
                    dwFlags: flags, time: 0, dwExtraInfo: 0,
                },
            },
        };
        let inputs = [
            mk(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            mk(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE),
            mk(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE),
        ];
        SendInput(&inputs, std::mem::size_of::<INPUT>() as i32);
        let _ = SetCursorPos(orig.x, orig.y);
    }
    #[cfg(not(windows))]
    let _ = (x, y);
    Ok(())
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
        #[cfg(windows)]
        force_foreground(&w);
        // The game can snatch focus back within the first frames — retry
        // twice on a short fuse so "summon, then just type" holds.
        #[cfg(windows)]
        {
            let w2 = w.clone();
            std::thread::spawn(move || {
                for delay in [120u64, 300] {
                    std::thread::sleep(std::time::Duration::from_millis(delay));
                    let _ = w2.set_focus();
                    force_foreground(&w2);
                }
            });
        }
    } else {
        w.set_ignore_cursor_events(true).map_err(crate::err_str)?;
        #[cfg(windows)]
        restore_foreground();
    }
    Ok(())
}
