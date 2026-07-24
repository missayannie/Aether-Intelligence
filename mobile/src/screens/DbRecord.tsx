import { useEffect, useState } from "react";
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
type LinkGroup = { group: string; refs: Ref[] };

const str = (v: unknown): string => (v == null || v === "" ? "" : String(v));

/** Item records carry their own field names; flatten them into the shared shape. */
function itemFields(r: Record_): Field[] {
  const out: Field[] = [];
  const push = (label: string, v: unknown) => { const s = str(v); if (s) out.push({ label, value: s }); };
  push("Item level", r.item_level);
  push("Equippable by", Array.isArray(r.category) ? (r.category as string[]).join(", ") : r.category);
  push("Patch", r.patch);
  push("Materia slots", r.materia_slots);
  push("Sells for", r.sell_price ? `${r.sell_price} gil` : "");

  // Stats arrive as data ({"Dexterity": 146, …}) rather than prose.
  const attrs = r.attributes;
  if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
    for (const [k, v] of Object.entries(attrs as Record<string, unknown>)) push(k, v);
  }

  const market = r.market as Record<string, unknown> | null | undefined;
  if (market && typeof market === "object") {
    const price = str(market.price_per_unit ?? market.price);
    if (price) push("Cheapest listing", `${price} gil${market.world ? ` (${market.world})` : ""}`);
  } else if (r.tradeable === false) {
    push("Market", "Untradeable");
  }
  return out;
}

function itemLinks(r: Record_): LinkGroup[] {
  const groups: LinkGroup[] = [];
  const add = (group: string, refs: unknown) => {
    if (Array.isArray(refs) && refs.length) groups.push({ group, refs: refs as Ref[] });
  };
  add("Upgrades to", r.upgrades);
  add("Downgrades from", r.downgrades);
  add("Ingredient of", r.ingredient_of);
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
            <div className="rec-head">
              {str(rec.icon) && <img className="rec-icon" src={str(rec.icon)} alt="" />}
              <div>
                <h2 className="rec-name">{heading}</h2>
                {sub && <p className="rec-sub">{sub}</p>}
              </div>
            </div>

            {description && <p className="rec-desc">{description}</p>}

            {fields.length > 0 && (
              <dl className="rec-fields">
                {fields.map((f, i) => (
                  <div className="rec-field" key={f.label + i}>
                    <dt>{f.label}</dt>
                    <dd>{f.value}</dd>
                  </div>
                ))}
              </dl>
            )}

            {links.map((g, i) => (
              <section className="rec-links" key={g.group + i}>
                <h3>{g.group}</h3>
                <ul className="rows">
                  {g.refs.map((ref) => (
                    <li key={`${ref.kind}-${ref.id}`}>
                      <button
                        className="row"
                        onClick={() => onOpenRecord(ref.kind || "item", ref.id, ref.name)}
                      >
                        {ref.icon && <img className="row-icon" src={ref.icon} alt="" />}
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
