// The right-pane "browser" tab. In the desktop app this drives a REAL second
// WebView2 (Chromium) view created by the Rust shell (see lib.rs) — an iframe
// can't do the job because most sites (google.com included) refuse framing.
// The native view floats OVER the window at coordinates we measure from the
// placeholder div below the toolbar, so this component's whole job is:
//   toolbar UI + keep the native view's rectangle in sync + relay navigations.
// In a plain-browser dev session (no Tauri) it degrades to an iframe, which
// many sites will refuse — that path exists only so `npm run dev` still runs.
import { useEffect, useRef, useState } from "react";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { openUrl } from "@tauri-apps/plugin-opener";

export const BROWSER_HOME = "https://www.google.com";

export type BrowserReq = { url: string; seq: number } | null;

// Whether the native child webview exists yet. Module-level: the component
// unmounts every time the tab deactivates, but the native view lives on for
// the whole app session (hidden), so this must survive remounts.
const embed = { created: false };

/** Bare words become a Google search; anything host-shaped gets https://. */
function normalize(input: string): string {
  const s = input.trim();
  if (!s) return BROWSER_HOME;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return s;
  if (/^[^\s]+\.[^\s]{2,}/.test(s)) return "https://" + s;
  return "https://www.google.com/search?q=" + encodeURIComponent(s);
}

export default function BrowserPane({ visible, req }: { visible: boolean; req: BrowserReq }) {
  const tauri = isTauri();
  const [cur, setCur] = useState<string>(
    () => req?.url || localStorage.getItem("browserUrl") || BROWSER_HOME,
  );
  const [addr, setAddr] = useState(cur);
  const [frameKey, setFrameKey] = useState(0); // iframe fallback reload knob
  const holder = useRef<HTMLDivElement>(null);
  const addrFocused = useRef(false);
  const curRef = useRef(cur);
  curRef.current = cur;
  const lastSeq = useRef(0);

  // A link click routed here (possibly while the tab was already open).
  useEffect(() => {
    if (!req || req.seq === lastSeq.current) return;
    lastSeq.current = req.seq;
    // curRef synchronously too: when this runs in the same commit as the show
    // effect below (link click mounted the pane), place() must already see the
    // requested URL — the state update alone lands a render too late.
    curRef.current = req.url;
    setCur(req.url);
    if (!addrFocused.current) setAddr(req.url);
    if (tauri) {
      // Always try; if the native view doesn't exist yet this fails silently
      // and the creation path (show effect) navigates instead. The shell
      // ignores `url` on an existing view, so double-navigation can't happen.
      invoke("browser_navigate", { url: req.url }).catch(() => {});
    } else {
      setFrameKey((k) => k + 1);
    }
  }, [req, tauri]);

  // Keep the native view shown + glued to the placeholder's rectangle while
  // visible; hide it when the tab deactivates, unmounts, or a modal opens over
  // it (a native view would otherwise paint on top of the modal). The 400ms
  // interval catches position-only shifts (e.g. sidebar resize) that neither
  // ResizeObserver nor window-resize report.
  useEffect(() => {
    if (!tauri) return;
    if (!visible) {
      invoke("browser_hide").catch(() => {});
      return;
    }
    const el = holder.current;
    if (!el) return;
    const place = () => {
      const r = el.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      const url = embed.created ? undefined : curRef.current;
      embed.created = true;
      // PHYSICAL pixels: the shell places the native view verbatim, so on a
      // scaled display (125%/150%) the CSS rect must be multiplied out here —
      // read per call, so dragging to a differently-scaled monitor stays right.
      const s = window.devicePixelRatio || 1;
      invoke("browser_show", {
        x: r.left * s, y: r.top * s, w: r.width * s, h: r.height * s, url,
      }).catch(() => { embed.created = false; });
    };
    place();
    let t: number | undefined;
    const nudge = () => {
      if (t !== undefined) return;
      t = window.setTimeout(() => { t = undefined; place(); }, 80);
    };
    const ro = new ResizeObserver(nudge);
    ro.observe(el);
    window.addEventListener("resize", nudge);
    const iv = window.setInterval(nudge, 400);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", nudge);
      window.clearInterval(iv);
      if (t !== undefined) window.clearTimeout(t);
      invoke("browser_hide").catch(() => {});
    };
  }, [visible, tauri]);

  // The native view reports every main-frame navigation → URL bar follows.
  useEffect(() => {
    if (!tauri) return;
    const un = listen<string>("embed-nav", (e) => {
      setCur(e.payload);
      if (!addrFocused.current) setAddr(e.payload);
      localStorage.setItem("browserUrl", e.payload);
    });
    return () => { un.then((f) => f()); };
  }, [tauri]);

  function go(raw: string) {
    const url = normalize(raw);
    setCur(url);
    setAddr(url);
    localStorage.setItem("browserUrl", url);
    if (tauri) {
      // If the native view doesn't exist yet this fails silently and the show
      // effect's creation path picks curRef up instead.
      invoke("browser_navigate", { url }).catch(() => {});
    } else {
      setFrameKey((k) => k + 1);
    }
  }

  const hist = (dir: "back" | "forward" | "reload") => {
    if (tauri) invoke("browser_history", { dir }).catch(() => {});
    else setFrameKey((k) => k + 1); // iframe: best we can do is reload
  };

  return (
    <div className="bp">
      <div className="bp-bar">
        <button className="bp-btn" title="Back" onClick={() => hist("back")}>‹</button>
        <button className="bp-btn" title="Forward" onClick={() => hist("forward")}>›</button>
        <button className="bp-btn" title="Reload" onClick={() => hist("reload")}>⟳</button>
        <button className="bp-btn" title="Home (Google)" onClick={() => go(BROWSER_HOME)}>⌂</button>
        <input
          className="bp-addr"
          value={addr}
          spellCheck={false}
          onFocus={(e) => { addrFocused.current = true; e.currentTarget.select(); }}
          onBlur={() => { addrFocused.current = false; setAddr(curRef.current); }}
          onChange={(e) => setAddr(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { go(addr); e.currentTarget.blur(); } }}
          placeholder="Search Google or enter a URL"
        />
        <button
          className="bp-btn"
          title="Open in your default browser"
          onClick={() => { openUrl(cur).catch(() => window.open(cur, "_blank")); }}
        >
          ↗
        </button>
      </div>
      {/* The native view is positioned exactly over this div. */}
      <div className="bp-view" ref={holder}>
        {!tauri && (
          <iframe
            key={frameKey}
            className="bp-frame"
            src={cur}
            title="browser"
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          />
        )}
        {!tauri && (
          <div className="bp-devnote muted small">
            Dev preview: many sites refuse to load in an iframe. The installed
            app uses a real browser view.
          </div>
        )}
      </div>
    </div>
  );
}
