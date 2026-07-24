// Keychain-backed secret storage for the device token.
//
// The token is a long-lived credential that authorizes every non-loopback
// request to the desktop, so it must not sit in Preferences (UserDefaults),
// which is plain plist data. @aparajita/capacitor-secure-storage puts it in the
// iOS Keychain, and falls back to web storage in a browser so `npm run dev`
// still works against the web target.
import { SecureStorage } from "@aparajita/capacitor-secure-storage";

export const setSecret = (k: string, v: string): Promise<void> => SecureStorage.set(k, v);

export const getSecret = (k: string): Promise<string | null> =>
  SecureStorage.get(k) as Promise<string | null>;

export const removeSecret = (k: string): Promise<boolean> => SecureStorage.remove(k);
