import { useEffect, useState } from "react";
import { health, setConnection, type Health } from "../lib/client";
import { loadConnection, saveConnection } from "../lib/store";

/** Bare host like "192.168.1.75:8756" or "desktop.tailnet.ts.net:8756" becomes
 * a full http base. Plaintext http is intended — the LAN link is plain, and
 * Tailscale encrypts at the network layer. */
function normalizeHost(input: string): string {
  const h = input.trim();
  if (!h) return "";
  const withScheme = /^https?:\/\//i.test(h) ? h : `http://${h}`;
  return withScheme.replace(/\/+$/, "");
}

type Status = "idle" | "testing" | "ok" | "error";

export default function Connect() {
  const [host, setHost] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [result, setResult] = useState<Health | null>(null);
  const [err, setErr] = useState("");

  // Reconnect to the last desktop on launch.
  useEffect(() => {
    loadConnection().then((c) => {
      if (c?.host) {
        setHost(c.host);
        void test(c.host);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function test(raw: string): Promise<void> {
    const base = normalizeHost(raw);
    if (!base) return;
    setStatus("testing");
    setErr("");
    setResult(null);
    try {
      const h = await health(base);
      setResult(h);
      setStatus("ok");
      setConnection(base);
      await saveConnection({
        host: base,
        token: "", // Phase 1 fills this in from pairing
        serverId: h.server_id ?? "",
        serverName: h.server_name ?? "",
      });
    } catch (e) {
      setStatus("error");
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="screen">
      <header className="hd">
        <span className="mark">✦</span>
        <div>
          <h1>Connect to your desktop</h1>
          <p className="sub">Enter the address your PC shows under Settings → Companion devices.</p>
        </div>
      </header>

      <label className="field">
        <span className="lbl">Desktop address</span>
        <input
          className="in"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="192.168.1.75:8756"
          value={host}
          onChange={(e) => setHost(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void test(host); }}
        />
      </label>

      <button
        className="btn"
        onClick={() => void test(host)}
        disabled={status === "testing" || !host.trim()}
      >
        {status === "testing" ? "Testing…" : "Test connection"}
      </button>

      {status === "ok" && result && (
        <div className="card ok">
          <span className="pill">Reachable</span>
          <p className="big">{result.server_name || "Desktop"}</p>
          <p className="mono">{result.app ?? "Aether Intelligence"}</p>
          {result.server_id && <p className="mono dim">id {result.server_id.slice(0, 12)}…</p>}
        </div>
      )}
      {status === "error" && (
        <div className="card err">
          <span className="pill">Can’t reach it</span>
          <p>{err}</p>
          <p className="dim">Is the desktop app open, on the same network, and is companion access turned on?</p>
        </div>
      )}

      <p className="foot">
        Phase 0 scaffold — manual address + liveness check. Scanning the pairing QR and
        completing the token handshake land in Phase 1.
      </p>
    </div>
  );
}
