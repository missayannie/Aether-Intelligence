// Concept 2 — the guide checklist, docked on the overlay under the game's
// quest list (docs/overlay-spec.md §6.4).
//
// Shows the doc you pinned to the overlay: current step highlighted, done
// steps struck through, ticking writes straight back to the doc so the app's
// editor and this widget never disagree. Ambient by default — it only takes
// clicks while a summoned surface holds the mouse, exactly like the chips.
import { useCallback, useEffect, useState } from "react";
import { api, type ChecklistStep, type OverlayChecklist } from "../api";

export default function Checklist({
  interactive, dragRef, dragStyle, onDragStart,
}: {
  interactive: boolean;                 // capture is on — clicks reach us
  dragRef: React.RefObject<HTMLDivElement | null>;
  dragStyle?: React.CSSProperties;
  onDragStart: (e: React.PointerEvent) => void;
}) {
  const [list, setList] = useState<OverlayChecklist | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setList(await api.checklistGet());
    } catch { /* backend briefly away — keep what we have */ }
  }, []);

  // Poll: the doc can change in the app (you edit it, or the agent rewrites
  // the plan) while this widget is on screen.
  useEffect(() => {
    void load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [load]);

  if (!list?.pinned || !list.steps.length) return null;

  const steps = list.steps;
  const doneCount = steps.filter((s) => s.done).length;
  const current = steps.find((s) => !s.done);

  const toggle = async (s: ChecklistStep) => {
    if (!interactive) return;
    setBusy(s.index);
    // Optimistic: the tick should feel instant mid-fight.
    setList((l) => l && {
      ...l,
      steps: l.steps.map((x) => (x.index === s.index ? { ...x, done: !x.done } : x)),
    });
    try {
      const r = await api.checklistToggle(s.index);
      setList((l) => l && { ...l, steps: r.steps });
    } catch {
      void load();   // put the truth back
    }
    setBusy(null);
  };

  return (
    <div className={"ov-check" + (interactive ? " arrange" : "")}
         ref={dragRef} style={dragStyle}>
      <div className="ov-check-head" onPointerDown={onDragStart}
           title={interactive ? "Drag to move" : undefined}>
        <span className="ov-check-title">{list.title}</span>
        <span className="ov-check-count">{doneCount}/{steps.length}</span>
        {interactive && (
          <button className="ov-check-fold"
                  onPointerDown={(e) => { e.stopPropagation(); e.preventDefault(); }}
                  onClick={() => setCollapsed((c) => !c)}
                  title={collapsed ? "Show steps" : "Collapse"}>
            {collapsed ? "▸" : "▾"}
          </button>
        )}
      </div>
      {!collapsed && (
        <div className="ov-check-list">
          {steps.map((s) => (
            <button
              key={s.index}
              className={"ov-check-step"
                + (s.done ? " done" : "")
                + (current && s.index === current.index ? " current" : "")
                + (busy === s.index ? " busy" : "")}
              onClick={() => void toggle(s)}
              disabled={!interactive}
            >
              <span className="ov-check-box">{s.done ? "✓" : ""}</span>
              <span className="ov-check-text">{s.text}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
