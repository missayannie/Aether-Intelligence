// Icon URL resolution for the Database tab.
//
// The backend builds icon URLs for its OWN consumption — absolute, pointing at
// http://127.0.0.1:<port>/map/icon?id=… — because on the desktop the UI and the
// backend share a loopback origin. Neither assumption holds on the phone:
//
//   1. 127.0.0.1 on the phone is the phone. The host must be re-pointed at
//      whichever desktop address we're currently paired through (LAN or
//      Tailscale — it changes as you roam).
//   2. Those routes sit behind the companion token gate, and an <img> tag can't
//      attach an Authorization header. So they're fetched with the token and
//      handed to the <img> as an object URL.
//
// Garland's public art (garlandtools.org) needs neither and is passed through.
import { authHeaders, currentBase } from "./client";

export { currentBase };

/** Does this URL point at the desktop backend (so: rewrite + authed fetch)? */
export function isDesktopIcon(url: string): boolean {
  if (!url) return false;
  if (/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) return true;
  // Already-current-base URLs (or bare paths) are ours too.
  if (url.startsWith("/")) return true;
  const base = currentBase();
  return !!base && url.startsWith(base);
}

/** Re-point a desktop icon URL at the paired desktop, keeping path + query. */
export function normalizeIconUrl(url: string, base: string): string {
  if (!base) return url;
  if (url.startsWith("/")) return `${base}${url}`;
  try {
    const u = new URL(url);
    return `${base}${u.pathname}${u.search}`;
  } catch {
    return url;
  }
}

// Object URLs are cached for the session and deliberately never revoked: the
// same sprite recurs across many rows, and re-fetching per render would be far
// more expensive than holding a few hundred small blobs. In-flight requests are
// shared so a list of 50 rows using one icon issues a single fetch.
const cache = new Map<string, string>();
const inflight = new Map<string, Promise<string>>();

/** Fetch a token-gated icon and return an object URL ("" if it can't load). */
export function iconObjectUrl(url: string): Promise<string> {
  const hit = cache.get(url);
  if (hit !== undefined) return Promise.resolve(hit);
  const running = inflight.get(url);
  if (running) return running;

  const p = (async () => {
    try {
      const r = await fetch(url, { headers: authHeaders() });
      if (!r.ok) throw new Error(String(r.status));
      const obj = URL.createObjectURL(await r.blob());
      cache.set(url, obj);
      return obj;
    } catch {
      cache.set(url, ""); // negative-cache: don't retry a 404 on every scroll
      return "";
    } finally {
      inflight.delete(url);
    }
  })();
  inflight.set(url, p);
  return p;
}
