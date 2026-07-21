// Phase-0 overlay stub (docs/overlay-spec.md). Proves the plumbing over the
// real game: a pill in the top-left dead zone, a fake timer chip on the right
// rail, and a capture round-trip — Alt+` expands the pill into an input
// (window takes keyboard), Esc/blur collapses it (game gets focus back).
// Enter echoes the text as a decaying card; no agent call yet — that's phase 1.
//
// While the pill is open (capture on), widgets are draggable: the pill+card
// group by its ✦ mark, the chip anywhere. Positions persist as fractions of
// the free space (left / (viewport − widget)), so a widget parked at an edge
// stays at that edge on any monitor resolution.
import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  armChips, ask as askAgent, fetchHistory, fetchWatches, newChat, plainText,
  removeWatch, type Card, type CardPlace,
} from "./agent";
import { api, type OverlayTimer, type OverlayWatch } from "../api";
import Checklist from "./Checklist";
import Drawer from "./Drawer";
import "./overlay.css";

/** "3:42" under an hour, "1h 12m" above — countdown text for a chip. */
function fmtDelta(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  if (m >= 60) return `${Math.floor(m / 60)}h ${m % 60}m`;
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
const LAYOUT_KEY = "ov-layout-v1";

async function setCapture(capture: boolean) {
  if (!IN_TAURI) return; // plain-web dev preview: render only
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("overlay_set_capture", { capture });
  } catch (e) {
    console.error("overlay_set_capture failed", e);
  }
}

// Widget scale, set from the main app's Settings ("Overlay size"). Applied as
// native webview zoom (like the app's text size) so everything scales and the
// drag/placement math keeps working in CSS pixels. localStorage is the source
// of truth; the overlay://scale broadcast is the live-update signal (storage
// events aren't guaranteed to fire across Tauri windows).
const SCALE_KEY = "overlayScale";

function readScale(): number {
  const v = parseFloat(localStorage.getItem(SCALE_KEY) ?? "1");
  return isNaN(v) ? 1 : clamp(v, 0.7, 1.6);
}

async function applyScale() {
  const s = readScale();
  if (IN_TAURI) {
    try {
      const { getCurrentWebviewWindow } = await import("@tauri-apps/api/webviewWindow");
      await getCurrentWebviewWindow().setZoom(s);
      relayout();
      return;
    } catch (e) {
      // Native zoom needs a Tauri capability and can be refused — the CSS
      // fallback below then does the scaling, which is why every widget's
      // geometry has to be zoom-aware (see cssZoom).
      console.error("overlay setZoom failed, using CSS zoom", e);
    }
  }
  (document.documentElement.style as unknown as Record<string, string>).zoom = String(s);
  relayout();
}

/** Scaling changes the usable area, so every placed widget must recompute.
 * Fired after the zoom actually lands — it's applied asynchronously, and the
 * first paint happens before it, which is what pushed edge-parked widgets
 * off-screen. */
function relayout() {
  window.dispatchEvent(new Event("ov-relayout"));
}

type Frac = { fx: number; fy: number };

