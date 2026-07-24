// Phase 1 — pairing. Turns a scanned/opened `aether://pair?…` link (or a pasted
// one) into a stored device token, then points the client at the desktop.
//
// Platform-agnostic on purpose: the QR *scanner* is native (added on the Mac),
// but everything here — parsing, the /pair/claim call, host failover, token
// storage — runs and is testable on the web target.
import { health, setConnection } from "./client";
import { getDeviceId, saveConnection } from "./store";

export type PairPayload = {
  hosts: string[];   // host:port candidates, most-reachable first
  code: string;      // single-use pairing code
  name?: string;     // desktop's display name (from the QR)
  sid?: string;      // desktop server id, for pinning
};

export type Claimed = { host: string; token: string; serverId: string; serverName: string };

// A stable, human-ish device name shown in the desktop's paired-devices list.
// The Mac build can replace this with the real device name via @capacitor/device.
const DEVICE_NAME = "iPhone (Aether Companion)";

/** Parse an `aether://pair?…` deep link, or a bare query string. */
export function parsePairUri(uri: string): PairPayload | null {
  try {
    const qs = uri.includes("?") ? uri.slice(uri.indexOf("?") + 1) : uri;
    const p = new URLSearchParams(qs);
    const hosts = (p.get("hosts") || "").split(",").map((s) => s.trim()).filter(Boolean);
    const code = p.get("code") || "";
    if (!hosts.length || !code) return null;
    return { hosts, code, name: p.get("name") || undefined, sid: p.get("sid") || undefined };
  } catch {
    return null;
  }
}

function toBase(hostPort: string): string {
  const h = /^https?:\/\//i.test(hostPort) ? hostPort : `http://${hostPort}`;
  return h.replace(/\/+$/, "");
}

/** Trade the code for a device token, trying each host until one answers. */
export async function claim(
  payload: PairPayload,
  deviceId: string,
  deviceName = DEVICE_NAME,
): Promise<Claimed> {
  let lastErr: unknown = null;
  for (const hp of payload.hosts) {
    const base = toBase(hp);
    try {
      await health(base); // reachability probe (open route) before the claim
      const r = await fetch(`${base}/pair/claim`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: payload.code, device_name: deviceName, device_id: deviceId }),
      });
      if (!r.ok) throw new Error(`claim failed: ${r.status} ${await r.text()}`);
      const j = (await r.json()) as { token: string; server_id: string; server_name: string };
      return { host: base, token: j.token, serverId: j.server_id, serverName: j.server_name };
    } catch (e) {
      lastErr = e; // try the next host
    }
  }
  throw new Error(
    `Couldn't reach the desktop at any of its addresses. ${lastErr instanceof Error ? lastErr.message : ""}`,
  );
}

/** End-to-end: parse → claim → persist → point the client at the desktop. */
export async function claimFromUri(uri: string): Promise<Claimed | null> {
  const payload = parsePairUri(uri);
  if (!payload) return null;
  const deviceId = await getDeviceId();
  const c = await claim(payload, deviceId);
  await saveConnection({
    host: c.host,
    hosts: payload.hosts.map(toBase), // keep every address for roaming reconnect
    token: c.token, serverId: c.serverId, serverName: c.serverName,
  });
  setConnection(c.host, c.token); // subsequent requests now carry the bearer token
  return c;
}
