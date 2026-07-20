// WYSIWYG markdown editor: shows formatted text (for users who don't know
// markdown) but stores markdown. contentEditable + execCommand for editing,
// `marked` to load markdown -> HTML, `turndown` to save HTML -> markdown.
import { useEffect, useRef, useState } from "react";
import { marked } from "marked";
import TurndownService from "turndown";
// Turndown has NO table support of its own: without this plugin it flattens a
// <table> into a bare vertical list of its cell values. Since this editor saves on
// every input (and on every checkbox tick), a reference table would be destroyed the
// first time the player touched the doc. The plugin is what makes tables survivable.
import { gfm } from "turndown-plugin-gfm";
import { api } from "./api";

// Treat a single newline as a line break, matching the chat renderer (which uses
// remark-breaks). Without this the assistant's "**do this.**\n*because…*" collapses
// onto one line here but breaks in chat — the same doc would read differently in the
// two places.
marked.setOptions({ breaks: true });

const td = new TurndownService({
  headingStyle: "atx",       // ## Heading (not underline style)
  bulletListMarker: "-",
  codeBlockStyle: "fenced",
});
td.use(gfm);                 // tables + strikethrough survive a save

// Images the model embedded as `![alt](asset:NAME)` are rewritten to a real URL for
// display (see hydrateAssets); this turns them back into the asset: form on save, so
// the doc keeps a portable reference instead of a machine-specific localhost URL.
td.addRule("assetImages", {
  filter: (node) =>
    node.nodeName === "IMG" && !!(node as HTMLElement).getAttribute("data-asset"),
  replacement: (_content, node) => {
    const el = node as HTMLElement;
    return `![${el.getAttribute("alt") || ""}](asset:${el.getAttribute("data-asset")})`;
  },
});

/** Point `asset:NAME` images at this chat's real asset URL so they render inline in
 *  the editor, remembering the original name in data-asset for the save rule above. */
function hydrateAssets(root: HTMLElement, chatId?: string): void {
  root.querySelectorAll("img").forEach((img) => {
    const src = img.getAttribute("src") || "";
    if (!src.startsWith("asset:")) return;
    const name = src.slice(6);
    img.setAttribute("data-asset", name);
    if (chatId) img.setAttribute("src", api.assetUrl(chatId, name));
  });
}

/** marked renders task-list checkboxes `disabled`, so they can't be ticked. Enable
 *  them, and mark them non-editable so the surrounding contentEditable treats each as
 *  an atomic widget (a click toggles it instead of placing a caret). */
function hydrateCheckboxes(root: HTMLElement): void {
  root.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.removeAttribute("disabled");
    cb.setAttribute("contenteditable", "false");
  });
}

/** Mirror each checkbox's `checked` PROPERTY into its attribute.
 *  Saving reads innerHTML, which serializes attributes — not live properties — so
 *  without this every tick the user makes would be dropped on the way to markdown. */
function syncCheckboxAttrs(root: HTMLElement): void {
  root.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    const box = cb as HTMLInputElement;
    if (box.checked) box.setAttribute("checked", "");
    else box.removeAttribute("checked");
  });
}

// Serialize checkboxes back to markdown. Two forms, because context decides which
// one survives a reload:
//   - in a LIST item -> GFM "- [x] " / "- [ ] " (what marked parses back to a box)
//   - in a TABLE cell -> raw <input>, because GFM task-list syntax is only defined
//     for list items; "[x]" in a cell would reload as the literal text "[x]".
// Without this rule turndown drops the <input> entirely and the checkbox is lost.
td.addRule("taskListItems", {
  filter: (node) =>
    node.nodeName === "INPUT" && (node as HTMLInputElement).type === "checkbox",
  replacement: (_content, node) => {
    const box = node as HTMLInputElement;
    if (box.closest?.("td, th")) {
      return box.checked ? '<input type="checkbox" checked>' : '<input type="checkbox">';
    }
    return box.checked ? "[x] " : "[ ] ";
  },
});