function loadLayout(): Record<string, Frac> {
  try {
    return JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function saveFrac(id: string, f: Frac) {
  const all = loadLayout();
  all[id] = f;
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(all));
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** The CSS zoom actually applied to the document (1 when the native webview
 * zoom did the work instead). Widget geometry — offsetLeft/Top, offsetWidth —
 * is expressed in ZOOMED css pixels, while window.innerWidth stays in device
 * pixels; mixing the two pushed widgets off-screen by exactly the zoom factor
 * (a pill at fx≈1 rendered at x=4088 on a 3440-wide screen, invisible, while
 * the overlay still held the mouse). Everything below divides by this. */
function cssZoom(): number {
  const z = parseFloat(
    (document.documentElement.style as unknown as Record<string, string>).zoom || "1");
  return !z || isNaN(z) ? 1 : z;
}
/** Usable area in the same units as offsetLeft/offsetWidth. */
function viewport(): { w: number; h: number } {
  const z = cssZoom();
  return { w: window.innerWidth / z, h: window.innerHeight / z };
}

/** Drag-to-move with resolution-independent persistence. Placement is
 * recomputed from the stored fraction on window resize, so the same saved
 * layout lands proportionally on any monitor. Deliberately NOT recomputed on
 * widget size change (pill expanding, card appearing) — that would make the
 * widget creep; it just grows in place. */
function useDraggable(id: string, def: Frac, enabled: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const [frac, setFrac] = useState<Frac>(() => loadLayout()[id] ?? def);
  const [px, setPx] = useState<{ x: number; y: number } | null>(null);
  // While the pointer owns this widget, NOTHING else may position it. The
  // re-place hooks below (resize, zoom-landed, the post-mount timers) were
  // firing mid-drag and snapping it back to its stored fraction, which read
  // as the widget jumping around under the cursor.
  const dragging = useRef(false);

  useEffect(() => {
    const place = () => {
      const el = ref.current;
      if (!el || dragging.current) return;
      const vp = viewport();
      const maxX = Math.max(0, vp.w - el.offsetWidth);
      const maxY = Math.max(0, vp.h - el.offsetHeight);
      setPx({ x: Math.round(clamp(frac.fx * maxX, 0, maxX)),
              y: Math.round(clamp(frac.fy * maxY, 0, maxY)) });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("ov-relayout", place);
    window.addEventListener("storage", place);
    // Belt-and-braces: the zoom can land a frame or two after mount, and a
    // widget must never be left parked outside the screen.
    const t1 = window.setTimeout(place, 120);
    const t2 = window.setTimeout(place, 600);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("ov-relayout", place);
      window.removeEventListener("storage", place);
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [frac]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (!enabled) return;
    const el = ref.current;
    if (!el) return;
    // preventDefault keeps focus where it is — dragging must not blur the
    // input (blur collapses the pill mid-drag).
    e.preventDefault();
    e.stopPropagation();
    dragging.current = true;
    // Deltas come from screenX/Y, which the webview's zoom ("Overlay size")
    // never touches, divided by the zoom WE set — clientX math drifted
    // down-right under zoom because rendered pixels and reported coordinates
    // disagreed. offsetLeft/Top are layout truth for the start position.
    // Screen deltas are DEVICE px; element geometry is zoomed-css px.
    const zoom = cssZoom();
    const startSX = e.screenX;
    const startSY = e.screenY;
    const start = { x: el.offsetLeft, y: el.offsetTop };
    const vp = viewport();
    const maxX = Math.max(0, vp.w - el.offsetWidth);
    const maxY = Math.max(0, vp.h - el.offsetHeight);
    let last = start;
    const move = (ev: PointerEvent) => {
      last = {
        x: Math.round(clamp(start.x + (ev.screenX - startSX) / zoom, 0, maxX)),
        y: Math.round(clamp(start.y + (ev.screenY - startSY) / zoom, 0, maxY)),
      };
      setPx(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      dragging.current = false;
      const f = {
        fx: maxX > 0 ? clamp(last.x / maxX, 0, 1) : 0,
        fy: maxY > 0 ? clamp(last.y / maxY, 0, 1) : 0,
      };
      setFrac(f);
      saveFrac(id, f);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const style = px
    ? ({ left: px.x, top: px.y, right: "auto", bottom: "auto" } as React.CSSProperties)
    : undefined;
  return { ref, style, onPointerDown };
}

const SHOT_KEY = "ov-screenshot";
// "Keep overlay surfaces open": stops the answer card decaying and stops a
// click away from closing the pill/drawer. Set from the main app's Settings.
const KEEP_KEY = "ov-keep-open";
const readKeepOpen = () => localStorage.getItem(KEEP_KEY) === "1";

// The ask group's size, dragged from its bottom-right grip and remembered.
const SIZE_KEY = "ov-ask-size";
const DEFAULT_SIZE = { w: 340, h: 190 };
// Never shrink past half the default — below that the pill can't show its
// controls and the history is unreadable.
const MIN_SIZE = { w: DEFAULT_SIZE.w / 2, h: DEFAULT_SIZE.h / 2 };
function readSize(): { w: number; h: number } {
  try {
    const v = JSON.parse(localStorage.getItem(SIZE_KEY) || "null");
    if (v && typeof v.w === "number" && typeof v.h === "number") return v;
  } catch { /* fall through */ }
  return { ...DEFAULT_SIZE };
}

/** A crash anywhere in the overlay tree must NEVER brick the screen: the
 * window may hold cursor capture, and a dead React root would eat every
 * click with nothing visible. Release capture, show a way out. */
class OverlayBoundary extends React.Component<
  { children: React.ReactNode }, { err: boolean }
> {
  state = { err: false };
  static getDerivedStateFromError() {
    return { err: true };
  }
  componentDidCatch(e: unknown) {
    console.error("overlay crashed", e);
    void setCapture(false);
  }
  render() {
    if (!this.state.err) return this.props.children;
    return (
      <div className="ov-card ov-card-err" style={{ position: "absolute", top: 96, left: 14 }}>
        The overlay hit an error. Your mouse is back in the game — press the
        kill switch (Alt+\) to hide this, then resummon.
      </div>
    );
  }
}

export default function OverlayRoot() {
  return (
    <OverlayBoundary>
      <Overlay />
    </OverlayBoundary>
  );
}

function Overlay() {
  const [expanded, setExpanded] = useState(false);
  const [card, setCard] = useState<Card | null>(null);
  const [hovered, setHovered] = useState(false);
  const [armed, setArmed] = useState(false); // this card's pins are armed
  const [watches, setWatches] = useState<OverlayWatch[]>([]);
  // 📷 — include a screenshot of the game with each ask (screen awareness).
  // Default OFF: sending the screen is opt-in, per ask session.
  const [shot, setShot] = useState(() => localStorage.getItem(SHOT_KEY) === "1");
  const [keepOpen, setKeepOpen] = useState(readKeepOpen);
  const [size, setSize] = useState(readSize);
  const inputRef = useRef<HTMLInputElement>(null);
  // Which surface the focus helpers should target. Both can be open at once
  // with "keep overlay open", so the most recently summoned one wins.
  const lastSummoned = useRef<"pill" | "drawer">("pill");

  /** Drag the bottom-right grip to size the chat surface. Deltas come from
   * screen coords over the zoom, same as the move drag, so the corner tracks
   * the cursor at any overlay size. */
  const onResize = (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const zoom = cssZoom();
    const sx = e.screenX;
    const sy = e.screenY;
    const start = readSize();
    const move = (ev: PointerEvent) => {
      setSize({
        w: Math.round(clamp(start.w + (ev.screenX - sx) / zoom, MIN_SIZE.w, 900)),
        h: Math.round(clamp(start.h + (ev.screenY - sy) / zoom, MIN_SIZE.h, 700)),
      });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setSize((s) => {
        localStorage.setItem(SIZE_KEY, JSON.stringify(s));
        return s;
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  // Settings (main window) toggles this; mirror it live.
  useEffect(() => {
    const sync = () => setKeepOpen(readKeepOpen());
    window.addEventListener("storage", sync);
    let un: (() => void) | undefined;
    if (IN_TAURI) {
      import("@tauri-apps/api/event")
        .then(({ listen }) => listen("overlay://keep-open", sync))
        .then((u) => { un = u; });
    }
    return () => { window.removeEventListener("storage", sync); if (un) un(); };
  }, []);

  /** Clicking away with "keep open" on should hand the mouse back to the game
   * WITHOUT tearing the surface down — that's the whole point of the setting.
   * With it off, this is the normal close. */
  const softDismiss = useCallback((close: () => void) => {
    if (readKeepOpen()) {
      void setCapture(false);
      return;
    }
    close();
  }, []);

  // Default ≈ the top-left dead zone above the chat log.
  const ask = useDraggable("ask", { fx: 0.005, fy: 0.07 }, expanded);
  // The chips rail: one draggable group on the right rail under the minimap.
  const chips = useDraggable("chips", { fx: 0.995, fy: 0.22 }, expanded);

  // Refresh on mount, on every summon, AND on a slow poll — chips armed from
  // the app (node pages, map pins) must appear on the overlay by themselves,
  // without the player having to re-summon anything.
  useEffect(() => {
    void fetchWatches().then(setWatches);
    const t = window.setInterval(() => void fetchWatches().then(setWatches), 30000);
    return () => window.clearInterval(t);
  }, [expanded]);

  // Timed watches: poll the backend's window math every 20s, tick the
  // countdown locally every second in between. Both idle when nothing is timed.
  const [timers, setTimers] = useState<Record<string, OverlayTimer>>({});
  const [, setTick] = useState(0);
  const anyTimed = watches.some((w) => (w.kind === "node") || (w as { windows?: unknown[] }).windows?.length);
  useEffect(() => {
    if (!anyTimed) return;
    const poll = async () => {
      try {
        const r = await api.overlayTimers();
        setTimers(Object.fromEntries(r.timers.filter((t) => t.timed).map((t) => [t.id, t])));
      } catch { /* backend briefly away — keep last values */ }
    };
    void poll();
    const p = window.setInterval(poll, 20000);
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => { window.clearInterval(p); window.clearInterval(t); };
  }, [anyTimed]);

  const chipTime = (w: OverlayWatch): string => {
    const t = timers[w.id];
    if (!t?.timed || !t.opens_at || !t.closes_at) return "";
    const now = Date.now() / 1000;
    return t.active || now >= t.opens_at
      ? ` · up now ${fmtDelta(t.closes_at - now)}`
      : ` · opens ${fmtDelta(t.opens_at - now)}`;
  };

  // The two SUMMONED surfaces (pill, drawer) are mutually exclusive; both own
  // the keyboard while open, and closing either hands input back to the game.
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Bumped on EVERY summon, even one that doesn't change open state. In
  // stay-open mode the surface is already up, so setExpanded(true) is a no-op
  // and the focus effect below would never re-fire — this gives it something
  // that always changes, so "press the hotkey, start typing" works whether the
  // surface was closed or already sitting there.
  const [summonTick, setSummonTick] = useState(0);
  const expand = useCallback(() => {
    // With "keep overlay open" the surfaces coexist — summoning the pill must
    // not tear down the database drawer (and its search box) you were using.
    if (!readKeepOpen()) setDrawerOpen(false);
    lastSummoned.current = "pill";
    setExpanded(true);
    setSummonTick((n) => n + 1);
    void setCapture(true);
  }, []);
  const collapse = useCallback(() => {
    setExpanded(false);
    void setCapture(false);
  }, []);
  const openDrawer = useCallback(() => {
    if (!readKeepOpen()) setExpanded(false);
    lastSummoned.current = "drawer";
    setDrawerOpen(true);
    setSummonTick((n) => n + 1);
    void setCapture(true);
  }, []);
  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    void setCapture(false);
  }, []);
  // Full teardown, fired by the kill switch (Alt+\) when it hides the window.
  // Without this the surfaces' open state survives the hide, so with "keep
  // overlay open" on, the next single summon reappears WITH whatever was open
  // before — you press Alt+` and both the pill and the drawer come back. A
  // hide is meant to be an exit, so the next summon should start from nothing.
  const reset = useCallback(() => {
    setExpanded(false);
    setDrawerOpen(false);
  }, []);
  // The drawer: draggable by its ✦ mark while open; lands centered-ish.
  const drawerDrag = useDraggable("drawer", { fx: 0.5, fy: 0.18 }, drawerOpen);
  // The guide checklist: right rail, under where the quest list sits.
  const checkDrag = useDraggable("checklist", { fx: 0.985, fy: 0.42 },
                                 expanded || drawerOpen);

  useEffect(() => {
    // Auto-open the surface whose hotkey CREATED this window (?summon=1 /
    // ?drawer=1). Alt+Win+` creates it ambient — widgets only, no capture.
    const boot = new URLSearchParams(window.location.search);
    if (boot.has("summon")) expand();
    else if (boot.has("drawer")) openDrawer();
    // Later summons arrive over TWO channels (see overlay.rs): the Tauri
    // event, and a direct eval calling these globals — so a missed listener
    // can never strand a hotkey.
    const w = window as unknown as Record<string, unknown>;
    w.__aetherOverlaySummon = expand;
    w.__aetherOverlayDrawer = openDrawer;
    w.__aetherOverlayReset = reset;
    const unlistens: (() => void)[] = [];
    if (IN_TAURI) {
      import("@tauri-apps/api/event").then(({ listen }) => {
        void listen("overlay://summon-ask", () => expand()).then((u) => unlistens.push(u));
        void listen("overlay://summon-drawer", () => openDrawer()).then((u) => unlistens.push(u));
        void listen("overlay://reset", () => reset()).then((u) => unlistens.push(u));
      });
    }
    return () => {
      delete w.__aetherOverlaySummon;
      delete w.__aetherOverlayDrawer;
      delete w.__aetherOverlayReset;
      unlistens.forEach((u) => u());
    };
  }, [expand, openDrawer, reset]);

  // Whichever summoned surface is open owns the keyboard — its input is the
  // focus target for all the fallbacks below.
  const activeInput = useCallback((): HTMLInputElement | null => {
    const drawerQ = () => document.querySelector<HTMLInputElement>(".ov-drawer-q");
    if (drawerOpen && expanded) {
      return lastSummoned.current === "drawer" ? drawerQ() : inputRef.current;
    }
    if (drawerOpen) return drawerQ();
    if (expanded) return inputRef.current;
    return null;
  }, [drawerOpen, expanded]);

  // Plain-JS focus on every summon (not just when `expanded` flips) — this is
  // what focuses the input in web dev, and inside Tauri it's a cheap first
  // attempt before the shell-click fallback below does the guaranteed thing.
  useEffect(() => {
    if (!expanded && !drawerOpen) return;
    activeInput()?.focus();
  }, [expanded, drawerOpen, activeInput, summonTick]);

  // Losing focus must NEVER close a summoned surface. Over a game the overlay
  // routinely can't hold OS focus — an earlier build closed the pill on
  // sustained blur and the hotkey looked broken (pill opened, vanished 1.5s
  // later, leaving only the ambient layer). So blur releases the MOUSE only:
  // the overlay stops eating clicks while you're playing, the pill stays put,
  // and regaining focus (or pressing the hotkey again) re-captures.
  useEffect(() => {
    if (!expanded && !drawerOpen) return;
    let t: number | undefined;
    const onBlur = () => {
      t = window.setTimeout(() => void setCapture(false), 1500);
    };
    const onFocus = () => {
      if (t) window.clearTimeout(t);
      void setCapture(true);
    };
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
      if (t) window.clearTimeout(t);
    };
  }, [expanded, drawerOpen]);

  // ESCAPE HATCH: Esc closes the summoned surface, at the WINDOW CAPTURE phase.
  // The pill needed two presses to close because the webview's native handling
  // of Escape blurs the focused input, swallowing the first keydown before the
  // input's own onKey (collapse) could run; only the second press, with focus
  // no longer in the input, reached a closer. Handling it here — capture phase,
  // before any native input handling — closes the pill on the FIRST press,
  // wherever focus sits.
  useEffect(() => {
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // When the drawer is the active surface, let ITS handler run (it walks
      // detail → list → close) while its search box has focus; only close from
      // here when focus is elsewhere.
      const drawerActive = drawerOpen && (!expanded || lastSummoned.current === "drawer");
      if (drawerActive) {
        if (document.activeElement?.tagName === "INPUT") return;
        e.preventDefault();
        e.stopPropagation();
        closeDrawer();
        return;
      }
      if (expanded) {
        e.preventDefault();
        e.stopPropagation();
        collapse();
      }
    };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [expanded, drawerOpen, collapse, closeDrawer]);

  // Belt-and-suspenders for focus: while a surface is open, ANY keystroke
  // that lands on the window but not in its input (focus theft, a click that
  // strayed) refocuses it — so "summon, then just type" always works.
  useEffect(() => {
    if (!expanded && !drawerOpen) return;
    const onWinKey = () => {
      const inp = activeInput();
      if (inp && document.activeElement !== inp) inp.focus();
    };
    window.addEventListener("keydown", onWinKey, true);
    return () => window.removeEventListener("keydown", onWinKey, true);
  }, [expanded, drawerOpen, activeInput]);

  // Typing must work the instant a hotkey opens a surface. Asking politely
  // (set_focus, AttachThreadInput, synthetic ALT) is unreliable over a
  // fullscreen game, so we ALWAYS do the thing Windows never refuses: have
  // the shell click our own input. This used to run only as a late fallback
  // when focus "looked" wrong, and document.hasFocus() lies often enough that
  // the player was left clicking the box by hand. The overlay holds cursor
  // capture and the cursor is put back, so the game never sees the click.
  useEffect(() => {
    if ((!expanded && !drawerOpen) || !IN_TAURI) return;
    let cancelled = false;

    const clickInput = async () => {
      const inp = activeInput();
      if (!inp || cancelled) return;
      try {
        const [{ invoke }, { getCurrentWebviewWindow }] = await Promise.all([
          import("@tauri-apps/api/core"),
          import("@tauri-apps/api/webviewWindow"),
        ]);
        const pos = await getCurrentWebviewWindow().outerPosition(); // physical px
        const r = inp.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        await invoke("overlay_click_at", {
          x: pos.x + (r.left + r.width / 2) * dpr,
          y: pos.y + (r.top + r.height / 2) * dpr,
        });
        inp.focus();
      } catch (e) {
        console.error("focus click failed", e);
      }
    };

    // The FIRST summon of a session is the hard one: it CREATES the window, so
    // the click/focus attempts race window show, cursor-capture taking effect,
    // and the zoom/layout settling — the click lands on nothing and the player
    // has to click the box by hand. Every later summon reuses the warm window
    // and lands focus on the first try. Two things fix the cold case:
    //   1. await setCapture(true) so ignore_cursor_events is definitely OFF
    //      before we click — otherwise the synthetic click falls through to
    //      the game instead of our input.
    //   2. retry until the input actually holds focus (capped ~1.4s), instead
    //      of two fixed-time shots that both fire before the window is ready.
    // It stops the instant focus lands, so a warm summon still takes one pass.
    // "Done" means the OS WINDOW holds keyboard focus AND our input is the
    // active element. document.activeElement alone is a trap: DOM focus is
    // sticky, so after the game quietly takes OS focus back (pill left open a
    // while), activeElement is still the input — the old check thought it was
    // focused and never clicked to reclaim it. document.hasFocus() is what
    // actually tracks the OS window, so we keep clicking until BOTH hold.
    const focused = () => {
      const inp = activeInput();
      return !!inp && document.hasFocus() && document.activeElement === inp;
    };
    let timer: number | undefined;
    void (async () => {
      await setCapture(true);
      for (let i = 0; i < 12 && !cancelled; i++) {
        if (focused()) return;
        await clickInput();
        if (cancelled || focused()) return;
        await new Promise((res) => { timer = window.setTimeout(res, 120); });
      }
    })();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [expanded, drawerOpen, activeInput, summonTick]);

  // Previous turns under the pill — refreshed on each summon, and again when
  // a streaming answer finishes so the newest exchange shows up.
  const [history, setHistory] = useState<{ role: string; content: string }[]>([]);
  // The turn in flight, shown at the end of the conversation until the stored
  // history catches up (which is what stops the answer appearing twice).
  const [liveQ, setLiveQ] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);
  const liveTurn = [
    ...(liveQ ? [{ role: "user", content: liveQ }] : []),
    ...(card?.text ? [{ role: "assistant", content: card.text }] : []),
  ];
  useEffect(() => {
    if (!expanded) return;
    void fetchHistory().then((h) => {
      setHistory(h);
      // Hand off: the backend now owns this exchange, so drop the live copy.
      if (card?.done) {
        setLiveQ("");
        setCard((c) => (c ? { ...c, text: "" } : c));
      }
    });
  }, [expanded, card?.done]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    historyRef.current?.scrollTo(0, historyRef.current.scrollHeight);
  }, [history, card?.text]);

  // Widget scale: apply on mount, then re-apply when Settings changes it —
  // via the Tauri broadcast (packaged app) or the storage event (web dev).
  useEffect(() => {
    void applyScale();
    const onStorage = (e: StorageEvent) => {
      if (e.key === SCALE_KEY) void applyScale();
    };
    window.addEventListener("storage", onStorage);
    let unlisten: (() => void) | undefined;
    if (IN_TAURI) {
      import("@tauri-apps/api/event")
        .then(({ listen }) => listen("overlay://scale", () => void applyScale()))
        .then((u) => {
          unlisten = u;
        });
    }
    return () => {
      window.removeEventListener("storage", onStorage);
      if (unlisten) unlisten();
    };
  }, []);

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") collapse(); // the card stays and decays on its timer
    if (e.key === "Enter") {
      const q = e.currentTarget.value.trim();
      if (!q) return;
      e.currentTarget.value = "";
      setHovered(false);
      setArmed(false);
      setLiveQ(q);
      // The pill STAYS open (follow-ups keep the rolling chat's context); the
      // answer streams into the card below. Esc/click-away still collapses —
      // the stream keeps going and the card keeps updating, just ambient.
      void (async () => {
        let screenshot = "";
        if (shot && IN_TAURI) {
          setCard({ status: "Reading your screen…", text: "", sources: [], done: false });
          try {
            const { invoke } = await import("@tauri-apps/api/core");
            screenshot = (await invoke("overlay_capture_screen")) as string;
          } catch (err) {
            console.error("screen capture failed", err); // ask proceeds blind
          }
        }
        await askAgent(q, setCard, screenshot);
      })();
    }
  };

  const toggleShot = () => {
    setShot((s) => {
      localStorage.setItem(SHOT_KEY, s ? "0" : "1");
      return !s;
    });
  };

  const onArmChips = async (place: CardPlace) => {
    try {
      await armChips(place);
      setArmed(true);
      setWatches(await fetchWatches());
    } catch (e) {
      console.error("arm chips failed", e);
    }
  };

  const onChipClick = async (w: OverlayWatch) => {
    if (!IN_TAURI || !w.place) return;
    const { invoke } = await import("@tauri-apps/api/core");
    try {
      await invoke("overlay_open_map", { payload: w.place });
    } catch (e) {
      console.error("overlay_open_map failed", e);
    }
  };

  const onChipRemove = async (w: OverlayWatch) => {
    setWatches((ws) => ws.filter((x) => x.id !== w.id));
    await removeWatch(w.id);
  };

  // A finished card decays after 20s — unless the pointer is on it (only
  // possible while capture is on; in ambient state the timer just runs), or
  // the player asked for surfaces to stay put.
  useEffect(() => {
    if (!card?.done || hovered || keepOpen) return;
    const t = window.setTimeout(() => setCard(null), 20000);
    return () => window.clearTimeout(t);
  }, [card, hovered, keepOpen]);

  // Card action: raise the main app on the pinned zone. preventDefault on
  // pointerdown keeps the input focused so the blur→collapse doesn't release
  // capture before the click lands.
  const openMap = async () => {
    if (!IN_TAURI || !card?.place) return;
    const { invoke } = await import("@tauri-apps/api/core");
    try {
      await invoke("overlay_open_map", { payload: card.place });
    } catch (e) {
      console.error("overlay_open_map failed", e);
    }
  };

  return (
    <div className={"ov-root" + (expanded || drawerOpen ? " active" : "")}>
      {/* Click-away for the pill (same contract as the drawer's backdrop):
          clicking anywhere that isn't overlay UI collapses and returns the
          mouse to the game. */}
      {expanded && (
        <div className="ov-drawer-backdrop"
             onPointerDown={() => softDismiss(collapse)} />
      )}
      <div className="ov-ask" ref={ask.ref} style={ask.style}>
        {/* The pill tracks the chat window's width so the resize grip lines up
            with BOTH — it used to keep its own content width, leaving the grip
            hanging off the wider of the two. Collapsed it stays a small dot. */}
        <div className={"ov-pill" + (expanded ? " open" : "")}
             style={expanded ? { width: size.w } : undefined}>
          <span
            className="ov-mark"
            onPointerDown={ask.onPointerDown}
            title={expanded ? "Drag to move" : undefined}
          >
            ✦
          </span>
          {expanded && (
            <>
              {/* NO onBlur-collapse: over a game, focus flaps while the shell
                  fights for the keyboard, and a transient blur used to close
                  the pill the instant it opened ("the hotkey just shows the
                  chip layer"). Click-away is the backdrop's job now, exactly
                  like the drawer — which never had this bug. */}
              <input
                ref={inputRef}
                className="ov-input"
                placeholder="Ask Eorzea…  (Esc to close)"
                onKeyDown={onKey}
              />
              <button
                className="ov-newchat"
                onPointerDown={(e) => e.preventDefault()}
                onClick={() => {
                  newChat();
                  setCard(null);
                  setHistory([]);
                  inputRef.current?.focus();
                }}
                title="Start a new overlay chat (drops this thread's context)"
              >
                ✚
              </button>
              <label
                className={"ov-cam" + (shot ? " on" : "")}
                onPointerDown={(e) => e.preventDefault()}
                title={shot ? "Sending your screen with each ask (uncheck to stop)"
                            : "Screen NOT included — check to let the agent see your game"}
              >
                <span className="ov-cam-ico">📷</span>
                <span className="ov-cam-box">{shot ? "✓" : ""}</span>
                <input
                  type="checkbox"
                  checked={shot}
                  onChange={toggleShot}
                  style={{ display: "none" }}
                />
              </label>
            </>
          )}
        </div>

        {/* ONE conversation surface. The answer streams into this box as the
            newest message — it used to also appear in a separate card, which
            showed the same text twice and then vanished. `live` is the turn
            in flight; once the backend has stored it, the refreshed history
            carries it and the live copy is dropped. */}
        {expanded && size.h > 0
          && (liveTurn.length > 0 || history.length > 0 || keepOpen) && (
          <div className="ov-history" ref={historyRef}
               style={{ width: size.w, maxHeight: size.h, minHeight: keepOpen ? 60 : undefined }}
               onMouseEnter={() => setHovered(true)}
               onMouseLeave={() => setHovered(false)}>
            {[...history, ...liveTurn].map((m, i) => (
              <div key={i} className={"ov-hist-msg " + m.role}>
                {plainText(m.content)}
              </div>
            ))}
            {!history.length && !liveTurn.length && (
              <div className="ov-hist-empty">Ask something — answers land here.</div>
            )}
            {card?.status && (
              <div className="ov-card-status">
                <span className="ov-spin" />
                {card.status}
              </div>
            )}
            {card?.error && <div className="ov-card-err">{card.error}</div>}
          </div>
        )}

        {/* Actions for the latest answer sit under the conversation, not in a
            card of their own. */}
        {expanded && card?.done && !card.error && (card.place || card.sources.length > 0) && (
          <div className="ov-answer-foot" style={{ width: size.w }}>
            {(
              <div className="ov-card-foot">
                {card.place && (
                  <button
                    className="ov-card-btn"
                    onPointerDown={(e) => e.preventDefault()}
                    onClick={() => void openMap()}
                  >
                    Open map
                  </button>
                )}
                {card.place && (
                  <button
                    className="ov-card-btn"
                    disabled={armed}
                    onPointerDown={(e) => e.preventDefault()}
                    onClick={() => card.place && void onArmChips(card.place)}
                  >
                    {armed
                      ? "Armed ✓"
                      : card.place.pins?.length
                        ? `Arm chips (${card.place.pins.length})`
                        : "Arm chip"}
                  </button>
                )}
                {card.sources.length > 0 && (
                  <span className="ov-card-src">{card.sources.join(" · ")}</span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Bottom-right grip: drag to size the chat surface (card + history).
            Only while summoned — in ambient state nothing can reach it. */}
        {expanded && (
          <div
            className="ov-resize"
            onPointerDown={onResize}
            title="Drag to resize"
          />
        )}
      </div>

      {drawerOpen && (
        <Drawer
          onClose={closeDrawer}
          onClickAway={() => softDismiss(closeDrawer)}
          dragRef={drawerDrag.ref}
          dragStyle={drawerDrag.style}
          onDragStart={drawerDrag.onPointerDown}
        />
      )}

      <Checklist
        interactive={expanded || drawerOpen}
        dragRef={checkDrag.ref}
        dragStyle={checkDrag.style}
        onDragStart={checkDrag.onPointerDown}
      />

      {/* The chips rail: armed watches, ambient + click-through. While the
          pill is open (capture on) each chip is clickable (open in app) and
          removable; the rail drags as one group. */}
      {watches.length > 0 && (
        <div className="ov-chips" ref={chips.ref} style={chips.style}>
          {watches.map((w) => (
            <div
              key={w.id}
              className={"ov-chip" + (expanded ? " arrange" : "")}
              onPointerDown={chips.onPointerDown}
              title={expanded ? "Click to open in the app · drag to move" : undefined}
            >
              <span className="ov-chip-label" onClick={() => void onChipClick(w)}>
                {w.label}
                {chipTime(w)}
              </span>
              {expanded && (
                <button
                  className="ov-chip-x"
                  onPointerDown={(e) => { e.stopPropagation(); e.preventDefault(); }}
                  onClick={() => void onChipRemove(w)}
                  title="Remove"
                >
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
