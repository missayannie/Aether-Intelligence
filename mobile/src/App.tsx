import { useEffect, useRef, useState } from "react";
import { App as CapApp } from "@capacitor/app";
import Pair from "./screens/Pair";
import Paired from "./screens/Paired";
import Chat from "./screens/Chat";
import Offline from "./screens/Offline";
import { claimFromUri, type Claimed } from "./lib/pairing";
import { loadConnection, saveConnection, type Connection } from "./lib/store";
import { reconnect, setConnection } from "./lib/client";

// Routing: not paired -> Pair. Paired + reachable -> Paired/Chat. Paired but the
// desktop isn't answering -> Offline (pairing kept, just reconnecting).
type Session = { serverName: string; host: string };
type Status = "connecting" | "online" | "offline";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [status, setStatus] = useState<Status>("connecting");
  const [view, setView] = useState<"home" | "chat">("home");
  const [ready, setReady] = useState(false);
  const [stored, setStored] = useState(false); // a saved pairing exists
  const connecting = useRef(false); // guard against overlapping reconnects

  // Try the last-good host first, then the rest (roaming: LAN at home, Tailscale
  // away). Remembers whichever answers.
  async function tryConnect(c: Connection): Promise<boolean> {
    if (connecting.current) return false;
    connecting.current = true;
    setStatus("connecting");
    try {
      const ordered = [c.host, ...c.hosts.filter((h) => h !== c.host)].filter(Boolean);
      const working = await reconnect(ordered, c.token);
      setSession({ serverName: c.serverName, host: working ?? c.host });
      if (working) {
        setStatus("online");
        if (working !== c.host) await saveConnection({ ...c, host: working });
        return true;
      }
      setStatus("offline");
      return false;
    } finally {
      connecting.current = false;
    }
  }

  useEffect(() => {
    loadConnection().then((c) => {
      if (c?.token) { setStored(true); void tryConnect(c); }
      else setStatus("offline");
      setReady(true);
    });

    let remove: (() => void) | undefined;
    // Deep-link pairing (iOS Camera scan) + re-probe when the app returns to
    // the foreground (roaming / desktop woke up). On web these are inert.
    const handles: Array<Promise<{ remove: () => void }>> = [];
    handles.push(
      CapApp.addListener("appUrlOpen", async (e) => {
        if (e.url?.startsWith("aether://pair")) {
          try {
            const c = await claimFromUri(e.url);
            if (c) { setSession({ serverName: c.serverName, host: c.host }); setStatus("online"); setView("home"); }
          } catch { /* surfaced on the Pair screen */ }
        }
      }),
    );
    handles.push(
      CapApp.addListener("appStateChange", ({ isActive }) => {
        if (isActive) loadConnection().then((c) => { if (c?.token) void tryConnect(c); });
      }),
    );
    Promise.all(handles)
      .then((hs) => { remove = () => hs.forEach((h) => void h.remove()); })
      .catch(() => { /* @capacitor/app unavailable (plain web) */ });

    return () => { if (remove) remove(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!ready) return null;

  const onPaired = (c: Claimed) => {
    setSession({ serverName: c.serverName, host: c.host });
    setStatus("online");
    setView("home");
  };
  const forget = () => { setConnection("", ""); setSession(null); setStatus("offline"); setView("home"); };
  const retry = () => loadConnection().then((c) => (c ? tryConnect(c) : false));

  // A stored pairing is still probing — don't flash the Pair screen.
  if (!session && stored && status === "connecting") {
    return (
      <div className="screen splash">
        <span className="mark big">✦</span>
        <p className="sub">Reconnecting to your desktop…</p>
      </div>
    );
  }
  if (!session) return <Pair onPaired={onPaired} />;
  if (status === "offline") {
    return <Offline serverName={session.serverName} onRetry={retry} onForget={() => { void forget(); }} />;
  }
  if (view === "chat") {
    return <Chat serverName={session.serverName} onBack={() => setView("home")} />;
  }
  return (
    <Paired
      serverName={session.serverName}
      host={session.host}
      onAsk={() => setView("chat")}
      onForget={() => { void forget(); }}
    />
  );
}
