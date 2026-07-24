// GarlandDB browse/search/detail — the data behind the Database tab.
//
// Every route already ships in the desktop backend (v2.0.0) behind the companion
// token gate, so this file is a thin typed client plus a cache. Nothing here
// needs a backend change.
import { Preferences } from "@capacitor/preferences";
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
// Browse responses are pure game data — the backend's own TTL is 7 days and keys
// freshness off the game-data hash rather than a clock, so caching between
// patches is safe. Session memory covers every kind; Items also persists,
// because it's the only genuinely expensive one (~40k rows).

const memo = new Map<string, Browse>();
const ITEM_CACHE_KEY = "aether.db.items";

async function persistedItems(): Promise<Browse | null> {
  try {
    const { value } = await Preferences.get({ key: ITEM_CACHE_KEY });
    return value ? (JSON.parse(value) as Browse) : null;
  } catch {
    return null;
  }
}

/** Drop the stored Items index (the Refresh control on that list). */
export async function clearItemCache(): Promise<void> {
  memo.delete("item");
  await Preferences.remove({ key: ITEM_CACHE_KEY }).catch(() => {});
}

// ---------------------------------------------------------------- calls

/** One kind's records, grouped server-side. Cached; `force` refetches. */
export async function browse(kind: Kind, force = false): Promise<Browse> {
  if (!force) {
    const hit = memo.get(kind);
    if (hit) return hit;
    if (kind === "item") {
      const stored = await persistedItems();
      if (stored) { memo.set(kind, stored); return stored; }
    }
  }
  const out = await apiGet<Browse>(`/db/browse?kind=${encodeURIComponent(kind)}`);
  // A truncated build (the backend flags `partial`) is usable but must not be
  // cached, or a bad network moment sticks around for the session.
  if (!out.partial) {
    memo.set(kind, out);
    if (kind === "item") {
      await Preferences.set({ key: ITEM_CACHE_KEY, value: JSON.stringify(out) }).catch(() => {});
    }
  }
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
