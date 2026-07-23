// Runtime API client for the Aether companion.
//
// Unlike the desktop's api.ts (which hard-codes 127.0.0.1:8756), the base URL is
// set at RUNTIME — after pairing, or from manual host entry in Phase 0 — and
// every request carries the device bearer token once paired. The chat-stream
// reader below is ported verbatim from the desktop so Phase 2 is a drop-in.

export type Health = {
  ok: boolean;
  app?: string;
  server_id?: string;
  server_name?: string;
};

// Subset of the desktop's ChatEvent union — enough for the companion's compact
// answer view. Extend as the mobile UI grows.
export type ChatEvent =
  | { type: "token"; text: string }
  | { type: "tool_call"; name: string }
  | { type: "tool_result"; name: string; ok: boolean }
  | { type: "source"; label: string; url: string }
  | { type: "done" }
  | { type: "error"; message: string };

export type Auth = "subscription" | "api";

let _base = "";
let _token = "";

/** Point the client at a desktop (after pairing or manual entry). */
export function setConnection(base: string, token = ""): void {
  _base = base.replace(/\/+$/, "");
  _token = token;
}
export function currentBase(): string {
  return _base;
}
export function isPaired(): boolean {
  return !!_token;
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return _token ? { Authorization: `Bearer ${_token}`, ...extra } : extra;
}

/** Liveness + identity. Phase 0 passes an explicit `base` to probe a typed host
 * before committing to it; once connected it defaults to the current base.
 * `/health` is an open route, so this works before a token exists. */
export async function health(base: string = _base): Promise<Health> {
  const target = base.replace(/\/+$/, "");
  const r = await fetch(`${target}/health`, { headers: authHeaders() });
  if (!r.ok) throw new Error(`Backend returned ${r.status}`);
  return (await r.json()) as Health;
}

/** Stream a chat response — Phase 2. Ported from the desktop's SSE-over-POST
 * reader: `/chat` returns a text stream of `data: {json}\n\n` frames. This is
 * the single biggest reuse win of the Capacitor path (no native rewrite). */
export async function streamChat(
  opts: { chatId: string; model: string; auth: Auth; message: string },
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch(`${_base}/chat`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      chat_id: opts.chatId,
      model: opts.model,
      auth: opts.auth,
      message: opts.message,
    }),
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
          onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
        } catch {
          /* ignore malformed keep-alive frames */
        }
      }
    }
  }
}
