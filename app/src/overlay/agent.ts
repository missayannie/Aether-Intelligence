// The Ask pill's backend plumbing (phase 1, docs/overlay-spec.md §6.1).
//
// The pill rides the app's normal /chat pipeline with surface="overlay" (the
// backend adds a compact-card system block), into ONE rolling overlay chat —
// follow-up questions keep their context, and the whole conversation is
// visible in the app's sidebar. The card is assembled CLIENT-side from the
// event stream (tokens → text, `map` → place, `source` → citation): no second
// model pass, no distillation cost.
import { api, type Auth, type ChatEvent, type OverlayWatch } from "../api";

const CHAT_KEY = "ov-chat-id";

export type CardPlace = {
  zone: string;
  focus?: { x: number; y: number } | null;
  pin?: { x: number; y: number; label: string; icon?: string; radius_px?: number } | null;
  // A CATEGORY of pins from one answer (pin_points_on_map) — armed as ONE chip.
  pins?: { x: number; y: number; label?: string }[] | null;
  category?: string;
  icon?: string;
};

export type Card = {
  status: string;          // live status line while the agent researches
  text: string;            // the accumulated answer
  place?: CardPlace;       // set when the agent pinned a spot (Open map action)
  sources: string[];
  done: boolean;
  error?: string;
};

/** Same model/auth resolution chain as the main app's startup (App.tsx):
 * saved default if still available, else the server default, else first. */
async function resolveModel(): Promise<{ model: string; auth: Auth } | null> {
  try {
    const r = await api.models();
    let saved: Record<string, unknown> = {};
    try {
      saved = (await api.getAppSettings()) as Record<string, unknown>;
    } catch { /* server default */ }
    const wanted = typeof saved.defaultModel === "string" ? saved.defaultModel : "";
    const hit = r.models.find((m) => m.id === wanted && m.available);
    if (hit) {
      const a = saved.defaultAuth as Auth | undefined;
      return { model: hit.id, auth: a && hit.auth_options.includes(a) ? a : (hit.default_auth ?? "api") };
    }
    if (r.default) return { model: r.default.id, auth: r.default.auth };
    if (r.models[0]) return { model: r.models[0].id, auth: r.models[0].default_auth ?? "api" };
  } catch { /* backend unreachable */ }
  return null;
}

/** The rolling overlay chat — created lazily, reused across asks, recreated
 * if the saved one was deleted in the app. */
async function overlayChatId(): Promise<string> {
  const saved = localStorage.getItem(CHAT_KEY);
  if (saved) {
    try {
      await api.getChat(saved);
      return saved;
    } catch { /* deleted — make a fresh one */ }
  }
  const c = await api.createChat("");
  localStorage.setItem(CHAT_KEY, c.id);
  return c.id;
}

/** Arm the card's pins as a passive chip: a whole pin CATEGORY becomes one
 * pinset chip; a single pin becomes a pin chip. Returns the created watch. */
export async function armChips(place: CardPlace): Promise<OverlayWatch> {
  const base = { zone: place.zone, place: place as Record<string, unknown> };
  if (place.pins?.length) {
    const r = await api.overlayWatchAdd({
      ...base,
      kind: "pinset",
      label: `${place.category || "Pins"} — ${place.zone} (${place.pins.length})`,
      category: place.category || "Pins",
      icon: place.icon || "",
      pins: place.pins,
    });
    return r.watch;
  }
  const r = await api.overlayWatchAdd({
    ...base,
    kind: "pin",
    label: place.pin?.label || place.zone,
    x: place.pin?.x,
    y: place.pin?.y,
    icon: place.pin?.icon || "",
  });
  return r.watch;
}

export async function fetchWatches(): Promise<OverlayWatch[]> {
  try {
    return (await api.overlayWatches()).watches;
  } catch {
    return [];
  }
}

export async function removeWatch(id: string): Promise<void> {
  try {
    await api.overlayWatchRemove(id);
  } catch { /* already gone */ }
}

/** One ask: stream the agent turn, updating the card as events land. */
export async function ask(q: string, onUpdate: (c: Card) => void,
                          screenshot = ""): Promise<void> {
  const card: Card = { status: "Connecting…", text: "", sources: [], done: false };
  const push = () => onUpdate({ ...card, sources: [...card.sources] });
  push();

  const picked = await resolveModel();
  if (!picked) {
    card.error = "The app's backend isn't reachable — is Aether Intelligence running?";
    card.done = true;
    push();
    return;
  }
  let chatId: string;
  try {
    chatId = await overlayChatId();
  } catch {
    card.error = "Couldn't open an overlay chat on the backend.";
    card.done = true;
    push();
    return;
  }

  card.status = "Thinking…";
  push();
  try {
    await api.streamChat(chatId, picked.model, picked.auth, q, (ev: ChatEvent) => {
      if (ev.type === "token") {
        card.text += ev.text;
        card.status = "";
      } else if (ev.type === "tool") {
        card.status = "Researching…";
      } else if (ev.type === "map") {
        card.place = { zone: ev.zone, focus: ev.focus ?? null, pin: ev.pin ?? null,
                       pins: ev.pins ?? null, category: ev.category, icon: ev.icon };
      } else if (ev.type === "source" && ev.label && !card.sources.includes(ev.label)) {
        card.sources.push(ev.label);
      } else if (ev.type === "error") {
        card.error = ev.message;
      }
      push();
    }, false, undefined, "overlay", screenshot);
  } catch (e) {
    card.error = card.error || String(e);
  }
  card.done = true;
  card.status = "";
  push();
}

/** The overlay chat's recent turns, for the history box under the pill.
 * Read-only: no chat is created just to show an empty box. */
export async function fetchHistory(limit = 20): Promise<{ role: string; content: string }[]> {
  const saved = localStorage.getItem(CHAT_KEY);
  if (!saved) return [];
  try {
    const chat = await api.getChat(saved);
    return (chat.messages || []).slice(-limit)
      .map((m) => ({ role: m.role, content: m.content }));
  } catch {
    return [];
  }
}

/** Card text is markdown from the model — the card renders it as plain text,
 * so drop link/bold markup rather than showing raw brackets. */
export function plainText(md: string): string {
  return md
    // Angle-bracketed URLs first — map: links carry parens/queries the plain
    // pattern would stop at, leaving "&icon=…>)" residue in the card.
    .replace(/\[([^\]]+)\]\(<[^>]*>\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}
