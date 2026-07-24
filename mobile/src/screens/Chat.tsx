import { useEffect, useRef, useState } from "react";
import { createChat, defaultModel, streamChat, type Auth, type ChatEvent } from "../lib/client";

type Source = { label: string; url: string };
type Msg = { role: "user" | "assistant"; content: string; sources?: Source[] };

export default function Chat({ serverName, onBack }: { serverName: string; onBack: () => void }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [err, setErr] = useState("");

  const chatId = useRef<string | null>(null);
  const model = useRef<{ id: string; auth: Auth } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Warm the model choice up front so the first send is instant.
  useEffect(() => {
    defaultModel().then((m) => { model.current = m; }).catch((e) => setErr(String(e)));
  }, []);

  // Keep the newest message in view as tokens stream in.
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [msgs]);

  function onEvent(e: ChatEvent) {
    if (e.type === "error") { setErr(e.message); return; }
    if (e.type !== "token" && e.type !== "source") return;
    setMsgs((m) => {
      const copy = m.slice();
      const i = copy.length - 1;
      if (i < 0 || copy[i].role !== "assistant") return m;
      const last = { ...copy[i] };
      if (e.type === "token") last.content += e.text;
      else last.sources = [...(last.sources ?? []), { label: e.label, url: e.url }];
      copy[i] = last;
      return copy;
    });
  }

  async function send() {
    const q = input.trim();
    if (!q || streaming) return;
    setErr("");
    setInput("");
    try {
      if (!chatId.current) chatId.current = await createChat();
      if (!model.current) model.current = await defaultModel();
    } catch (e) {
      setErr(`Couldn't start a chat: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }

    setMsgs((m) => [...m, { role: "user", content: q }, { role: "assistant", content: "" }]);
    setStreaming(true);
    abort.current = new AbortController();
    try {
      await streamChat(
        { chatId: chatId.current, model: model.current.id, auth: model.current.auth, message: q },
        onEvent,
        abort.current.signal,
      );
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setStreaming(false);
      abort.current = null;
    }
  }

  return (
    <div className="chat">
      <header className="chat-head">
        <button className="icon-btn" onClick={onBack} aria-label="Back">‹</button>
        <div className="chat-title">
          <span className="dot-on" /> {serverName || "Desktop"}
        </div>
      </header>

      <div className="msgs" ref={listRef}>
        {msgs.length === 0 && (
          <p className="empty">Ask about mechanics, gear, prices, lore — your desktop answers.</p>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={"msg " + m.role}>
            <div className="bubble">
              {m.content || (m.role === "assistant" && streaming && i === msgs.length - 1 ? "…" : "")}
            </div>
            {m.sources && m.sources.length > 0 && (
              <div className="msg-sources">
                {m.sources.map((s, j) => (
                  <a key={j} href={s.url} target="_blank" rel="noreferrer">{s.label}</a>
                ))}
              </div>
            )}
          </div>
        ))}
        {err && <div className="chat-err">{err}</div>}
      </div>

      <div className="composer">
        <textarea
          className="in"
          rows={1}
          placeholder="Ask a question…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); }
          }}
        />
        {streaming ? (
          <button className="send stop" onClick={() => abort.current?.abort()} aria-label="Stop">■</button>
        ) : (
          <button className="send" onClick={() => void send()} disabled={!input.trim()} aria-label="Send">↑</button>
        )}
      </div>
    </div>
  );
}
