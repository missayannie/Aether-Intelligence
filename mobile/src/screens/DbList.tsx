import { useEffect, useRef, useState } from "react";
import {
  browse, clearItemCache, isSearchable, KIND_LABEL, search,
  type BrowseGroup, type Kind, type SearchHit,
} from "../lib/db";

// One kind's records. Groups render COLLAPSED (label + count) and expand their
// rows on tap — that's what keeps Items usable: ~40k rows arrive grouped by
// category, and we only ever render the ~100 headers plus one open group.
//
// The search field here scopes to this kind. Four kinds (patch, action, status,
// fishing) aren't in Garland's search index, so they say so rather than
// returning nothing.
export default function DbList({
  kind,
  onBack,
  onOpenRecord,
}: {
  kind: Kind;
  onBack: () => void;
  onOpenRecord: (kind: string, id: string | number, name: string) => void;
}) {
  const [groups, setGroups] = useState<BrowseGroup[] | null>(null);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const abort = useRef<AbortController | null>(null);

  const searchable = isSearchable(kind);

  function load(force = false) {
    setErr("");
    setGroups(null);
    browse(kind, force)
      .then((b) => setGroups(b.groups ?? []))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }

  useEffect(() => {
    load();
    setOpen(new Set());
    setQ("");
    setHits(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind]);

  useEffect(() => {
    const query = q.trim();
    abort.current?.abort();
    if (!query || !searchable) { setHits(null); return; }
    const ctrl = new AbortController();
    abort.current = ctrl;
    const t = setTimeout(() => {
      search(query, kind, 30, ctrl.signal)
        .then(setHits)
        .catch((e) => {
          if (!(e instanceof DOMException && e.name === "AbortError")) {
            setErr(e instanceof Error ? e.message : String(e));
          }
        });
    }, 250);
    return () => { clearTimeout(t); ctrl.abort(); };
  }, [q, kind, searchable]);

  function toggle(i: number) {
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });
  }

  async function refresh() {
    await clearItemCache();
    load(true);
  }

  return (
    <div className="db">
      <header className="chat-head">
        <button className="icon-btn" onClick={onBack} aria-label="Back">‹</button>
        <div className="chat-title">{KIND_LABEL[kind]}</div>
        {kind === "item" && groups && (
          <button className="db-refresh" onClick={() => void refresh()}>Refresh</button>
        )}
      </header>

      <div className="db-search">
        <input
          className="in db-in"
          type="search"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder={searchable ? `Search ${KIND_LABEL[kind].toLowerCase()}…` : "Browse only — not searchable"}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          disabled={!searchable}
        />
      </div>

      <div className="db-body">
        {err && <div className="db-err">{err}</div>}

        {hits !== null ? (
          hits.length === 0 ? (
            <p className="empty">Nothing matched “{q.trim()}”.</p>
          ) : (
            <ul className="rows">
              {hits.map((h) => (
                <li key={`${h.type}-${h.id}`}>
                  <button className="row" onClick={() => onOpenRecord(h.type, h.id, h.name)}>
                    <span className="row-name">{h.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : groups === null ? (
          <p className="empty">
            {kind === "item" ? "Building the item index — this one's big, and only the first time." : "Loading…"}
          </p>
        ) : groups.length === 0 ? (
          <p className="empty">Nothing to browse here.</p>
        ) : (
          <ul className="groups">
            {groups.map((g, i) => (
              <li key={g.label + i}>
                <button className="group" onClick={() => toggle(i)} aria-expanded={open.has(i)}>
                  <span className="group-caret">{open.has(i) ? "▾" : "▸"}</span>
                  <span className="group-name">{g.label}</span>
                  <span className="group-count">{g.count ?? g.rows.length}</span>
                </button>
                {open.has(i) && (
                  <ul className="rows nested">
                    {g.rows.map((r) => (
                      <li key={String(r.id)}>
                        <button className="row" onClick={() => onOpenRecord(kind, r.id, r.name)}>
                          <span className="row-name">{r.name}</span>
                          {r.sub && <span className="row-sub">{r.sub}</span>}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