export default function Editor({
  docKey, markdown, chatId, title, shared, onChange, onTitleChange, onSharedChange,
  onBlur, onSave, pinnedToOverlay, onPinToOverlay,
  onImageClick, onAsk, askBusy, askStatus, thread, onClearThread,
}: {
  docKey: string;                 // changes when the target doc/note changes
  markdown: string;
  chatId?: string;                // resolves `asset:` images to this chat's assets
  title?: string;
  shared?: boolean;               // findable from your other character profiles
  onChange: (md: string) => void;
  onTitleChange?: (title: string) => void;
  onSharedChange?: (shared: boolean) => void;
  onBlur?: () => void;
  onSave?: () => void;            // explicit save (toolbar Save button)
  pinnedToOverlay?: boolean;      // this doc's checklist shows on the overlay
  onPinToOverlay?: () => void;
  onImageClick?: (assetName: string) => void;  // expand a map/portrait full-screen
  onAsk?: (instruction: string) => void;       // ask the agent about/edit THIS doc
  askBusy?: boolean;
  askStatus?: string;
  thread?: { role: "user" | "assistant"; content: string }[];  // this doc's subchat
  onClearThread?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const askWrapRef = useRef<HTMLDivElement>(null);
  const [saved, setSaved] = useState(false);
  const [ask, setAsk] = useState("");
  const [threadOpen, setThreadOpen] = useState(false);
  // Read mode by default — the doc is something you USE while playing, and a
  // stray click shouldn't drop a caret into it. ✎ Edit switches the surface
  // to editing; a brand-new empty doc starts there since there's nothing to
  // read yet. Checkboxes stay tickable in BOTH modes (ticking is using the
  // guide, not editing it).
  const [editMode, setEditMode] = useState(false);
  useEffect(() => { setEditMode(!(markdown || "").trim()); }, [docKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Collapse the thread when switching docs — it belongs to the doc you left.
  useEffect(() => { setThreadOpen(false); }, [docKey]);

  // Clicking anywhere OUTSIDE the thread + ask box closes the thread — it
  // floats over the document, so leaving it open while editing hid the text
  // underneath. Focusing the ask box brings it (and its saved context) back.
  useEffect(() => {
    if (!threadOpen) return;
    const away = (e: PointerEvent) => {
      const wrap = askWrapRef.current;
      if (wrap && !wrap.contains(e.target as Node)) setThreadOpen(false);
    };
    document.addEventListener("pointerdown", away, true);
    return () => document.removeEventListener("pointerdown", away, true);
  }, [threadOpen]);
  // Keep the newest reply in view as it lands.
  useEffect(() => {
    threadRef.current?.scrollTo(0, threadRef.current.scrollHeight);
  }, [thread, askBusy]);

  // The markdown this surface last loaded or produced. Lets the [markdown]
  // effect below tell an ECHO of our own onChange (same string coming back as
  // a prop — ignore, or typing would drop the caret) from an EXTERNAL change
  // (the doc-thread agent's update_doc — must re-render, or the open tab keeps
  // showing the old doc and its next emit() writes the stale DOM back over
  // the agent's edit).
  const lastMd = useRef<string>("");

  const loadDom = () => {
    if (!ref.current) return;
    ref.current.innerHTML = marked.parse(markdown || "") as string;
    lastMd.current = markdown || "";
    hydrateAssets(ref.current, chatId);  // make embedded maps/portraits actually show
    hydrateCheckboxes(ref.current);      // make checklist boxes tickable
  };

  // Load HTML when the target changes — never mid-typing (would drop the caret).
  useEffect(() => { loadDom(); }, [docKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // …and when the CONTENT changes under us (agent edit landing in the open tab).
  useEffect(() => {
    if ((markdown || "") !== lastMd.current) loadDom();
  }, [markdown]); // eslint-disable-line react-hooks/exhaustive-deps

  const emit = () => {
    if (!ref.current) return;
    syncCheckboxAttrs(ref.current);
    const md = td.turndown(ref.current.innerHTML);
    lastMd.current = md;
    onChange(md);
  };

  // Clicks inside the editable area. Two widgets behave as widgets rather than text:
  //   - checkboxes toggle (we cancel the webview's own toggle and flip it ourselves,
  //     so the result is deterministic inside contentEditable), then save
  //   - asset images expand full-screen, since a map pinned into a guide is
  //     unreadable at the inline size
  const onAreaClick = (e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    if (t instanceof HTMLInputElement && t.type === "checkbox") {
      e.preventDefault();
      t.checked = !t.checked;
      emit();
      return;
    }
    const asset = t instanceof HTMLImageElement ? t.getAttribute("data-asset") : null;
    if (asset && onImageClick) {
      e.preventDefault();     // don't drop a caret next to the image
      onImageClick(asset);
    }
  };
  const exec = (cmd: string, arg?: string) => {
    ref.current?.focus();
    document.execCommand(cmd, false, arg);
    emit();
  };
  const block = (tag: string) => exec("formatBlock", tag);
  const link = () => {
    const url = window.prompt("Link URL:");
    if (url) exec("createLink", url);
  };
  // Turn the current line into a checklist item: make it a bulleted list, then
  // drop a checkbox at the caret. The CSS hides the bullet for any <li> holding a
  // checkbox, so it reads as a clean "☐ item" (no bullet), and turndown's rule
  // above saves it as "- [ ] item".
  const checklist = () => {
    ref.current?.focus();
    document.execCommand("insertUnorderedList");
    document.execCommand("insertHTML", false, '<input type="checkbox"> ');
    emit();
  };
  const save = () => {
    emit();
    onSave?.();
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1500);
  };

  // Keep the text selection while clicking a toolbar button.
  const keep = (e: React.MouseEvent) => e.preventDefault();
  const Btn = (p: { t: string; on: () => void; children: React.ReactNode }) => (
    <button title={p.t} onMouseDown={keep} onClick={p.on}>{p.children}</button>
  );

  return (
    <div className="editor">
      {onTitleChange && (
        <input
          className="editor-title"
          value={title || ""}
          placeholder="Untitled"
          aria-label="Title"
          readOnly={!editMode}
          onChange={(e) => onTitleChange(e.target.value)}
          onBlur={onBlur}
        />
      )}
      <div className="editor-toolbar">
        {editMode && (
          <>
            <Btn t="Heading 1" on={() => block("h1")}>H1</Btn>
            <Btn t="Heading 2" on={() => block("h2")}>H2</Btn>
            <Btn t="Heading 3" on={() => block("h3")}>H3</Btn>
            <Btn t="Normal text" on={() => block("p")}>¶</Btn>
            <span className="tb-sep" />
            <Btn t="Bold (Ctrl+B)" on={() => exec("bold")}><b>B</b></Btn>
            <Btn t="Italic (Ctrl+I)" on={() => exec("italic")}><i>I</i></Btn>
            <span className="tb-sep" />
            <Btn t="Bulleted list" on={() => exec("insertUnorderedList")}>• List</Btn>
            <Btn t="Numbered list" on={() => exec("insertOrderedList")}>1. List</Btn>
            <Btn t="Checklist" on={checklist}>☑ List</Btn>
            <Btn t="Quote" on={() => block("blockquote")}>❝</Btn>
            <Btn t="Code block" on={() => block("pre")}>{"</>"}</Btn>
            <span className="tb-sep" />
            <Btn t="Insert link" on={link}>🔗</Btn>
            <Btn t="Clear formatting" on={() => exec("removeFormat")}>Clear</Btn>
          </>
        )}
        {!editMode && <span className="tb-reading muted">Reading</span>}
        {onSharedChange && (
          <label
            className="tb-share"
            title="Share across your profiles — it stays in this chat, but stays findable from any profile"
          >
            <input
              type="checkbox"
              checked={!!shared}
              onChange={(e) => onSharedChange(e.target.checked)}
            />
            Shared
          </label>
        )}
        {onPinToOverlay && (
          <button
            className={"tb-edit" + (pinnedToOverlay ? " on" : "")}
            title={pinnedToOverlay
              ? "Showing on the in-game overlay — click to stop"
              : "Show this doc's checklist on the in-game overlay"}
            onMouseDown={keep}
            onClick={onPinToOverlay}
          >
            {pinnedToOverlay ? "✦ On overlay" : "✦ Overlay"}
          </button>
        )}
        <button
          className={"tb-edit" + (editMode ? " on" : "")}
          title={editMode ? "Done editing (saves & switches back to reading)" : "Edit this doc"}
          onMouseDown={keep}
          onClick={() => {
            if (editMode) save();   // leaving edit mode saves (and marks final)
            setEditMode(!editMode);
          }}
        >
          ✎ Edit
        </button>
        {editMode && (
          <button
            className="tb-save"
            title="Save this doc (marks it final)"
            onMouseDown={keep}
            onClick={save}
          >
            {saved ? "Saved ✓" : "Save"}
          </button>
        )}
      </div>
      <div
        ref={ref}
        className={"editor-area md" + (editMode ? "" : " reading")}
        contentEditable={editMode}
        suppressContentEditableWarning
        onInput={emit}
        onClick={onAreaClick}
        onBlur={onBlur}
      />

      {/* The doc's own side thread, Gemini-style: a narrow pill at the foot of the
          document, with the conversation floating ABOVE it rather than taking a
          column. The thread is scoped to this doc and saved as a subchat, so closing
          the panel and reopening it brings the context back. */}
      {onAsk && (
        <div className="doc-ask-wrap" ref={askWrapRef}>
          {threadOpen && !!thread?.length && (
            <div className="doc-thread">
              <div className="doc-thread-head">
                <span className="doc-thread-title">✦ About this doc</span>
                {onClearThread && (
                  <button className="doc-thread-x" title="Clear this thread"
                          onClick={onClearThread}>Clear</button>
                )}
                <button className="doc-thread-x" title="Close"
                        onClick={() => setThreadOpen(false)}>✕</button>
              </div>
              <div className="doc-thread-body" ref={threadRef}>
                {thread.map((m, i) => (
                  <div key={i} className={"doc-msg " + m.role}>{m.content}</div>
                ))}
                {askBusy && <div className="doc-msg assistant busy">{askStatus || "Working…"}</div>}
              </div>
            </div>
          )}

          <form
            className={"doc-ask" + (askBusy ? " busy" : "")}
            onSubmit={(e) => {
              e.preventDefault();
              const v = ask.trim();
              if (!v || askBusy) return;
              setThreadOpen(true);
              onAsk(v);
              setAsk("");
            }}
          >
            <span className="doc-ask-ico">✦</span>
            <input
              className="doc-ask-input"
              value={ask}
              placeholder={askBusy ? (askStatus || "Working…") : "Ask about this doc…"}
              aria-label="Ask the assistant about this document"
              onChange={(e) => setAsk(e.target.value)}
              // Clicking the box reopens the thread, so the context is one tap away.
              onFocus={() => { if (thread?.length) setThreadOpen(true); }}
            />
            {!!thread?.length && (
              <button
                type="button"
                className="doc-ask-count"
                title={threadOpen ? "Hide the thread" : "Show the thread"}
                onClick={() => setThreadOpen((o) => !o)}
              >
                {Math.ceil(thread.length / 2)}
              </button>
            )}
            <button className="doc-ask-send" type="submit" disabled={askBusy || !ask.trim()}
                    title="Send">
              {askBusy ? "…" : "↑"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
