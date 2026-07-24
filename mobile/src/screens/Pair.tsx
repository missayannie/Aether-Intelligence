import { useState } from "react";
import { Capacitor } from "@capacitor/core";
import { BarcodeScanner } from "@capacitor-mlkit/barcode-scanning";
import { claimFromUri, type Claimed } from "../lib/pairing";

// Native QR scan. Returns the QR's text (an `aether://pair?…` link), which goes
// straight to claimFromUri — the exact same path the pasted link takes below.
// On the web target there's no camera, so we fall through to the paste field.
async function scanQr(): Promise<string | null> {
  if (!Capacitor.isNativePlatform()) return null;
  const perm = await BarcodeScanner.requestPermissions();
  if (perm.camera !== "granted" && perm.camera !== "limited") return null;
  const { barcodes } = await BarcodeScanner.scan();
  return barcodes[0]?.rawValue ?? null;
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
    setErr("");
    try {
      const text = await scanQr();
      if (text) { void pair(text); return; }
      // No text: camera denied, nothing scanned, or the web target (no camera).
      setErr(
        Capacitor.isNativePlatform()
          ? "Nothing scanned. Allow camera access in Settings, or paste the link below."
          : "Camera scanning is on the installed app — for now paste the link below.",
      );
    } catch {
      // Scanner dismissed or unavailable — the paste field is always the fallback.
      setErr("Couldn't open the camera. Paste the pairing link below instead.");
    }
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
