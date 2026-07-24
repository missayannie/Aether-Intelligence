import { useEffect, useState } from "react";
import DbIcon from "../components/DbIcon";
import { record, type Record_ } from "../lib/db";

// One record. Two shapes come back:
//
//  /db/detail — already uniform and render-ready: {name, sub, description,
//               fields:[{label,value}], links:[{group, refs:[{kind,id,name}]}]}
//  /db/item   — richer and item-specific: attributes, upgrades, market, nodes…
//
// Both are normalised here into the same "fields + link groups" render, so the
// view stays one component. Refs are tappable and push another record.

type Field = { label: string; value: string };
type Ref = { kind: string; id: string | number; name: string; icon?: string; sub?: string };
// `defaultKind` covers the item route's ref lists, which carry {id, name} with
// no kind of their own. Most are items; vendors are NPCs, and routing those to
// /db/item would look up whatever item shares the number.
type LinkGroup = { group: string; refs: Ref[]; defaultKind?: string };

const str = (v: unknown): string => (v == null || v === "" ? "" : String(v));

/** Item records carry their own field names; flatten them into the shared shape. */
function itemFields(r: Record_): Field[] {
  const out: Field[] = [];
  const push = (label: string, v: unknown) => { const s = str(v); if (s) out.push({ label, value: s }); };
  push("Item level", r.item_level);
  push("Equippable by", Array.isArray(r.category) ? (r.category as string[]).join(", ") : r.category);
  push("Patch", r.patch);
  // Zero materia slots is the default for most items — a row saying "0" is noise.
  push("Materia slots", Number(r.materia_slots) > 0 ? r.materia_slots : "");
  push("Sells for", r.sell_price ? `${r.sell_price} gil` : "");

  // Stats arrive as data ({"Dexterity": 146, …}) rather than prose.
  const attrs = r.attributes;
  if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
    for (const [k, v] of Object.entries(attrs as Record<string, unknown>)) push(k, v);
  }

  // Universalis, fetched live and uncached by the backend. The keys are
  // {world_or_dc, lowest, average, world, listings} — not a price_per_unit.
  const market = r.market as Record<string, unknown> | null | undefined;
  if (market && typeof market === "object") {
    const low = str(market.lowest);
    if (low) push("Cheapest listing", `${low} gil${market.world ? ` on ${market.world}` : ""}`);
    const listings = str(market.listings);
    if (listings) push("Listings", `${listings}${market.world_or_dc ? ` on ${market.world_or_dc}` : ""}`);
  } else if (r.tradeable === false) {
    push("Market", "Untradeable");
  }
  return out;
}

type Node = {
  id?: string | number; name?: string; zone?: string; level?: number;
  type?: string; x?: number; y?: number; uptime_minutes?: number; folklore?: string;
};

/** Where an item is gathered. The phone has no map, so this reads as text —
 *  zone plus the in-game coordinates you'd type into a marker. */
function nodeLines(r: Record_): { name: string; detail: string }[] {
  const nodes = Array.isArray(r.nodes) ? (r.nodes as Node[]) : [];
  return nodes.map((n) => {
    const where = [n.zone, n.x != null && n.y != null ? `(${n.x}, ${n.y})` : ""].filter(Boolean).join(" ");
    const what = [n.type, n.level ? `Lv ${n.level}` : "", n.uptime_minutes ? `${n.uptime_minutes} min` : ""]
      .filter(Boolean).join(" · ");
    return { name: str(n.name) || str(n.zone) || "Node", detail: [where, what].filter(Boolean).join(" — ") };
  });
}

function itemLinks(r: Record_): LinkGroup[] {
  const groups: LinkGroup[] = [];
  const add = (group: string, refs: unknown, defaultKind = "item") => {
    if (Array.isArray(refs) && refs.length) groups.push({ group, refs: refs as Ref[], defaultKind });
  };
  add("Upgrades to", r.upgrades);
  add("Downgrades from", r.downgrades);
  add("Ingredient of", r.ingredient_of);
  add("Sold by", r.vendors, "npc");
  return groups;
}

