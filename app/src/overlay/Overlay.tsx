// Phase-0 overlay stub (docs/overlay-spec.md). Proves the plumbing over the
// real game: a pill in the top-left dead zone, a fake timer chip on the right
// rail, and a capture round-trip — Alt+` expands the pill into an input
// (window takes keyboard), Esc/blur collapses it (game gets focus back).
// Enter echoes the text as a decaying card; no agent call yet — that's phase 1.
import { useCallback, useEffect, useRef, useState } from "react";
import "./overlay.css";

const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function setCapture(capture: boolean) {
  if (!IN_TAURI) return; // plain-web dev preview: render only
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("overlay_set_capture", { capture });
  } catch (e) {
    console.error("overlay_set_capture failed", e);
  }
}

export default function Overlay() {
  const [expanded, setExpanded] = useState(false);
  const [card, setCard] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cardTimer = useRef<number | undefined>(undefined);

  const expand = useCallback(() => {
    setExpanded(true);
    void setCapture(true);
  }, []);
  const collapse = useCallback(() => {
    setExpanded(false);
    void setCapture(false);
  }, []);

  useEffect(() => {
    // The window only comes into being through Alt+`, so creation IS the
    // first summon; later summons arrive as events from the Rust hotkey.
    expand();
    let unlisten: (() => void) | undefined;
    if (IN_TAURI) {
      import("@tauri-apps/api/event")
        .then(({ listen }) => listen("overlay://summon-ask", () => expand()))
        .then((u) => {
          unlisten = u;
        });
    }
    return () => {
      if (unlisten) unlisten();
    };
  }, [expand]);

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") collapse();
    if (e.key === "Enter") {
      const q = e.currentTarget.value.trim();
      e.currentTarget.value = "";
      collapse();
      if (q) {
        setCard(q);
        window.clearTimeout(cardTimer.current);
        cardTimer.current = window.setTimeout(() => setCard(null), 8000);
      }
    }
  };

  return (
    <div className="ov-root">
      <div className={"ov-pill" + (expanded ? " open" : "")}>
        <span className="ov-mark">✦</span>
        {expanded && (
          <input
            ref={inputRef}
            className="ov-input"
            placeholder="Ask Eorzea…  (Esc to close)"
            onKeyDown={onKey}
            onBlur={collapse}
          />
        )}
      </div>

      {card && (
        <div className="ov-card">
          <div className="ov-card-title">Phase-0 echo</div>
          <div className="ov-card-body">{card}</div>
          <div className="ov-card-hint">auto-dismisses in 8s</div>
        </div>
      )}

      <div className="ov-chip">Cordia Sap · opens 12:34</div>
    </div>
  );
}
