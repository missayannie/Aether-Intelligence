import { useEffect, useState } from "react";
import { modelsCount, setConnection } from "../lib/client";
import { clearConnection } from "../lib/store";

// Shown once a device token exists. The models count is a real authed call —
// it only succeeds if the bearer token is valid, so it doubles as proof the
// pairing works. Phase 2 replaces this with the Ask (chat) screen.
export default function Paired({
  serverName,
  host,
  onForget,
}: {
  serverName: string;
  host: string;
  onForget: () => void;
}) {
  const [models, setModels] = useState<number | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    modelsCount().then(setModels).catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  async function forget() {
    await clearConnection();
    setConnection("", "");
    onForget();
  }

  return (
    <div className="screen">
      <header className="hd">
        <span className="mark">✦</span>
        <div>
          <h1>Paired</h1>
          <p className="sub">Your phone is authenticated to your desktop.</p>
        </div>
      </header>

      <div className="card ok">
        <span className="pill">Connected</span>
        <p className="big">{serverName || "Desktop"}</p>
        <p className="mono dim">{host}</p>
        {models !== null && <p>{models} model{models === 1 ? "" : "s"} available</p>}
        {err && <p className="dim">Token check failed: {err}</p>}
      </div>

      <p className="foot">
        Phase 1 complete — a device token is stored and every request is authenticated.
        The Ask (chat) screen lands in Phase 2.
      </p>

      <button className="btn ghost danger" onClick={() => void forget()}>Forget this desktop</button>
    </div>
  );
}
