import { useEffect, useRef, useState } from "react";
import DbIcon from "../components/DbIcon";
import { BROWSE_KINDS, KIND_LABEL, search, type Kind, type SearchHit } from "../lib/db";

// The Database tab's root: a search field over a grid of the 13 browsable kinds.
// Empty query -> the grid. Typing -> all-type search results.
//
// Search is debounced and abortable: a new keystroke cancels the in-flight
// request, so fast typing can't land stale results after fresher ones.
export default function Database({
  onOpenKind,
  onOpenRecord,
}: {
  onOpenKind: (k: Kind) => void;
  onOpenRecord: (kind: string, id: string | number, name: string) => void;
}) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    const query = q.trim();
    abort.current?.abort();
    if (!query) { setHits(null); setBusy(false); setErr(""); return; }

    setBusy(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const t = setTimeout(() => {
      search(query, undefined, 30, ctrl.signal)
        .then((h) => { setHits(h); setErr(""); })
        .catch((e) => {
          if (e instanceof DOMException && e.name === "AbortError") return;
          setErr(e instanceof Error ? e.message : String(e));
        })
        .finally(() => { if (!ctrl.signal.aborted) setBusy(false); });
    }, 250);

    return () => { clearTimeout(t); ctrl.abort(); };
  }, [q]);

  return (
    <div className="db">
      <div className="db-search">
        <input
          className="in db-in"
          type="search"
          inputMode="search"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="Search the database…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div className="db-body">
        {err && <div className="db-err">{err}</div>}

        {hits === null ? (
          <div className="kind-grid">
            {BROWSE_KINDS.map((k) => (
              <button key={k} className="kind" onClick={() => onOpenKind(k)}>
                {KIND_LABEL[k]}
              </button>
            ))}
          </div>
        ) : busy && hits.length === 0 ? (
          <p className="empty">Searching…</p>
        ) : hits.length === 0 ? (
          <p className="empty">Nothing matched “{q.trim()}”.</p>
        ) : (
          <ul className="rows">
            {hits.map((h) => (
              <li key={`${h.type}-${h.id}`}>
                <button className="row" onClick={() => onOpenRecord(h.type, h.id, h.name)}>
                  <DbIcon url={h.icon} className="row-icon" />
                  <span className="row-name">{h.name}</span>
                  <span className="row-tag">{KIND_LABEL[h.type as Kind] ?? h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
