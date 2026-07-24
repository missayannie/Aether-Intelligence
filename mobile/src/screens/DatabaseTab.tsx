import { useState } from "react";
import Database from "./Database";
import DbList from "./DbList";
import DbRecord from "./DbRecord";
import type { Kind } from "../lib/db";

// The Database tab's navigation stack. Kept here rather than in App.tsx so the
// tab owns its own history: switching to Ask and back returns you exactly where
// you were, several records deep if that's where you left off.
//
// Records push onto the stack (a cross-reference tap opens another record), so
// Back walks the chain in reverse rather than jumping to the root.
type Entry =
  | { level: "kinds" }
  | { level: "list"; kind: Kind }
  | { level: "record"; kind: string; id: string | number; name: string };

export default function DatabaseTab() {
  const [stack, setStack] = useState<Entry[]>([{ level: "kinds" }]);
  const top = stack[stack.length - 1];

  const push = (e: Entry) => setStack((s) => [...s, e]);
  const pop = () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s));
  const openRecord = (kind: string, id: string | number, name: string) =>
    push({ level: "record", kind, id, name });

  if (top.level === "record") {
    return (
      <DbRecord
        kind={top.kind}
        id={top.id}
        name={top.name}
        onBack={pop}
        onOpenRecord={openRecord}
      />
    );
  }
  if (top.level === "list") {
    return <DbList kind={top.kind} onBack={pop} onOpenRecord={openRecord} />;
  }
  return (
    <Database
      onOpenKind={(k) => push({ level: "list", kind: k })}
      onOpenRecord={openRecord}
    />
  );
}
