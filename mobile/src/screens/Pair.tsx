import { useState } from "react";
import { claimFromUri, type Claimed } from "../lib/pairing";

// NATIVE HOOK (Mac): replace scan() with @capacitor-mlkit/barcode-scanning.
// It returns the QR's text (an `aether://pair?…` link), which you hand straight
// to claimFromUri — the exact same path the pasted link takes below.
async function scanQr(): Promise<string | null> {
  return null; // web/dev: no camera — use the paste field
}

export default function Pair({ onPaired }: { onPaired: (c: Claimed) => void }) {
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function pair(uri: string) {
    const trimmed = uri.trim();
    if (!trimmed) return;
    setBusy(true);
    setErr("");
    try {
      const c = await claimFromUri(trimmed);
      if (!c) throw new Error("That doesn't look like a pairing link.");
      onPaired(c);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onScan() {
    const text = await scanQr();
    if (text) void pair(text);
    else setErr("Camera scanning is on the installed app — for now paste the link below.");
  }

  return (
    <div className="screen">
      <header className="hd">
        <span className="mark">✦</span>
        <div>
          <h1>Pair with your desktop</h1>
          <p className="sub">On your PC: Settings → Companion devices → Pair a device. Scan the QR, or paste its link.</p>
        </div>
      </header>

      <button className="btn" onClick={onScan} disabled={busy}>Scan QR code</button>

      <label className="field">
        <span className="lbl">…or paste the pairing link</span>
        <textarea
          className="in link-in"
          rows={3}
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="aether://pair?v=1&hosts=…&code=…"
          value={link}
          onChange={(e) => setLink(e.target.value)}
        />
      </label>
      <button className="btn ghost" onClick={() => void pair(link)} disabled={busy || !link.trim()}>
        {busy ? "Pairing…" : "Pair"}
      </button>

      {err && (
        <div className="card err">
          <span className="pill">Not paired</span>
          <p>{err}</p>
        </div>
      )}

      <p className="foot">
        The code is single-use and expires in two minutes — if it fails, generate a fresh one on the desktop.
      </p>
    </div>
  );
}
