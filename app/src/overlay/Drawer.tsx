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

export default function Drawer({ onClose, onClickAway, dragRef, dragStyle, onDragStart }: {
  onClose: () => void;
  // Click-away is separate from close: with "keep overlay open" on it hands
  // the mouse back to the game but leaves the drawer standing.
  onClickAway: () => void;
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

  /** Open a record in the MAIN app. Called for the open record itself and for
   * every cross-link in the detail, so anything named here is one click from
   * its full page. */
  const openInApp = async (kind?: string, id?: string) => {
    if (!IN_TAURI) return;
    const [k, i] = kind && id
      ? [kind, id]
      : detail?.kind === "record"
        ? [detail.doc.kind, detail.doc.id]
        : detail?.kind === "item"
          ? ["item", detail.hit.id]
          : [null, null];
    if (!k || !i) return;
    const { invoke } = await import("@tauri-apps/api/core");
    try {
      await invoke("overlay_open_db", { kind: k, id: i });
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
      <div className="ov-drawer-backdrop" onPointerDown={onClickAway} />
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
                <div className="ov-drawer-desc">{plainText(detail.doc.description)}</div>
              )}
              {!!detail.doc.fields?.length && (
                <div className="ov-drawer-fields">
                  {detail.doc.fields.filter((f) => f.value).map((f) => (
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
              {/* Cross-references — rewards, the quest giver, what unlocks it.
                  Each opens that record in the main app. */}
              {detail.doc.links?.filter((g) => g.refs?.length).map((g) => (
                <div className="ov-drawer-group" key={g.group}>
                  <div className="ov-drawer-group-h">{g.group}</div>
                  <div className="ov-drawer-refs">
                    {g.refs.map((r) => (
                      <button key={r.kind + r.id} className="ov-drawer-ref"
                              title={`Open ${r.name} in the app`}
                              onClick={() => void openInApp(r.kind, r.id)}>
                        {r.icon && <img src={r.icon} alt="" loading="lazy" />}
                        <span>{r.name}</span>
                        {r.sub && <em>{r.sub}</em>}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
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
              {(detail.item.category || detail.item.patch) && (
                <div className="ov-drawer-sub">
                  {[detail.item.category, detail.item.patch && `Patch ${detail.item.patch}`]
                    .filter(Boolean).join(" · ")}
                </div>
              )}
              {detail.item.description && (
                <div className="ov-drawer-desc">{plainText(detail.item.description)}</div>
              )}

              {/* Stats, and the numbers you'd otherwise open the app for. */}
              {!!detail.item.attributes && Object.keys(detail.item.attributes).length > 0 && (
                <div className="ov-drawer-fields">
                  {Object.entries(detail.item.attributes).slice(0, 8).map(([k, v]) => (
                    <span key={k}><b>{k}</b> {v}</span>
                  ))}
                </div>
              )}
              <div className="ov-drawer-fields">
                {detail.item.market && (
                  <span>
                    <b>Market</b> {detail.item.market.lowest.toLocaleString()} gil
                    {detail.item.market.world ? ` · ${detail.item.market.world}` : ""}
                    {detail.item.market.listings
                      ? ` (${detail.item.market.listings} listings)` : ""}
                  </span>
                )}
                {!!detail.item.sell_price && (
                  <span><b>Vendor</b> sells for {detail.item.sell_price.toLocaleString()} gil</span>
                )}
                {!!detail.item.materia_slots && (
                  <span><b>Materia</b> {detail.item.materia_slots} slots</span>
                )}
                {!!detail.item.ventures?.length && (
                  <span><b>Ventures</b> {detail.item.ventures.length} retainer venture(s)</span>
                )}
              </div>

              {/* How you get one — each node opens the zone map in the app. */}
              {!!detail.item.nodes?.length && (
                <div className="ov-drawer-group">
                  <div className="ov-drawer-group-h">Gathering</div>
                  <div className="ov-drawer-refs">
                    {detail.item.nodes.map((nd) => (
                      <button key={nd.id} className="ov-drawer-ref"
                              title={`Open ${nd.name} in the app`}
                              onClick={() => void openInApp("node", nd.id)}>
                        <span>{nd.name}</span>
                        <em>{[nd.zone, nd.level ? `Lv ${nd.level}` : "", nd.type]
                          .filter(Boolean).join(" · ")}</em>
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {!!detail.item.vendors?.length && (
                <div className="ov-drawer-group">
                  <div className="ov-drawer-group-h">Sold by</div>
                  <div className="ov-drawer-refs">
                    {detail.item.vendors.map((v) => (
                      <button key={v.id} className="ov-drawer-ref"
                              title={`Open ${v.name} in the app`}
                              onClick={() => void openInApp("npc", v.id)}>
                        <span>{v.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {!!detail.item.upgrades?.length && (
                <div className="ov-drawer-group">
                  <div className="ov-drawer-group-h">Upgrades to</div>
                  <div className="ov-drawer-refs">
                    {detail.item.upgrades.map((u) => (
                      <button key={u.id} className="ov-drawer-ref"
                              onClick={() => void openInApp("item", u.id)}>
                        <span>{u.name}</span>
                        {!!u.item_level && <em>i{u.item_level}</em>}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {!!detail.item.ingredient_of?.length && (
                <div className="ov-drawer-group">
                  <div className="ov-drawer-group-h">Used in</div>
                  <div className="ov-drawer-refs">
                    {detail.item.ingredient_of.map((i) => (
                      <button key={i.id} className="ov-drawer-ref"
                              onClick={() => void openInApp("item", i.id)}>
                        <span>{i.name}</span>
                        {!!i.qty && <em>×{i.qty}</em>}
                      </button>
                    ))}
                  </div>
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
            {detail.kind === "item" && !!detail.item.nodes?.length && (
              <button className="ov-card-btn"
                      onClick={() => void openInApp("node", detail.item.nodes![0].id)}>
                Open node
              </button>
            )}
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
