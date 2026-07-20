// API client for the local FastAPI backend.

// VITE_BACKEND lets a dev session point at a backend on another port — the packaged
// app always uses the default, which is where the sidecar binds.
const BASE = (import.meta as any).env?.VITE_BACKEND || "http://127.0.0.1:8756";
// One value per app launch — appended to map image urls to defeat stale webview
// HTTP-cache entries while still allowing within-session reuse.
const SESSION = Date.now().toString(36);

export type Auth = "subscription" | "api";

export type Model = {
  provider: string;
  provider_label: string;
  id: string;
  label: string;
  tool_use: string;
  recommended: boolean;
  available: boolean;
  auth_options: Auth[];
  default_auth: Auth | null;
  // Per-token prices, sourced from litellm (the library that actually bills the
  // request) rather than a table of our own. 0 means "unknown", not "free".
  input_cost_per_token: number;
  output_cost_per_token: number;
};

export type SubStatus = {
  cli_found: boolean;
  cli_path: string | null;
  token_set: boolean;
  ready: boolean;
  setup_command: string;
};

// The server orders the sidebar by updated_at (most recently ACTIVE first) —
// using an old chat bumps it to the top.
export type ChatSummary = {
  id: string; title: string; count: number; owner: string;
  surface?: string;   // "overlay" = the in-game Ask pill's chat
  created_at?: string; updated_at?: string;
};
export type Workspace = { slug: string; display_name: string; character_id: string; kind: "global" | "profile" };
export type CharacterHit = { id: string; name: string; world: string };
export type BoundCharacter = { id: string; name: string; world: string; data_center: string; active_job?: string };
export type Attachment = { name: string; kind: string; size: number; chars: number };
export type Message = {
  role: "user" | "assistant";
  content: string;
  // Draft docs the assistant produced on THIS turn, rendered inline under the
  // message (session-local — the backend stores only role/content).
  docLinks?: { id: string; title: string; draft: boolean }[];
};
export type DocItem = {
  id: string; content: string; title?: string; draft?: boolean;
  // Shared items stay in their own chat but stay findable from your other profiles.
  shared?: boolean;
};
// --- Eorzea Database browser (right-panel tab) ---
// Rendered from our own scraper: the Lodestone sends X-Frame-Options: SAMEORIGIN, so
// it cannot be iframed cross-origin.
// "all" is not a Garland type — it's the absence of a filter. One search.php call
// already returns every type, so the backend maps any non-LINKABLE kind to no filter.
export type DbKind =
  | "all"
  | "item" | "instance" | "quest" | "npc" | "mob"
  | "achievement" | "fate" | "node" | "leve";
export type DbHit = {
  name: string; url: string; id: string; type: string;
  item_level: number; icon: string;
};
export type DbComment = { text: string; author: string; date: string };
export type DbRef = { id: string; name: string; item_level?: number; qty?: number };
// `name` is the NODE ("The Xobr'it Cinderfield"); `zone` is the map you open to find
// it ("Yak T'el"). The zone always opens on the rebuilt in-game map, pinned by `id`.
export type DbNode = {
  id: string; name: string; level: number; type: string; zone?: string;
};
export type DbMarket = {
  world_or_dc: string; lowest: number; average: number; world: string; listings: number;
};
// A clickable cross-reference inside a non-item detail (a reward item, the
// quest giver, the next quest…). kind+id feed straight back into openDbKind.
// icon: the item's own icon; sub: a small annotation (fish level).
export type DbRefLink = { kind: string; id: string; name: string; icon?: string; sub?: string };
/** One record of any non-item kind, in a uniform render-ready shape.
 *  image: a large picture (NPC photo, duty banner); icon_name: a named game
 *  symbol for the header when Garland has no direct icon (fate, node…). */
export type DbDetailDoc = {
  found: boolean; kind: string; id: string; url: string;
  name?: string; icon?: string; icon_name?: string; image?: string;
  sub?: string; description?: string;
  fields?: { label: string; value: string }[];
  location?: { zone: string; x: number; y: number; label: string;
               icon?: string; radius?: number } | null;
  links?: { group: string; refs: DbRefLink[] }[];
};
export type DbBrowseRow = { id: string; name: string; sub: string; icon?: string };
export type DbBrowseGroup = { label: string; count: number; rows: DbBrowseRow[] };
export type DbBrowse = { kind: string; label: string; groups: DbBrowseGroup[] };
export type DbItem = {
  found: boolean; url: string;
  source?: string; name?: string; category?: string; item_level?: string;
  description?: string; details?: string; icon?: string; comments?: DbComment[];
  patch?: string; materia_slots?: number;
  attributes?: Record<string, number>;
  upgrades?: DbRef[]; downgrades?: DbRef[];
  // Sources & Uses — how you get one, and what it feeds into.
  sell_price?: number; tradeable?: boolean;
  nodes?: DbNode[]; ventures?: string[]; ingredient_of?: DbRef[]; vendors?: DbRef[];
  market?: DbMarket | null;
};

