import { useState } from "react";

// Shown when a stored pairing exists but the desktop can't be reached right now
// (asleep, app closed, off-network). The pairing is kept — this is a reconnect
// state, not an un-pairing.
export default function Offline({
  serverName,
  onRetry,
  onForget,
}: {
  serverName: string;
  onRetry: () => Promise<boolean>;
  onForget: () => void;
}) {
  const [trying, setTrying] = useState(false);

  async function retry() {
    setTrying(true);
    try {
      await onRetry();
    } finally {
      setTrying(false);
    }
  }

  return (
    <div className="screen">
      <header className="hd">
        <span className="mark">✦</span>
        <div>
          <h1>Can’t reach your desktop</h1>
          <p className="sub">Still paired with {serverName || "your PC"} — it just isn’t answering right now.</p>
        </div>
      </header>

      <div className="card err">
        <span className="pill">Offline</span>
        <p>Make sure the desktop app is open and you’re on the same Wi-Fi or Tailscale network, then try again.</p>
      </div>

      <button className="btn" onClick={() => void retry()} disabled={trying}>
        {trying ? "Reconnecting…" : "Retry"}
      </button>

      <p className="foot">Your pairing is saved — you won’t need to scan again.</p>
      <button className="btn ghost danger" onClick={onForget}>Forget this desktop</button>
    </div>
  );
}
