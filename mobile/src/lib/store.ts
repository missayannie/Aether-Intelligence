// Connection persistence.
//
// Uses @capacitor/preferences (UserDefaults on iOS, localStorage on the web),
// so it works in the browser during development and on device unchanged.
//
// NOTE for Phase 1: the device TOKEN is a long-lived credential and should move
// to a Keychain-backed secure-storage plugin before it's real. The shape here
// is deliberately a single object so that swap touches only this file — split
// the token into secure storage, keep host/serverId in Preferences.
import { Preferences } from "@capacitor/preferences";

export type Connection = {
  host: string;        // e.g. "http://desktop.tailnet.ts.net:8756"
  token: string;       // device bearer token ("" until paired — Phase 1)
  serverId: string;    // pinned so we know we're reconnecting to the same PC
  serverName: string;
};

const KEY = "aether.connection";

export async function saveConnection(c: Connection): Promise<void> {
  await Preferences.set({ key: KEY, value: JSON.stringify(c) });
}

export async function loadConnection(): Promise<Connection | null> {
  const { value } = await Preferences.get({ key: KEY });
  if (!value) return null;
  try {
    return JSON.parse(value) as Connection;
  } catch {
    return null;
  }
}

export async function clearConnection(): Promise<void> {
  await Preferences.remove({ key: KEY });
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