export type SearchHit = {
  kind: "doc" | "note" | "asset";
  id: string; chat_id: string; chat_title: string; owner: string;
  title: string; snippet: string; shared: boolean;
};
export type Chat = {
  id: string; title: string; messages: Message[];
  notes?: DocItem[]; docs?: DocItem[];
  // Cited sources, persisted server-side so the Sources tab survives a restart.
  sources?: { label: string; url: string }[];
  // NOTE: a *citation* label is a human string from the tool that produced it
  // ("A Realm Remapped (by Icarus Twine)"), not a SourceInfo.id — see matchSource().
  shared_assets?: string[];   // asset names findable from your other profiles
};

/** One marker on the in-game map. x/y are pixels in `coord_space` (2048), NOT screen
 *  pixels and NOT in-game flag coords — so they survive any zoom. */
export type MapMarker = {
  x: number; y: number; label: string; kind: string;
  icon: string; icon_id: number; detail?: string;
};
export type MapLayer = { id: string; label: string; glyph: string };
/** A player-placed pin, in the same 2048 coord space as the game's markers.
 *  kind groups pins under their own toolbar toggle ("gathering" → Custom – Gathering);
 *  icon is a named game symbol drawn instead of the coloured dot. */
export type CustomPin = { id: string; x: number; y: number; label: string; color?: string;
                          kind?: string; icon?: string };
/** A named game marker found by the map bar's search — same 2048 coord space. */
export type MarkerHit = {
  zone: string; x: number; y: number; label: string; kind: string; icon: string;
};
/** A zone's map: Garland's texture + the game's own markers (via XIVAPI). */
export type ZoneMap = {
  found: boolean; zone: string; texture: string; size_factor: number;
  coord_space: number; markers: MapMarker[]; layers: MapLayer[];
  sources: string[]; focus?: { x: number; y: number };
  /** The gathering node a GarlandDB link asked for — drawn as the temp pin,
   *  deliberately NOT in markers (it rendered the same label twice). */
  node?: { x: number; y: number; label: string; kind: string; icon: string; detail?: string };
};

/** A project this app reads from. `support` is its own funding page, "" if it has none. */
export type SourceInfo = { id: string; label: string; url: string; support: string };

/** Find the project a citation came from.
 *
 * Citations carry a *display* label produced by whichever tool ran, so it rarely
 * equals the catalog label exactly ("A Realm Remapped (by Icarus Twine)" vs
 * "A Realm Remapped"). Match on label prefix, then fall back to the URL host so a
 * relabelled tool still resolves.
 */
export function matchSource(
  cited: { label: string; url: string },
  catalog: SourceInfo[],
): SourceInfo | undefined {
  const label = cited.label.toLowerCase();
  const byLabel = catalog.find((s) => s.label && label.startsWith(s.label.toLowerCase()));
  if (byLabel) return byLabel;
  const host = (u: string) => {
    try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; }
  };
  const h = host(cited.url);
  return h ? catalog.find((s) => s.url && host(s.url) === h) : undefined;
}

// A passive-chip watch (overlay). `place` is the raw map payload the chip
// re-opens on click — same shape the Ask card's "Open map" uses.
export type OverlayWatch = {
  id: string;
  kind: "pin" | "pinset" | "node";
  label: string;
  ref?: string;   // node: gathering-point id — the backend enriches the rest
  zone?: string;  // optional at arm time; the backend fills it for node watches
  x?: number;
  y?: number;
  icon?: string;
  category?: string;
  pins?: { x: number; y: number; label?: string }[];
  place?: Record<string, unknown>;
  created_at?: string;
};

// The guide checklist shown on the overlay (docs/overlay-spec.md concept 2).
export type ChecklistStep = { index: number; text: string; done: boolean };
export type OverlayChecklist = {
  pinned: boolean; chat_id?: string; doc_id?: string; title?: string;
  steps: ChecklistStep[];
};

