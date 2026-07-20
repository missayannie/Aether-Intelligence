// Phase 2 — the summoned database drawer (docs/overlay-spec.md concept 4).
// Alt+D over the game: type to search the whole database, ↑↓ walks results,
// Enter opens the compact detail, Esc backs out one level then dismisses.
// While open the overlay owns the keyboard (the parent set capture); closing
// hands input straight back to the game.
import { useEffect, useRef, useState } from "react";
import { api, type DbDetailDoc, type DbHit, type DbItem } from "../api";
import { plainText } from "./agent";

const IN_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

type Detail =
  | { kind: "record"; doc: DbDetailDoc }
  | { kind: "item"; item: DbItem; hit: DbHit };

export default function Drawer({ onClose, dragRef, dragStyle, onDragStart }: {
  onClose: () => void;
  dragRef: React.RefObject<HTMLDivElement | null>;
  dragStyle?: React.CSSProperties;
  onDragStart: (e: React.PointerEvent) => void;
}) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<DbHit[]>([]);
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [flagged, setFlagged] = useState(false);
  const [armed, setArmed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, [detail]);

  // Search-as-you-type, debounced. Play-time questions want speed over depth.
  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      setSel(0);
      return;
    }
    const t = window.setTimeout(async () => {
      setBusy(true);
      try {
        const r = await api.dbSearch(q.trim(), "all");
        setHits(r.hits.slice(0, 12));
        setSel(0);
      } catch { /* backend briefly away */ }
      setBusy(false);
    }, 250);
    return () => window.clearTimeout(t);
  }, [q]);

  // Keep the selected row scrolled into view as ↑↓ walks the list.
  useEffect(() => {
    listRef.current?.children[sel]?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const openHit = async (h: DbHit) => {
    setFlagged(false);
    setArmed(false);
    try {
      if (h.type === "item") {
        setDetail({ kind: "item", item: await api.dbItem(h.url), hit: h });
      } else {
        setDetail({ kind: "record", doc: await api.dbDetail(h.type, h.id) });
      }
    } catch { /* keep the list */ }
  };

  const flagOnMap = async () => {
    if (!IN_TAURI || detail?.kind !== "record" || !detail.doc.location) return;
    const loc = detail.doc.location;
    const { invoke } = await import("@tauri-apps/api/core");
    try {
      await invoke("overlay_open_map", {
        payload: {
          zone: loc.zone,
          pin: loc.x
            ? { x: loc.x, y: loc.y, label: loc.label || detail.doc.name || "",
                icon: loc.icon, space: "game" }
            : null,
        },
      });
      setFlagged(true);
    } catch (e) {
      console.error("flag on map failed", e);
    }
  };

  // Arm anything armable as an overlay chip, right from the drawer:
  // a gathering node (timed — the backend derives the spawn windows), or any
  // record's location as a plain pin chip.
  const armNode = async (ref: string) => {
    try {
      await api.overlayWatchAdd({ kind: "node", ref, label: "" });
      setArmed(true);
    } catch (e) {
      console.error("arm node failed", e);
    }
  };
  const armSpot = async (doc: DbDetailDoc) => {
    const loc = doc.location;
    if (!loc) return;
    try {
      await api.overlayWatchAdd({
        kind: "pin",
        label: doc.name || loc.label || "Spot",
        zone: loc.zone,
        x: loc.x,
        y: loc.y,
        place: {
          zone: loc.zone,
          pin: { x: loc.x, y: loc.y, label: doc.name || loc.label || "",
                 icon: loc.icon, space: "game" },
        },
      });
      setArmed(true);
    } catch (e) {
      console.error("arm spot failed", e);
    }
  };

  const openInApp = async () => {
    if (!IN_TAURI || !detail) return;
    const { invoke } = await import("@tauri-apps/api/core");
    const [kind, id] = detail.kind === "record"
      ? [detail.doc.kind, detail.doc.id]
      : ["item", detail.hit.id];
    try {
      await invoke("overlay_open_db", { kind, id });
    } catch (e) {
      console.error("open in app failed", e);
    }
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      if (detail) setDetail(null);
      else onClose();
      return;
    }
    if (detail) return; // detail view: only Esc navigates
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(hits.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter" && hits[sel]) {
      e.preventDefault();
      void openHit(hits[sel]);
    }
  };

  return (
    <>
      {/* Click-away: the whole screen behind the drawer. Without it the
          summoned window silently ate every click ("the screen froze") —
          now clicking anywhere outside closes the drawer and hands the
          mouse straight back to the game. */}
      <div className="ov-drawer-backdrop" onPointerDown={onClose} />
      <div className="ov-drawer" ref={dragRef} style={dragStyle} onKeyDown={onKey}>
      <div className="ov-drawer-head">
        <span
          className="ov-mark ov-drawer-grab"
          onPointerDown={onDragStart}
          title="Drag to move"
        >
          ✦
        </span>
        <input
          ref={inputRef}
          className="ov-drawer-q"
          placeholder="Search the database…  (Esc to close)"
          value={q}
          onChange={(e) => { setQ(e.target.value); setDetail(null); }}
        />
        {busy && <span className="ov-spin" />}
      </div>

      {detail ? (
        <div className="ov-drawer-detail">
          {detail.kind === "record" ? (
            <>
              <div className="ov-drawer-title">
                {detail.doc.icon && <img className="ov-drawer-ico" src={detail.doc.icon} alt="" />}
                <span>{detail.doc.name}</span>
                {detail.doc.sub && <span className="ov-drawer-sub">{detail.doc.sub}</span>}
              </div>
              {detail.doc.description && (
                <div className="ov-drawer-desc">{plainText(detail.doc.description).slice(0, 280)}</div>
              )}
              {!!detail.doc.fields?.length && (
                <div className="ov-drawer-fields">
                  {detail.doc.fields.slice(0, 6).map((f) => (
                    <span key={f.label}><b>{f.label}</b> {f.value}</span>
                  ))}
                </div>
              )}
              {detail.doc.location && (
                <div className="ov-drawer-loc">
                  📍 {detail.doc.location.zone}
                  {detail.doc.location.x
                    ? ` (${detail.doc.location.x.toFixed(1)}, ${detail.doc.location.y.toFixed(1)})`
                    : ""}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="ov-drawer-title">
                {detail.item.icon && <img className="ov-drawer-ico" src={detail.item.icon} alt="" />}
                <span>{detail.item.name}</span>
                {detail.item.item_level && (
                  <span className="ov-drawer-sub">i{detail.item.item_level}</span>
                )}
              </div>
              {detail.item.description && (
                <div className="ov-drawer-desc">{plainText(detail.item.description).slice(0, 280)}</div>
              )}
              {!!detail.item.nodes?.length && (
                <div className="ov-drawer-fields">
                  <span><b>Gathered</b> {detail.item.nodes.slice(0, 3)
                    .map((n) => `${n.name}${n.zone ? ` (${n.zone})` : ""}`).join(" · ")}</span>
                </div>
              )}
              {!!detail.item.vendors?.length && (
                <div className="ov-drawer-fields">
                  <span><b>Sold by</b> {detail.item.vendors.slice(0, 3)
                    .map((v) => v.name).join(" · ")}</span>
                </div>
              )}
            </>
          )}
          <div className="ov-card-foot">
            {detail.kind === "record" && detail.doc.location && (
              <button className="ov-card-btn" onClick={() => void flagOnMap()}>
                {flagged ? "Flagged ✓" : "Flag on map"}
              </button>
            )}
            {detail.kind === "record" && detail.doc.kind === "node" ? (
              <button className="ov-card-btn" disabled={armed}
                      onClick={() => detail.kind === "record" && void armNode(detail.doc.id)}>
                {armed ? "Armed ✓" : "⏱ Watch"}
              </button>
            ) : detail.kind === "record" && detail.doc.location ? (
              <button className="ov-card-btn" disabled={armed}
                      onClick={() => detail.kind === "record" && void armSpot(detail.doc)}>
                {armed ? "Armed ✓" : "⏱ Watch"}
              </button>
            ) : detail.kind === "item" && detail.item.nodes?.length ? (
              <button className="ov-card-btn" disabled={armed}
                      onClick={() => detail.kind === "item"
                        && void armNode(detail.item.nodes![0].id)}>
                {armed ? "Armed ✓" : "⏱ Watch node"}
              </button>
            ) : null}
            <button className="ov-card-btn" onClick={() => void openInApp()}>
              Open in app
            </button>
            <span className="ov-drawer-hint">Esc back</span>
          </div>
        </div>
      ) : (
        <>
          <div className="ov-drawer-list" ref={listRef}>
            {hits.map((h, i) => (
              <button
                key={h.url + h.id}
                className={"ov-drawer-row" + (i === sel ? " sel" : "")}
                onClick={() => void openHit(h)}
                onMouseEnter={() => setSel(i)}
              >
                {h.icon ? (
                  <img className="ov-drawer-ico" src={h.icon} alt="" loading="lazy" />
                ) : (
                  <span className="ov-drawer-ico ov-drawer-glyph">•</span>
                )}
                <span className="ov-drawer-name">{h.name}</span>
                <span className="ov-drawer-sub">
                  {h.type}{h.item_level ? ` · i${h.item_level}` : ""}
                </span>
              </button>
            ))}
            {!hits.length && q.trim() && !busy && (
              <div className="ov-drawer-empty">No matches.</div>
            )}
          </div>
          <div className="ov-drawer-hint">↑↓ navigate · Enter open · Esc or click away to close</div>
        </>
      )}
      </div>
    </>
  );
}
