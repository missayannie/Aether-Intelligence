import type { CapacitorConfig } from "@capacitor/cli";

// The web assets are bundled INTO the app (webDir), not served from a URL — the
// companion has no cloud host by design. It talks to your desktop's backend at
// runtime over the LAN / Tailscale, configured after pairing.
const config: CapacitorConfig = {
  appId: "com.ffxivguide.companion",
  appName: "Aether Companion",
  webDir: "dist",
  ios: {
    // Lets the WKWebView make plaintext HTTP calls to a LAN / 100.x address.
    // (Also add NSAllowsLocalNetworking to Info.plist — see README.)
    limitsNavigationsToAppBoundDomains: false,
  },
};

export default config;
