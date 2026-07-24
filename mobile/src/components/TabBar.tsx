export type Tab = "ask" | "db";

// Persistent bottom tabs. Both tabs stay mounted behind this (see App.tsx), so
// switching keeps a half-typed question and a scroll position intact.
export default function TabBar({
  tab,
  onTab,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
}) {
  return (
    <nav className="tabbar">
      <button
        className={"tab" + (tab === "ask" ? " on" : "")}
        onClick={() => onTab("ask")}
        aria-current={tab === "ask"}
      >
        <span className="tab-mark">✦</span>
        Ask
      </button>
      <button
        className={"tab" + (tab === "db" ? " on" : "")}
        onClick={() => onTab("db")}
        aria-current={tab === "db"}
      >
        <span className="tab-mark">▤</span>
        Database
      </button>
    </nav>
  );
}