export default function DbRecord({
  kind,
  id,
  name,
  onBack,
  onOpenRecord,
}: {
  kind: string;
  id: string | number;
  name: string;
  onBack: () => void;
  onOpenRecord: (kind: string, id: string | number, name: string) => void;
}) {
  const [rec, setRec] = useState<Record_ | null>(null);
  const [err, setErr] = useState("");

  function load() {
    setErr("");
    setRec(null);
    record(kind, id)
      .then(setRec)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }

  useEffect(load, [kind, id]);

  const isItem = kind === "item";
  const fields: Field[] = rec
    ? isItem
      ? itemFields(rec)
      : ((rec.fields as Field[]) ?? []).filter((f) => f && f.value)
    : [];
  const links: LinkGroup[] = rec ? (isItem ? itemLinks(rec) : ((rec.links as LinkGroup[]) ?? [])) : [];

  const heading = str(rec?.name) || name;
  const sub = str(rec?.sub);
  const description = str(rec?.description);
  const notFound = rec !== null && rec.found === false;
  const nodes = rec && isItem ? nodeLines(rec) : [];

  // Non-item records carry a single {zone, x, y} instead of a node list. No map
  // on the phone, so it renders as coordinates you can act on in game.
  const loc = rec && !isItem ? (rec.location as Node | null) : null;
  const locText = loc
    ? [loc.zone, loc.x != null && loc.y != null ? `(${loc.x}, ${loc.y})` : ""].filter(Boolean).join(" ")
    : "";

  return (
    <div className="db">
      <header className="chat-head">
        <button className="icon-btn" onClick={onBack} aria-label="Back">‹</button>
        <div className="chat-title">{heading}</div>
      </header>

      <div className="db-body">
        {err && (
          <div className="db-err">
            {err}
            <button className="btn ghost retry" onClick={load}>Try again</button>
          </div>
        )}

        {!rec && !err && <p className="empty">Loading…</p>}

        {notFound && <p className="empty">Nothing found for this record.</p>}

        {rec && !notFound && (
          <>
            {/* Instances ship a splash banner (`image`) like Garland's header. */}
            {str(rec.image) && <DbIcon url={str(rec.image)} className="rec-banner" />}

            <div className="rec-head">
              <DbIcon url={str(rec.icon)} className="rec-icon" />
              <div>
                <h2 className="rec-name">{heading}</h2>
                {sub && <p className="rec-sub">{sub}</p>}
              </div>
            </div>

            {description && <p className="rec-desc">{description}</p>}

            {(fields.length > 0 || locText) && (
              <dl className="rec-fields">
                {locText && (
                  <div className="rec-field">
                    <dt>Location</dt>
                    <dd>{locText}</dd>
                  </div>
                )}
                {fields.map((f, i) => (
                  <div className="rec-field" key={f.label + i}>
                    <dt>{f.label}</dt>
                    <dd>{f.value}</dd>
                  </div>
                ))}
              </dl>
            )}

            {nodes.length > 0 && (
              <section className="rec-links">
                <h3>Gathered from</h3>
                <ul className="rows">
                  {nodes.map((n, i) => (
                    <li key={n.name + i}>
                      <div className="row static stack">
                        <span className="row-name">{n.name}</span>
                        {n.detail && <span className="row-sub">{n.detail}</span>}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {links.map((g, i) => (
              <section className="rec-links" key={g.group + i}>
                <h3>{g.group}</h3>
                <ul className="rows">
                  {g.refs.map((ref) => (
                    <li key={`${ref.kind}-${ref.id}`}>
                      <button
                        className="row"
                        onClick={() => onOpenRecord(ref.kind || g.defaultKind || "item", ref.id, ref.name)}
                      >
                        <DbIcon url={ref.icon} className="row-icon" />
                        <span className="row-name">{ref.name}</span>
                        {ref.sub && <span className="row-sub">{ref.sub}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ))}

            {str(rec.url) && (
              <a className="rec-ext" href={str(rec.url)} target="_blank" rel="noreferrer">
                Open on Garland Tools ↗
              </a>
            )}
          </>
        )}
      </div>
    </div>
  );
}
