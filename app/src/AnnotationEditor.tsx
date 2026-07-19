import { useEffect, useRef, useState } from "react";
import { api } from "./api";

// Interactive annotation editor. Drops draggable markers/zones/arrows/labels on a
// base image (e.g. a pinned map) and exports a flattened PNG via the backend
// annotate engine. Coordinates are relative (0..1) — the same model annotate.py
// uses — so on-screen positions and the exported image line up exactly.

type Kind = "marker" | "circle" | "arrow" | "label";
type Anno = {
  id: string; kind: Kind;
  x: number; y: number; x2?: number; y2?: number;
  radius: number; text: string; color: string;
};

// Must match annotate.py COLORS keys.
const COLORS: Record<string, string> = {
  marker: "#fac775", safe: "#5dcaa5", danger: "#f0997b", note: "#afa9ec", boss: "#f0957a",
};

let _seq = 0;
const nid = () => `a${_seq++}`;

export default function AnnotationEditor(props: {
  chatId: string;
  assetName: string;
  onClose: () => void;
  onExported: (newName: string) => void;
}) {
  const [annos, setAnnos] = useState<Anno[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [size, setSize] = useState({ w: 600, h: 600 });
  const [title, setTitle] = useState("");
  const [exporting, setExporting] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: string; pt: "main" | "end" } | null>(null);

  const measure = () => {
    const el = boxRef.current;
    if (el) setSize({ w: el.clientWidth, h: el.clientHeight });
  };
  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  function add(kind: Kind) {
    const a: Anno = {
      id: nid(), kind, x: 0.5, y: 0.5, radius: 0.06,
      text: kind === "label" ? "Label" : "",
      color: kind === "circle" ? "safe" : kind === "arrow" ? "danger" : "marker",
    };
    if (kind === "arrow") { a.x = 0.42; a.y = 0.5; a.x2 = 0.6; a.y2 = 0.5; }
    setAnnos((s) => [...s, a]);
    setSel(a.id);
  }
  const update = (id: string, patch: Partial<Anno>) =>
    setAnnos((s) => s.map((a) => (a.id === id ? { ...a, ...patch } : a)));
  const del = (id: string) => { setAnnos((s) => s.filter((a) => a.id !== id)); setSel(null); };

  function toRel(e: React.PointerEvent) {
    const r = boxRef.current!.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }
  function onMove(e: React.PointerEvent) {
    if (!drag.current) return;
    const { x, y } = toRel(e);
    drag.current.pt === "end"
      ? update(drag.current.id, { x2: x, y2: y })
      : update(drag.current.id, { x, y });
  }
  const endDrag = () => { drag.current = null; };

  async function exportPng() {
    setExporting(true);
    try {
      const res = await api.annotateAsset(
        props.chatId, props.assetName, title,
        annos.map((a) => ({
          kind: a.kind, x: a.x, y: a.y, x2: a.x2, y2: a.y2,
          radius: a.radius, text: a.text, color: a.color,
        })),
      );
      props.onExported(res.asset_id);
    } catch (e) {
      alert("Export failed: " + String(e));
    } finally {
      setExporting(false);
    }
  }

  const W = size.w, H = size.h;
  const selAnno = annos.find((a) => a.id === sel) || null;
  let mnum = 0;

  const down = (id: string, pt: "main" | "end") => (e: React.PointerEvent) => {
    e.stopPropagation();
    setSel(id);
    drag.current = { id, pt };
  };

  return (
    <div className="modal-bg" onClick={props.onClose}>
      <div
        className="anno-editor"
        onClick={(e) => e.stopPropagation()}
        onPointerMove={onMove}
        onPointerUp={endDrag}
        onPointerLeave={endDrag}
      >
        <aside className="anno-tools">
          <div className="modal-head">
            Annotate
            <button className="x" onClick={props.onClose}>✕</button>
          </div>
          <input
            className="anno-input" placeholder="Title (optional)"
            value={title} onChange={(e) => setTitle(e.target.value)}
          />
          <div className="section-label">Add</div>
          <div className="anno-add">
            <button onClick={() => add("marker")}>① Marker</button>
            <button onClick={() => add("circle")}>◯ Zone</button>
            <button onClick={() => add("arrow")}>➜ Arrow</button>
            <button onClick={() => add("label")}>T Label</button>
          </div>

          {selAnno ? (
            <div className="anno-inspect">
              <div className="section-label">Selected · {selAnno.kind}</div>
              <input
                className="anno-input" placeholder="Label text"
                value={selAnno.text} onChange={(e) => update(selAnno.id, { text: e.target.value })}
              />
              <div className="anno-colors">
                {Object.keys(COLORS).map((c) => (
                  <button
                    key={c} title={c}
                    className={"cdot" + (selAnno.color === c ? " on" : "")}
                    style={{ background: COLORS[c] }}
                    onClick={() => update(selAnno.id, { color: c })}
                  />
                ))}
              </div>
              {selAnno.kind === "circle" && (
                <label className="anno-range small muted">
                  Size
                  <input
                    type="range" min={0.02} max={0.28} step={0.005} value={selAnno.radius}
                    onChange={(e) => update(selAnno.id, { radius: parseFloat(e.target.value) })}
                  />
                </label>
              )}
              <button className="danger anno-del" onClick={() => del(selAnno.id)}>Delete</button>
            </div>
          ) : (
            <div className="muted small anno-hint">Add a marker, then drag it into place. Click one to edit or recolor.</div>
          )}

          <div className="anno-actions">
            <button className="anno-export" onClick={exportPng} disabled={exporting || !annos.length}>
              {exporting ? "Exporting…" : "Export PNG"}
            </button>
          </div>
        </aside>

        <div className="anno-canvas">
         <div className="anno-stage" ref={boxRef}>
          <img
            src={api.assetUrl(props.chatId, props.assetName)}
            onLoad={measure} draggable={false} alt="base"
          />
          <svg className="anno-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
            <defs>
              {Object.entries(COLORS).map(([k, c]) => (
                <marker
                  key={k} id={`ah-${k}`} markerUnits="userSpaceOnUse"
                  markerWidth={16} markerHeight={16} refX={11} refY={7} orient="auto"
                >
                  <path d="M0,0 L14,7 L0,14 Z" fill={c} />
                </marker>
              ))}
            </defs>
            {annos.map((a) => {
              const col = COLORS[a.color] || COLORS.marker;
              const x = a.x * W, y = a.y * H;
              const on = a.id === sel;
              if (a.kind === "circle") {
                const r = a.radius * W;
                return (
                  <g key={a.id}>
                    <circle cx={x} cy={y} r={r} fill="none" stroke={col} strokeWidth={on ? 5 : 4} />
                    {a.text && <text x={x} y={y - r - 8} fill={col} textAnchor="middle" fontSize={22}>{a.text}</text>}
                    <circle className="handle" cx={x} cy={y} r={13} fill={col} onPointerDown={down(a.id, "main")} />
                  </g>
                );
              }
              if (a.kind === "arrow") {
                const x2 = (a.x2 ?? a.x) * W, y2 = (a.y2 ?? a.y) * H;
                return (
                  <g key={a.id}>
                    <line x1={x} y1={y} x2={x2} y2={y2} stroke={col} strokeWidth={on ? 7 : 6} markerEnd={`url(#ah-${a.color})`} />
                    <circle className="handle" cx={x} cy={y} r={12} fill={col} onPointerDown={down(a.id, "main")} />
                    <circle className="handle" cx={x2} cy={y2} r={10} fill="#fff" stroke={col} strokeWidth={2} onPointerDown={down(a.id, "end")} />
                  </g>
                );
              }
              if (a.kind === "label") {
                const w = (a.text || "Label").length * 11 + 16;
                return (
                  <g key={a.id} className="handle" onPointerDown={down(a.id, "main")}>
                    <rect x={x - 6} y={y - 18} width={w} height={30} rx={6} fill="rgba(18,18,18,.82)" stroke={on ? col : "none"} />
                    <text x={x + 4} y={y + 3} fill={col} fontSize={19}>{a.text || "Label"}</text>
                  </g>
                );
              }
              mnum++;
              return (
                <g key={a.id} className="handle" onPointerDown={down(a.id, "main")}>
                  <circle cx={x} cy={y} r={on ? 19 : 17} fill={col} />
                  <text x={x} y={y + 7} textAnchor="middle" fill="#141414" fontSize={20} fontWeight={700}>{mnum}</text>
                  {a.text && <text x={x} y={y + 38} textAnchor="middle" fill={col} fontSize={17}>{a.text}</text>}
                </g>
              );
            })}
          </svg>
         </div>
        </div>
      </div>
    </div>
  );
}
