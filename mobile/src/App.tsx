import { useEffect, useState } from "react";
import { App as CapApp } from "@capacitor/app";
import Pair from "./screens/Pair";
import Paired from "./screens/Paired";
import { claimFromUri, type Claimed } from "./lib/pairing";
import { loadConnection } from "./lib/store";
import { setConnection } from "./lib/client";

// Phase 1: pair, then show the paired status. If a token is already stored we
// skip straight to Paired; otherwise Pair. Phase 2 swaps Paired for the chat UI.
type Session = { serverName: string; host: string };

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // Restore a prior pairing on launch.
    loadConnection().then((c) => {
      if (c?.token && c.host) {
        setConnection(c.host, c.token);
        setSession({ serverName: c.serverName, host: c.host });
      }
      setReady(true);
    });

    // Handle the app being opened via the aether://pair deep link (iOS Camera
    // scan of the desktop QR). On web this listener simply never fires.
    let remove: (() => void) | undefined;
    CapApp.addListener("appUrlOpen", async (e) => {
      if (e.url?.startsWith("aether://pair")) {
        try {
          const c = await claimFromUri(e.url);
          if (c) setSession({ serverName: c.serverName, host: c.host });
        } catch {
          /* surfaced in the Pair screen if the user retries there */
        }
      }
    })
      .then((h) => { remove = () => void h.remove(); })
      .catch(() => { /* @capacitor/app unavailable (plain web) */ });

    return () => { if (remove) remove(); };
  }, []);

  if (!ready) return null;

  const onPaired = (c: Claimed) => setSession({ serverName: c.serverName, host: c.host });

  return session ? (
    <Paired serverName={session.serverName} host={session.host} onForget={() => setSession(null)} />
  ) : (
    <Pair onPaired={onPaired} />
  );
}
