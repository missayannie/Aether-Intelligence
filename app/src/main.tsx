import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Overlay from "./overlay/Overlay";

// The overlay window renders this same bundle. Primary signal is the
// ?overlay=1 query the Rust side puts on the window URL; the window label is
// the fallback in case a packaging mode strips the query. In plain-web dev you
// can preview the stub at http://localhost:1420/?overlay=1.
function isOverlayWindow(): boolean {
  if (new URLSearchParams(window.location.search).has("overlay")) return true;
  try {
    const internals = (window as unknown as Record<string, any>).__TAURI_INTERNALS__;
    return internals?.metadata?.currentWebview?.label === "overlay";
  } catch {
    return false;
  }
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {isOverlayWindow() ? <Overlay /> : <App />}
  </React.StrictMode>,
);