// In-app updates, read from the project's GitHub Releases.
export type UpdateInfo = {
  found: boolean; current: string; newer?: boolean;
  // True when the release carries the SAME version but a newer installer
  // upload than the one this install came from (a rebuild of the same tag).
  rebuilt?: boolean;
  version?: string; tag?: string; name?: string; notes?: string;
  url?: string; size?: number; published_at?: string; asset_updated_at?: string;
};
export type UpdateStatus = {
  status: "idle" | "downloading" | "ready" | "error";
  pct: number; path: string; error: string; version: string;
};

export type OverlayTimer = {
  id: string;          // the watch id
  timed: boolean;
  active?: boolean;    // window open right now
  opens_at?: number;   // unix seconds
  closes_at?: number;
};

export type ChatEvent =
  | { type: "token"; text: string }
  | { type: "tool"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; name: string; ok: boolean }
  | { type: "source"; label: string; url: string }
  | { type: "asset"; name: string; kind?: string; zone?: string; url?: string }
  | { type: "map"; url: string; zone: string; focus?: { x: number; y: number } | null;
      pin?: { x: number; y: number; label: string; icon?: string; radius_px?: number } | null;
      // A CATEGORY of temp pins at once (pin_points_on_map), 2048 map space.
      pins?: { x: number; y: number; label: string }[] | null;
      category?: string; icon?: string }
  | { type: "ask"; id: string; question: string; options: string[]; header: string }
  | { type: "doc"; id: string; title: string; draft: boolean; content: string }
  | { type: "doc_edited"; content: string }   // in-place edit of the open doc
  | { type: "done" }
  | { type: "error"; message: string };

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  health: () => j<{ ok: boolean }>("/health"),

  // Wait for the Python backend sidecar to be up. On a cold launch it takes a
  // few seconds to start, so initial loads must gate on this or they race it and
  // fail silently (empty chat list, subscription looking un-set-up).
  async ready(timeoutMs = 40000): Promise<boolean> {
    const start = Date.now();
    for (;;) {
      try {
        const r = await fetch(BASE + "/health");
        if (r.ok) return true;
      } catch {
        /* backend not listening yet */
      }
      if (Date.now() - start > timeoutMs) return false;
      await new Promise((res) => setTimeout(res, 350));
    }
  },
  models: () =>
    j<{
      models: Model[];
      default: { id: string; auth: Auth } | null;
      system_tokens: number;   // re-sent every turn; needed for a cost estimate
    }>("/models"),
  keys: () => j<Record<string, boolean>>("/keys"),
  usageSummary: () =>
    j<{
      billed_usd: number;      // real money spent on API keys
      covered_usd: number;     // what subscription turns would have cost
      estimated: boolean;      // true if any line fell back to a chars/4 guess
      rows: { model: string; auth: string; context: string; turns: number;
              input_tokens: number; output_tokens: number; cost_usd: number }[];
    }>("/usage/summary"),
  setKey: (provider: string, api_key: string) =>
    j(`/keys/${provider}`, { method: "POST", body: JSON.stringify({ api_key }) }),
  deleteKey: (provider: string) => j(`/keys/${provider}`, { method: "DELETE" }),

  // App-wide UI settings, persisted server-side in the per-user data dir so they
  // survive reinstalls (unlike WebView localStorage, which the installer wipes).
  getAppSettings: () => j<Record<string, unknown>>("/settings"),
  putAppSettings: (settings: Record<string, unknown>) =>
    j("/settings", { method: "PUT", body: JSON.stringify({ settings }) }),

  subStatus: () => j<SubStatus>("/subscription/status"),
  setSubToken: (token: string) =>
    j("/subscription/token", { method: "POST", body: JSON.stringify({ api_key: token }) }),
  deleteSubToken: () => j("/subscription/token", { method: "DELETE" }),

  // Search docs/notes/assets. scope "global" spans every profile; "workspace" is the
  // current profile plus anything marked shared anywhere.
  search: (q: string, owner: string, scope: "workspace" | "global") =>
    j<{ hits: SearchHit[] }>(
      `/search?q=${encodeURIComponent(q)}&owner=${encodeURIComponent(owner)}&scope=${scope}`,
    ),
  setAssetShared: (chatId: string, name: string, shared: boolean) =>
    j(`/chats/${chatId}/assets/${encodeURIComponent(name)}/shared`, {
      method: "POST", body: JSON.stringify({ shared }),
    }),

  // Eorzea Database browser
  dbSearch: (q: string, kind: DbKind) =>
    j<{ kind: DbKind; hits: DbHit[] }>(
      `/db/search?q=${encodeURIComponent(q)}&kind=${kind}`,
    ),
  // Promote an agent-fetched TEMPORARY image (tmp_*) to a real shelf asset.
  keepAsset: (chatId: string, name: string) =>
    j<{ ok: boolean; asset_id: string }>(
      `/chats/${chatId}/assets/${encodeURIComponent(name)}/keep`, { method: "POST" }),

  dbItem: (url: string) => j<DbItem>(`/db/item?url=${encodeURIComponent(url)}`),
  dbDetail: (kind: string, id: string) =>
    j<DbDetailDoc>(`/db/detail?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`),
  // Garland-style Browse: one kind's whole catalogue, grouped like Garland's UI.
  dbBrowse: (kind: string) => j<DbBrowse>(`/db/browse?kind=${encodeURIComponent(kind)}`),

  // The projects this app reads from, each with its own funding page (may be "").
  sources: () => j<{ sources: SourceInfo[] }>("/sources"),

  // The in-game map for a zone. node_id pins one gathering node and sets `focus`.
  // Texture/icon come back as backend-relative paths (served from its disk cache);
  // absolutize them here so <img src> works. The per-session `v=` param busts the
  // WEBVIEW's HTTP cache: a bad response cached under Cache-Control once kept one
  // zone broken across restarts while the backend served it fine. The backend's
  // own disk cache still does the real caching.
  zoneMap: async (zone: string, nodeId = "") => {
    const z = await j<ZoneMap>(`/map/zone?zone=${encodeURIComponent(zone)}` +
      (nodeId ? `&node_id=${encodeURIComponent(nodeId)}` : ""));
    const bust = (u: string) => BASE + u + (u.includes("?") ? "&" : "?") + "v=" + SESSION;
    if (z.texture.startsWith("/")) z.texture = bust(z.texture);
    for (const m of z.markers)
      if (m.icon.startsWith("/")) m.icon = bust(m.icon);
    return z;
  },
  // Drawable zones grouped by region, in the in-game picker's order. `complete`
  // false = the list was truncated by a flaky fetch and must not be cached.
  mapZones: () =>
    j<{ regions: { region: string; zones: string[] }[]; complete?: boolean }>("/map/zones"),

  // The player's own map pins (global — a pin marks a place, not a conversation).
  mapPins: (zone: string) =>
    j<{ pins: CustomPin[] }>(`/map/pins?zone=${encodeURIComponent(zone)}`),
  // Every zone's pins at once — the map bar's pin search jumps across zones.
  allMapPins: () => j<{ zones: Record<string, CustomPin[]> }>("/map/pins/all"),
  // Named game markers (aetherytes, dungeons, landmarks…) matching q, all zones.
  searchMapMarkers: async (q: string) => {
    const r = await j<{ markers: MarkerHit[] }>(
      `/map/markers/search?q=${encodeURIComponent(q)}`);
    for (const m of r.markers)
      if (m.icon.startsWith("/")) m.icon = BASE + m.icon;
    return r;
  },
  addMapPin: (zone: string, x: number, y: number, label: string, color = "",
              kind = "", icon = "") =>
    j<CustomPin>("/map/pins", { method: "POST",
                                body: JSON.stringify({ zone, x, y, label, color, kind, icon }) }),
  updateMapPin: (zone: string, id: string, patch: { label?: string; color?: string }) =>
    j<CustomPin>(`/map/pins/${id}`, { method: "PATCH", body: JSON.stringify({ zone, ...patch }) }),
  deleteMapPin: (zone: string, id: string) =>
    j<{ ok: boolean }>(`/map/pins/${id}?zone=${encodeURIComponent(zone)}`, { method: "DELETE" }),

  // Save a UI-generated image (captioned map screenshot) as a chat asset.
  uploadAsset: async (chatId: string, blob: Blob, name: string) => {
    const fd = new FormData();
    fd.append("file", blob, "shot.png");
    const r = await fetch(BASE + `/chats/${chatId}/assets?name=${encodeURIComponent(name)}`,
                          { method: "POST", body: fd });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json() as Promise<{ ok: boolean; asset_id: string }>;
  },

  // Cross-profile context the AI reads in every profile (replaced the global workspace).
  sharedProfile: async () => (await fetch(`${BASE}/shared-profile`)).text(),
  putSharedProfile: (content: string) =>
    j("/shared-profile", { method: "PUT", body: JSON.stringify({ content }) }),

  // Standing agent-behaviour preferences — the agent appends, the player edits.
  getPreferences: async () => (await fetch(`${BASE}/preferences`)).text(),
  putPreferences: (content: string) =>
    j("/preferences", { method: "PUT", body: JSON.stringify({ content }) }),

  // Profile workspaces
  workspaces: () => j<{ workspaces: Workspace[] }>("/workspaces"),
  createWorkspace: (display_name: string) =>
    j<Workspace>("/workspaces", { method: "POST", body: JSON.stringify({ display_name }) }),
  deleteWorkspace: (slug: string) =>
    j<{ ok: boolean }>(`/workspaces/${slug}`, { method: "DELETE" }),
  getWsProfile: async (slug: string) => (await fetch(`${BASE}/workspaces/${slug}/profile`)).text(),
  putWsProfile: (slug: string, content: string) =>
    j(`/workspaces/${slug}/profile`, { method: "PUT", body: JSON.stringify({ content }) }),
  getWsCharacter: (slug: string) =>
    j<{ character: BoundCharacter | null }>(`/workspaces/${slug}/character`),
  charSearch: (slug: string, q: string, world: string) =>
    j<{ results: CharacterHit[] }>(`/workspaces/${slug}/character/search`, {
      method: "POST", body: JSON.stringify({ q, world }),
    }),
  charBind: (slug: string, payload: { id?: string; url?: string }) =>
    j<BoundCharacter>(`/workspaces/${slug}/character/bind`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  charRefresh: (slug: string) =>
    j<BoundCharacter>(`/workspaces/${slug}/character/refresh`, { method: "POST" }),

  listChats: () => j<{ chats: ChatSummary[] }>("/chats"),
  // Overlay watches — the passive chips' data (pins/pinsets armed from answer
  // cards or the app; docs/overlay-spec.md §6.3).
  // The overlay's guide checklist: one doc pinned to the in-game widget.
  checklistGet: () => j<OverlayChecklist>("/overlay/checklist"),
  checklistPin: (chat_id: string, doc_id: string) =>
    j<{ ok: boolean }>("/overlay/checklist", {
      method: "POST", body: JSON.stringify({ chat_id, doc_id }),
    }),
  checklistUnpin: () => j<{ ok: boolean }>("/overlay/checklist", { method: "DELETE" }),
  checklistToggle: (index: number) =>
    j<{ ok: boolean; steps: ChecklistStep[] }>("/overlay/checklist/toggle", {
      method: "POST", body: JSON.stringify({ index }),
    }),

  // In-app updates, from the project's GitHub Releases.
  updateCheck: (current: string, since = "") =>
    j<UpdateInfo>(`/update/check?current=${encodeURIComponent(current)}`
      + `&since=${encodeURIComponent(since)}`),
  updateDownload: () =>
    j<{ ok: boolean; version: string }>("/update/download", { method: "POST" }),
  updateStatus: () => j<UpdateStatus>("/update/status"),

  overlayWatches: () => j<{ watches: OverlayWatch[] }>("/overlay/watches"),
  overlayWatchAdd: (w: Omit<OverlayWatch, "id" | "created_at">) =>
    j<{ ok: boolean; watch: OverlayWatch }>("/overlay/watches", {
      method: "POST",
      body: JSON.stringify(w),
    }),
  overlayWatchRemove: (id: string) =>
    j<{ ok: boolean }>(`/overlay/watches/${id}`, { method: "DELETE" }),
  // Real-clock open/close for timed watches (unix seconds — tick locally).
  overlayTimers: () =>
    j<{ now: number; timers: OverlayTimer[] }>("/overlay/timers"),

  createChat: (owner: string) =>
    j<Chat>("/chats", { method: "POST", body: JSON.stringify({ owner }) }),
  getChat: (id: string) => j<Chat>(`/chats/${id}`),
  moveChat: (id: string, owner: string) =>
    j<{ ok: boolean; owner: string }>(`/chats/${id}/move`, {
      method: "POST", body: JSON.stringify({ owner }),
    }),
  deleteChat: (id: string) => j<{ ok: boolean }>(`/chats/${id}`, { method: "DELETE" }),
  putMessages: (id: string, messages: Message[]) =>
    j(`/chats/${id}/messages`, { method: "PUT", body: JSON.stringify({ messages }) }),
  suggestions: (chat_id: string, model: string, auth: Auth) =>
    j<{ suggestions: string[] }>("/chat/suggestions", {
      method: "POST",
      body: JSON.stringify({ chat_id, model, auth }),
    }),

  assetUrl: (chatId: string, name: string) =>
    `${BASE}/chats/${chatId}/assets/${name}`,
  // A named game icon (the agent's icon vocabulary) — what `icon:<name>`
  // markdown and typed map pins resolve to.
  iconByName: (name: string) => `${BASE}/icons/by-name/${encodeURIComponent(name)}`,
  // Any game icon by raw id, through the disk-cached XIVAPI proxy.
  gameIcon: (id: number) => `${BASE}/map/icon?id=${id}`,
  listAssets: (id: string) => j<{ assets: string[] }>(`/chats/${id}/assets`),
  putNotes: (id: string, items: DocItem[]) =>
    j(`/chats/${id}/notes`, { method: "PUT", body: JSON.stringify({ items }) }),
  putDocs: (id: string, items: DocItem[]) =>
    j(`/chats/${id}/docs`, { method: "PUT", body: JSON.stringify({ items }) }),

  listAttachments: (id: string) =>
    j<{ attachments: Attachment[] }>(`/chats/${id}/attachments`),
  async attachFiles(id: string, files: File[]): Promise<{ attachments: Attachment[] }> {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, (f as any).webkitRelativePath || f.name);
    const r = await fetch(BASE + `/chats/${id}/attach`, { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  deleteAttachment: (id: string, name: string) => {
    const enc = name.split("/").map(encodeURIComponent).join("/");
    return j<{ attachments: Attachment[] }>(`/chats/${id}/attachments/${enc}`, { method: "DELETE" });
  },
  annotateAsset: (
    id: string,
    asset_id: string,
    title: string,
    annotations: unknown[],
  ) =>
    j<{ ok: boolean; asset_id: string }>(`/chats/${id}/annotate`, {
      method: "POST",
      body: JSON.stringify({ asset_id, title, annotations }),
    }),

  // Answer a pending ask_user question; resumes the still-open chat stream.
  answer: (ask_id: string, answer: string) =>
    j<{ ok: boolean }>("/chat/answer", {
      method: "POST",
      body: JSON.stringify({ ask_id, answer }),
    }),

  // One doc's side thread ("subchat"). Persisted per doc on the chat, so reopening
  // the bubble shows the context again. The agent edits via its update_doc tool and
  // REPLIES conversationally — the reply is what shows in the thread.
  subchat: (chat_id: string, kind: "docs" | "notes", doc_id: string) =>
    j<{ messages: Message[] }>(
      `/chats/${chat_id}/subchat?kind=${kind}&doc_id=${encodeURIComponent(doc_id)}`,
    ),
  clearSubchat: (chat_id: string, kind: "docs" | "notes", doc_id: string) =>
    j<{ ok: boolean }>(
      `/chats/${chat_id}/subchat?kind=${kind}&doc_id=${encodeURIComponent(doc_id)}`,
      { method: "DELETE" },
    ),
  async editDoc(
    chat_id: string,
    kind: "docs" | "notes",
    doc_id: string,
    instruction: string,
    model: string,
    auth: Auth,
    onEvent: (e: ChatEvent) => void,
  ): Promise<void> {
    const r = await fetch(BASE + "/docs/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id, kind, doc_id, instruction, model, auth }),
    });
    if (!r.ok || !r.body) throw new Error(`edit failed: ${r.status} ${await r.text()}`);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.trim();
        if (line.startsWith("data:")) {
          try { onEvent(JSON.parse(line.slice(5).trim())); } catch { /* keep-alive */ }
        }
      }
    }
  },

  // Stream a chat response. Calls onEvent for each SSE event.
  async streamChat(
    chat_id: string,
    model: string,
    auth: Auth,
    message: string,
    onEvent: (e: ChatEvent) => void,
    ignoreProfile = false,
    signal?: AbortSignal,
    surface = "",   // "overlay" = the in-game Ask pill (compact-card answers)
    screenshot = "",  // one-shot data-URL JPEG of the game screen (overlay §6.5)
  ): Promise<void> {
    const r = await fetch(BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id, model, message, auth,
                             ignore_profile: ignoreProfile, surface, screenshot }),
      signal,
    });
    if (!r.ok || !r.body) throw new Error(`chat failed: ${r.status}`);

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.trim();
        if (line.startsWith("data:")) {
          try {
            onEvent(JSON.parse(line.slice(5).trim()));
          } catch {
            /* ignore malformed keep-alive */
          }
        }
      }
    }
  },
};
