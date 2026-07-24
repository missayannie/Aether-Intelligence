// GarlandDB browse/search/detail — the data behind the Database tab.
//
// Every route already ships in the desktop backend (v2.0.0) behind the companion
// token gate, so this file is a thin typed client plus a cache. Nothing here
// needs a backend change.
import { apiGet } from "./client";

// gdb.BROWSE_KINDS / BROWSE_LABEL, mirrored. Order matches the desktop's tab.
export const BROWSE_KINDS = [
  "item", "quest", "instance", "fate", "leve", "npc", "mob",
  "node", "fishing", "achievement", "action", "status", "patch",
] as const;
export type Kind = (typeof BROWSE_KINDS)[number];

export const KIND_LABEL: Record<Kind, string> = {
  item: "Items", patch: "Patches", action: "Actions", status: "Status Effects",
  achievement: "Achievements", instance: "Instances", quest: "Quests",
  fate: "FATEs", leve: "Leves", node: "Gathering Nodes",
  fishing: "Fishing Spots", npc: "NPCs", mob: "Mobs",
};

// garland.LINKABLE — the kinds Garland's search can actually return. The other
// four are browse-only, and the UI says so rather than returning nothing.
const SEARCHABLE = new Set<string>([
  "item", "instance", "npc", "quest", "achievement", "mob", "fate", "node", "leve",
]);
export const isSearchable = (k: Kind): boolean => SEARCHABLE.has(k);

export type BrowseRow = { id: string | number; name: string; sub?: string; icon?: string };
export type BrowseGroup = { label: string; count: number; rows: BrowseRow[] };
export type Browse = { kind: string; label?: string; groups: BrowseGroup[]; partial?: boolean };

export type SearchHit = {
  name: string; url: string; id: string | number; type: string;
  item_level?: number; icon?: string;
};

/** Any record. Shapes differ per kind, so the view reads defensively. */
export type Record_ = Record<string, unknown>;

// ---------------------------------------------------------------- cache
//
// In-memory for the session only. Persisting was tempting for Items (~40k rows)
// but measured out badly: the payload is 5.4MB, and @capacitor/preferences is
// UserDefaults on iOS — which is read into memory at app launch, so storing it
// would inflate launch memory for the life of the install. Meanwhile the
// backend memoises its own build, so a refetch is ~0.4s on the LAN. Session
// memory gets nearly all the benefit at none of the cost.
const memo = new Map<string, Browse>();

/** Drop the cached Items index (the Refresh control on that list). */
export async function clearItemCache(): Promise<void> {
  memo.delete("item");
}

// ---------------------------------------------------------------- calls

/** One kind's records, grouped server-side. Cached; `force` refetches. */
export async function browse(kind: Kind, force = false): Promise<Browse> {
  if (!force) {
    const hit = memo.get(kind);
    if (hit) return hit;
  }
  const out = await apiGet<Browse>(`/db/browse?kind=${encodeURIComponent(kind)}`);
  // A truncated build (the backend flags `partial`) is usable but must not be
  // cached, or a bad network moment sticks around for the session.
  if (!out.partial) memo.set(kind, out);
  return out;
}

/** Search. `kind` omitted searches every type in one request. */
export async function search(
  q: string,
  kind?: Kind,
  limit = 30,
  signal?: AbortSignal,
): Promise<SearchHit[]> {
  const k = kind && isSearchable(kind) ? kind : "all";
  const out = await apiGet<{ hits?: SearchHit[] }>(
    `/db/search?q=${encodeURIComponent(q)}&kind=${k}&limit=${limit}`,
    signal,
  );
  return out.hits ?? [];
}

/** One record. Items have their own richer route. */
export async function record(kind: string, id: string | number): Promise<Record_> {
  const path = kind === "item"
    ? `/db/item?id=${encodeURIComponent(String(id))}`
    : `/db/detail?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(String(id))}`;
  return apiGet<Record_>(path);
}
