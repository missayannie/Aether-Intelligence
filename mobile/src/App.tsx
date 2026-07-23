import Connect from "./screens/Connect";

// Phase 0 is a single screen. Phase 1 adds Pair (QR scan) and Phase 2 adds Ask
// (chat), at which point this becomes a small tab/stack shell.
export default function App() {
  return <Connect />;
}
