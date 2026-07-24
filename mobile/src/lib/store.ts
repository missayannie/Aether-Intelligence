// Connection persistence.
//
// Split by sensitivity: host/serverId/serverName are ordinary settings and live
// in @capacitor/preferences (UserDefaults on iOS, localStorage on the web); the
// device TOKEN is a long-lived credential and lives in the iOS Keychain via
// secure.ts. The `Connection` shape is unchanged to the rest of the app — this
// file is the only place that knows the two halves are stored differently.
import { Preferences } from "@capacitor/preferences";
import { getSecret, removeSecret, setSecret } from "./secure";

export type Connection = {
  host: string;        // last-known-good base, e.g. "http://desktop.tailnet.ts.net:8756"
  hosts: string[];     // all candidate bases from pairing — for roaming reconnect
  token: string;       // device bearer token ("" until paired — Phase 1)
  serverId: string;    // pinned so we know we're reconnecting to the same PC
  serverName: string;
};

const KEY = "aether.connection";
const TOKEN_KEY = "aether.token";

export async function saveConnection(c: Connection): Promise<void> {
  const { token, ...rest } = c;
  await Preferences.set({ key: KEY, value: JSON.stringify(rest) });
  if (token) await setSecret(TOKEN_KEY, token);
  else await removeSecret(TOKEN_KEY).catch(() => {});
}

export async function loadConnection(): Promise<Connection | null> {
  const { value } = await Preferences.get({ key: KEY });
  if (!value) return null;
  let rest: Omit<Connection, "token">;
  try {
    rest = JSON.parse(value) as Omit<Connection, "token">;
    // Back-compat: connections stored before roaming had no `hosts` list.
    if (!Array.isArray(rest.hosts) || rest.hosts.length === 0) {
      rest.hosts = rest.host ? [rest.host] : [];
    }
  } catch {
    return null;
  }
  // A missing/unreadable Keychain entry means "not paired" rather than a hard
  // failure — the app falls back to the Pair screen and the user re-pairs.
  const token = await getSecret(TOKEN_KEY).catch(() => null);
  return { ...rest, token: token ?? "" };
}

export async function clearConnection(): Promise<void> {
  await Preferences.remove({ key: KEY });
  await removeSecret(TOKEN_KEY).catch(() => {});
}

// Stable per-install id, sent with /pair/claim so re-pairing replaces this
// device's token rather than piling up duplicates in the desktop's list.
const DEVICE_ID_KEY = "aether.deviceId";

export async function getDeviceId(): Promise<string> {
  const { value } = await Preferences.get({ key: DEVICE_ID_KEY });
  if (value) return value;
  const id =
    (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`)
      .replace(/-/g, "")
      .slice(0, 12);
  await Preferences.set({ key: DEVICE_ID_KEY, value: id });
  return id;
}
