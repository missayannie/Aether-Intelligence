import { useEffect, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
// Render a single newline as a line break. Markdown normally folds one into a space,
// which is why an italic reason written on its own line still hugged the bold
// instruction above it. The assistant writes "**do this.**\n*because…*" — this is
// what makes that second line actually land on a second line.
import remarkBreaks from "remark-breaks";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  api,
  type Model,
  type ChatSummary,
  type Message,
  type Auth,
  type SubStatus,
  type Attachment,
  type DocItem,
  type Workspace,
  type CharacterHit,
  type BoundCharacter,
  type SearchHit,
  type DbKind,
  type DbHit,
  type DbItem,
  type DbDetailDoc,
  type DbBrowse,
  type SourceInfo,
  matchSource,
} from "./api";

// Database sections, mirroring Garland's own navigation. The icon is the fastest way
// to tell the type of a row apart at a glance — the same reason Garland uses them.
// What the Map tab shows when the player has never opened a zone — a capital
// everyone recognises, rather than an empty picker.
const DEFAULT_MAP_ZONE = "Ul'dah - Steps of Nald";

const DB_KINDS: { id: DbKind; label: string; icon: string }[] = [
  { id: "all", label: "All", icon: "🔎" },
  { id: "item", label: "Items", icon: "🎁" },
  { id: "instance", label: "Duty", icon: "🌀" },
  { id: "quest", label: "Quests", icon: "❗" },
  { id: "npc", label: "NPCs", icon: "💬" },
  { id: "mob", label: "Mobs", icon: "👹" },
  { id: "achievement", label: "Achievements", icon: "🏆" },
  { id: "fate", label: "FATEs", icon: "⚔️" },
  { id: "node", label: "Gathering", icon: "⛏" },
  { id: "leve", label: "Leves", icon: "📜" },
];
const kindIcon = (t: string) => DB_KINDS.find((k) => k.id === t)?.icon ?? "•";

// Overlay hotkeys: accelerator strings the Rust shell parses ("Alt+Backquote").
// Edited in Settings via HotkeyField, applied live by set_overlay_hotkeys.
type OverlayHotkeySet = { ask: string; ambient: string; kill: string; drawer: string };
const OVERLAY_HOTKEY_DEFAULTS: OverlayHotkeySet = {
  ask: "Alt+Backquote",
  // Alt+Shift+`, not Alt+Win+` — Windows reserves several Win+Alt combos.
  ambient: "Alt+Shift+Backquote",
  kill: "Alt+Backslash",
  drawer: "Alt+D",
};

// Real per-type icons — the SAME images Garland Tools' own Browse toolbar uses
// (read off their live page), so the kind selector and browse chips look like
// the site the data comes from. Emoji stay as the on-error fallback.
const DB_TYPE_ICON: Record<string, string> = {
  all: "https://garlandtools.org/db/images/site/Search.png",
  item: api.gameIcon(65002),   // the gil/coin stack — the classic "items" mark
  instance: "https://garlandtools.org/files/icons/instance/type/61802.png",
  quest: "https://garlandtools.org/files/icons/journal/61412.png",
  npc: "https://garlandtools.org/db/images/marker/Shop.png",
  mob: "https://garlandtools.org/db/images/Mob.png",
  achievement: "https://garlandtools.org/files/icons/achievement/3773.png",
  fate: "https://garlandtools.org/files/icons/fate/Unknown.png",
  node: "https://garlandtools.org/files/icons/job//MIN.png",
  leve: "https://garlandtools.org/db/images/marker/Leve.png",
  patch: "https://garlandtools.org/db/images/LatestPatch.png",
  action: "https://garlandtools.org/files/icons/action/103.png",
  status: "https://garlandtools.org/files/icons/status/10101.png",
  fishing: "https://garlandtools.org/files/icons/job//FSH.png",
};

/** A DB type's icon: Garland's real image, falling back to the emoji.
 *
 *  Error state RESETS when the id changes — the collapsed KindSelect chip is one
 *  instance reused across selections, and a single failed load (e.g. the Items
 *  icon requested at boot before the backend sidecar was listening) used to
 *  stick it on the emoji forever. Failures also retry with growing delays for
 *  exactly that boot race. */
function TypeIcon({ id, emoji }: { id: string; emoji: string }) {
  const url = DB_TYPE_ICON[id];
  const [tries, setTries] = useState(0);
  const [err, setErr] = useState(false);
  useEffect(() => { setTries(0); setErr(false); }, [url]);
  if (!url || err) return <span className="db-type-emoji">{emoji}</span>;
  const src = tries ? `${url}${url.includes("?") ? "&" : "?"}rty=${tries}` : url;
  // No loading="lazy": these are tiny, always-visible chrome. Lazy loading only
  // adds a scheduler dependency that can defer them indefinitely.
  return (
    <img className="db-type-icon" src={src} alt=""
         onError={() => {
           if (tries < 3) window.setTimeout(() => setTries(tries + 1), 1200 * (tries + 1));
           else setErr(true);
         }} />
  );
}

/** The search bar's kind picker — a custom dropdown, because a native <select>
 *  can't render the type icons. */
function KindSelect({ value, onChange }: { value: DbKind; onChange: (k: DbKind) => void }) {
  const [open, setOpen] = useState(false);
  const cur = DB_KINDS.find((k) => k.id === value) ?? DB_KINDS[0];
  return (
    <div className="db-kind-wrap">
      <button type="button" className="db-kind" onClick={() => setOpen((o) => !o)}>
        <TypeIcon id={cur.id} emoji={cur.icon} /> {cur.label}
        <span className="db-kind-caret">▾</span>
      </button>
      {open && (
        <>
          <div className="db-kind-veil" onClick={() => setOpen(false)} />
          <div className="db-kind-pop">
            {DB_KINDS.map((k) => (
              <button key={k.id} type="button"
                      className={"db-kind-opt" + (k.id === value ? " on" : "")}
                      onClick={() => { onChange(k.id); setOpen(false); }}>
                <TypeIcon id={k.id} emoji={k.icon} /> {k.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/** img onError for remote (Garland-hosted) art: retry once with a cache-buster
 *  — their CDN hiccups are usually one-off — then hide rather than show a
 *  broken-image glyph. */
function retryImg(e: React.SyntheticEvent<HTMLImageElement>) {
  const el = e.currentTarget;
  if (!el.dataset.rty) {
    el.dataset.rty = "1";
    el.src = el.src.split("?")[0] + "?rty=1";
  } else {
    el.style.display = "none";
  }
}

// The Browse tool's catalogue kinds, in Garland's own toolbar order. Wider than
// DB_KINDS: browse covers types search doesn't (patches, actions, statuses,
// fishing spots).
const BROWSE_UI: { id: string; label: string; icon: string }[] = [
  { id: "item", label: "Items", icon: "🎁" },
  { id: "patch", label: "Patches", icon: "🧩" },
  { id: "action", label: "Actions", icon: "💫" },
  { id: "status", label: "Status Effects", icon: "✨" },
  { id: "achievement", label: "Achievements", icon: "🏆" },
  { id: "instance", label: "Instances", icon: "🌀" },
  { id: "quest", label: "Quests", icon: "❗" },
  { id: "fate", label: "FATEs", icon: "⚔️" },
  { id: "leve", label: "Leves", icon: "📜" },
  { id: "node", label: "Gathering Nodes", icon: "⛏" },
  { id: "fishing", label: "Fishing Spots", icon: "🎣" },
  { id: "npc", label: "NPCs", icon: "💬" },
  { id: "mob", label: "Mobs", icon: "👹" },
];

// The Lodestone's bot-challenge fires in bursts and is not about volume, so the fix
// is to try again rather than to back off for long.
const WAF_HINT =
  "The database couldn’t be reached just now. It’s usually momentary — try again.";
import AnnotationEditor from "./AnnotationEditor";
import Editor from "./Editor";
import GameMap, { MapNavBar } from "./GameMap";
import BrowserPane, { type BrowserReq } from "./BrowserPane";
import type { OverlayWatch, UpdateInfo, UpdateStatus, ZoneMap } from "./api";
import "./App.css";

// Render assistant text as formatted markdown (headings, lists, bold, links,
// tables, code). External links open in the in-app BROWSER TAB in the right
// pane (never inside the app webview itself, which would navigate away from the app).
// When `chatId` is given, images with an `asset:<name>` src resolve to that
// chat's local asset, so the model can drop an NPC portrait / map pin inline in
// its answer. When `onToggleTask` is given, markdown task-list checkboxes
// ('- [ ] item') become interactive.
// The hover chip on an agent-shown (temporary) chat image. `added` comes from
// the shelf itself (rt.assets), so the label is right even across re-renders,
// chat switches, and restarts — local state only covers the in-flight click.
function KeepAssetBtn({ onKeep, added }: { onKeep: () => Promise<void>; added: boolean }) {
  const [busy, setBusy] = useState(false);
  if (added) {
    return <button className="msg-keep done" disabled>✓ Added</button>;
  }
  return (
    <button
      className="msg-keep"
      title="Save this image to this chat's Assets"
      disabled={busy}
      onClick={async (e) => {
        e.stopPropagation();
        setBusy(true);
        try { await onKeep(); } finally { setBusy(false); }
      }}
    >
      {busy ? "…" : "＋ Add to Assets"}
    </button>
  );
}

function Markdown({ text, chatId, onToggleTask, onImageClick, onKeepAsset, shelf }: {
  text: string;
  chatId?: string;
  onToggleTask?: (index: number) => void;
  onImageClick?: (assetName: string) => void;
  onKeepAsset?: (assetName: string) => Promise<void>;
  shelf?: string[];   // the chat's Assets shelf — drives the ✓ Added label
}) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        // Raw HTML renders (the guide docs put `<input type="checkbox">` in
        // table cells — GFM task syntax doesn't exist there, and the full doc
        // editor already renders the same HTML via `marked`). Script-capable
        // elements are nulled in `components` below.
        rehypePlugins={[rehypeRaw]}
        // Keep the app's own schemes (react-markdown's default sanitizer would
        // strip them): `asset:<name>` for inline images, `map:<zone>` for links
        // that open a zone on the interactive Map tab, `icon:<name>` for the
        // game's own symbols at text size.
        urlTransform={(url) =>
          url.startsWith("asset:") || url.startsWith("map:") || url.startsWith("icon:")
            ? url : defaultUrlTransform(url)}
        components={{
          // rehype-raw lets model-written HTML through; these must never run.
          script: () => null,
          style: () => null,
          iframe: () => null,
          object: () => null,
          embed: () => null,
          // A paragraph that holds only image(s) is unwrapped to an inline row, so
          // consecutive inline images (e.g. a map pin + an NPC portrait) sit SIDE BY
          // SIDE and wrap, instead of each taking a full-width block.
          p: ({ node, children }) => {
            const kids: any[] = (node as any)?.children ?? [];
            const onlyImages =
              kids.length > 0 &&
              kids.every(
                (c: any) =>
                  (c.type === "element" && c.tagName === "img") ||
                  (c.type === "text" && !String(c.value).trim()),
              );
            return onlyImages ? <span className="msg-imgrow">{children}</span> : <p>{children}</p>;
          },
          // Opening in the default browser is handled globally (see the delegated
          // link handler in App), so every anchor everywhere behaves the same.
          a: ({ href, children }) => <a href={href}>{children}</a>,
          img: ({ src, alt }) => {
            const s = typeof src === "string" ? src : "";
            // `icon:<name>`: a game symbol inline at text size — the agent's
            // icon vocabulary (aetheryte, mining, fate…), served by the backend.
            if (s.startsWith("icon:")) {
              const iname = s.slice(5).trim();
              return (
                <img className="md-icon" src={api.iconByName(iname)} alt={alt || iname}
                     title={alt || iname}
                     onError={(e) => { e.currentTarget.style.display = "none"; }} />
              );
            }
            // Models sometimes write a map link as IMAGE markdown
            // (`![Zone — pins](<map:...>)`) — render the link it was meant to
            // be instead of a broken <img>. The delegated link handler in App
            // opens map: anchors on the Map tab like any other map link.
            if (s.startsWith("map:")) {
              return <a href={s}>{alt || decodeURIComponent(s.slice(4).split("?")[0])}</a>;
            }
            const isAsset = s.startsWith("asset:");
            const name = isAsset ? s.slice(6) : "";
            const real = isAsset && chatId ? api.assetUrl(chatId, name) : s;
            const img = (
              <img
                className="msg-img"
                src={real}
                alt={alt || ""}
                title={isAsset && onImageClick ? "Click to enlarge" : undefined}
                onClick={isAsset && onImageClick ? () => onImageClick(name) : undefined}
              />
            );
            // Agent-shown images are temporary (tmp_*): they carry a hover
            // button that promotes them to the Assets shelf — the player
            // chooses what's worth keeping, nothing auto-saves.
            if (isAsset && onKeepAsset && name.startsWith("tmp_")) {
              const promoted = name.replace(/^tmp_/, "");
              return (
                <span className="msg-imgwrap">
                  {img}
                  <KeepAssetBtn
                    key={name}
                    added={!!shelf?.includes(promoted)}
                    onKeep={() => onKeepAsset(name)}
                  />
                </span>
              );
            }
            return img;
          },
          input: (props: any) => {
            if (props.type === "checkbox" && onToggleTask) {
              return (
                <input
                  type="checkbox"
                  checked={!!props.checked}
                  className="md-task"
                  // The checkbox's index is computed from the DOM at CLICK time
                  // (its position among this block's checkboxes = its position
                  // in the markdown source). A counter incremented during
                  // render drifted whenever React re-rendered the tree — a
                  // tick then flipped the WRONG row's checkbox.
                  onChange={(e) => {
                    const root = e.currentTarget.closest(".md");
                    if (!root) return;
                    const all = Array.from(
                      root.querySelectorAll('input[type="checkbox"]'));
                    const idx = all.indexOf(e.currentTarget);
                    if (idx >= 0) onToggleTask(idx);
                  }}
                />
              );
            }
            const { node, ...rest } = props;
            void node;
            return <input {...rest} readOnly />;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

// Search across docs, notes and asset names. Unchecked searches the current profile
// (plus anything marked shared anywhere — that's what sharing an item buys you);
// checked spans every profile.
function SearchModal(props: {
  activeWs: string;
  onClose: () => void;
  onOpenHit: (hit: SearchHit) => void;
}) {
  const [q, setQ] = useState("");
  const [global, setGlobal] = useState(false);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [busy, setBusy] = useState(false);

  // Debounced so typing doesn't fire a request per keystroke.
  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    let live = true;
    setBusy(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await api.search(q, props.activeWs, global ? "global" : "workspace");
        if (live) setHits(r.hits);
      } catch { if (live) setHits([]); } finally { if (live) setBusy(false); }
    }, 220);
    return () => { live = false; window.clearTimeout(t); };
  }, [q, global, props.activeWs]);

  const icon = (k: SearchHit["kind"]) => (k === "doc" ? "◆" : k === "note" ? "✎" : "🖼");

  return (
    <div className="modal-bg" onClick={props.onClose}>
      <div className="modal search-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          Search docs, notes &amp; assets
          <button className="x" onClick={props.onClose}>✕</button>
        </div>
        <input
          className="search-input"
          placeholder="Search titles and contents…"
          value={q}
          autoFocus
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && props.onClose()}
        />
        <label className="search-scope">
          <input type="checkbox" checked={global} onChange={(e) => setGlobal(e.target.checked)} />
          Search all profiles
          <span className="muted small">
            {global ? "— every profile" : "— this profile, plus anything shared"}
          </span>
        </label>
        <div className="search-results">
          {busy && <div className="muted small">Searching…</div>}
          {!busy && q.trim() && hits.length === 0 && (
            <div className="muted small">No matches.</div>
          )}
          {hits.map((h, i) => (
            <button key={h.kind + h.id + i} className="search-hit" onClick={() => props.onOpenHit(h)}>
              <span className="hit-ico">{icon(h.kind)}</span>
              <span className="hit-main">
                <span className="hit-title">
                  {h.title || "(untitled)"}
                  {h.shared && <span className="hit-shared">shared</span>}
                </span>
                {h.snippet && <span className="hit-snip">{h.snippet}</span>}
                <span className="hit-chat">in {h.chat_title}</span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// Flip the N-th markdown task-list checkbox ('- [ ] '/'- [x] ') in `md`.
function toggleTask(md: string, index: number): string {
  // Checkboxes exist in TWO source forms: GFM task lines, and raw
  // `<input type="checkbox">` HTML in table cells (the guide-doc convention —
  // GFM task syntax isn't defined there). The renderer counts every checkbox
  // it draws in document order, so flip the nth match of EITHER form.
  const re = /^([ \t]*[-*+][ \t]+)\[( |x|X)\]|<input type="checkbox"( checked)?[^>]*>/gm;
  let i = -1;
  return md.replace(re, (match, liPrefix, gfmState, rawChecked) => {
    i++;
    if (i !== index) return match;
    if (liPrefix !== undefined) {
      return liPrefix + (gfmState === " " ? "[x]" : "[ ]");
    }
    return rawChecked ? '<input type="checkbox">' : '<input type="checkbox" checked>';
  });
}

// Force the WebView compositor to paint pending DOM updates. Tauri/WebView2 on
// Windows can defer painting content that changes right after the window first
// shows until a user interaction; this triggers a composite so it appears anyway.
function nudgeRepaint() {
  const paint = () => {
    window.dispatchEvent(new Event("resize"));
    const el = document.getElementById("root") || document.body;
    const prev = el.style.transform;
    el.style.transform = "translateZ(0)";
    void el.offsetHeight; // force reflow
    el.style.transform = prev;
  };
  requestAnimationFrame(paint);
  setTimeout(paint, 60);   // React may commit a tick after the awaited loads
  setTimeout(paint, 300);
}

// Copy text to the clipboard (with a legacy fallback for older webviews).
async function copyText(t: string) {
  try {
    await navigator.clipboard.writeText(t);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    document.body.removeChild(ta);
  }
}

// Lifetime agent-API spend: one low-key muted line above ♥ Support Me, styled
// like the composer's per-chat cost readout. Total only — the tooltip carries
// the subscription-covered figure.
function UsageMeter() {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.usageSummary>> | null>(null);
  useEffect(() => {
    // Retry until the backend answers — at app launch the packaged backend
    // is still booting, and a single fetch here lost that race and left the
    // line missing for the whole session. Once live, refresh each minute so
    // the total tracks the chatting you do.
    let dead = false;
    let delay = 1000;
    const tick = () => {
      api.usageSummary()
        .then((d) => { if (!dead) { setData(d); setTimeout(tick, 60_000); } })
        .catch(() => {
          if (!dead) { delay = Math.min(delay * 2, 15_000); setTimeout(tick, delay); }
        });
    };
    tick();
    return () => { dead = true; };
  }, []);
  if (!data) return null;
  const fmt = (v: number) => (v >= 0.01 || v === 0 ? `$${v.toFixed(2)}` : "<$0.01");
  const covered = data.covered_usd > 0
    ? `\nPlus ${fmt(data.covered_usd)} of subscription turns — covered, you paid $0 for those.`
    : "";
  return (
    <div
      className="usage-line"
      title={"Total agent API usage across all chats, doc edits and follow-up suggestions." + covered}
    >
      ≈{fmt(data.billed_usd)} total API usage
    </div>
  );
}

// A list of expandable, markdown-rendered cards (used for both Docs and Notes).
// Each card collapses to a title; expanded it shows formatted markdown, or a
// textarea when editing. `promote` adds an optional extra action per card.
function CardList({
  items, onChange, onCommit, emptyText, promote, promoteLabel, onOpen, chatId,
}: {
  items: DocItem[];
  onChange: (items: DocItem[]) => void;
  onCommit: (items: DocItem[]) => void;
  emptyText: string;
  promote?: (item: DocItem) => void;
  promoteLabel?: string;
  onOpen?: (item: DocItem) => void;
  chatId?: string;   // so `asset:` images in a card preview resolve and render
}) {
  const [openId, setOpenId] = useState<string | null>(null);

  if (!items.length) return <div className="muted small">{emptyText}</div>;
  return (
    <div className="cards">
      {items.map((it) => {
        const open = openId === it.id;
        return (
          <div key={it.id} className="card">
            {/* The row itself OPENS the doc in the editor; only the caret
                toggles the inline preview underneath. */}
            <div className="card-head"
                 title={onOpen ? "Open in the editor" : undefined}
                 onClick={() => (onOpen ? onOpen(it) : setOpenId(open ? null : it.id))}>
              <span
                className="card-caret"
                title={open ? "Hide preview" : "Preview here"}
                onClick={(e) => { e.stopPropagation(); setOpenId(open ? null : it.id); }}
              >
                {open ? "▾" : "▸"}
              </span>
              <span className="card-title">{it.title || cardTitle(it.content)}</span>
              {it.draft && <span className="card-badge">Draft</span>}
              {it.shared && <span className="card-badge shared">Shared</span>}
              <span className="card-tools" onClick={(e) => e.stopPropagation()}>
                {/* Shared items stay in this chat but stay findable from your
                    other character profiles. */}
                <label className="share-toggle" title="Share across your profiles (stays in this chat, findable from any profile)">
                  <input
                    type="checkbox"
                    checked={!!it.shared}
                    onChange={(e) => {
                      const next = items.map((x) =>
                        x.id === it.id ? { ...x, shared: e.target.checked } : x);
                      onChange(next);
                      onCommit(next);
                    }}
                  />
                  Shared
                </label>
                {promote && (
                  <button onClick={() => promote(it)}>{promoteLabel}</button>
                )}
                <button
                  onClick={() => {
                    const next = items.filter((x) => x.id !== it.id);
                    onChange(next);
                    onCommit(next);
                  }}
                >
                  Delete
                </button>
              </span>
            </div>
            {open && (
                <div className="card-body">
                  <Markdown
                    text={it.content}
                    chatId={chatId}
                    onToggleTask={(idx) => {
                      const next = items.map((x) =>
                        x.id === it.id ? { ...x, content: toggleTask(x.content, idx) } : x,
                      );
                      onChange(next);
                      onCommit(next);
                    }}
                  />
                </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

type Source = { label: string; url: string };
type Activity = { tool: string; args?: Record<string, unknown>; ok?: boolean };
type PendingAsk = { id: string; question: string; options: string[]; header: string };
type TabId = "map" | "assets" | "docs" | "sources" | "notes" | "eorzeadb" | "browser";
const ALL_TABS: TabId[] = ["map", "eorzeadb", "assets", "docs", "browser", "sources", "notes"];
// Shown on the tab strip; the raw ids are lowercase words, which reads wrong for this one.
const TAB_LABEL: Record<TabId, string> = {
  map: "map", assets: "assets", docs: "docs", sources: "sources",
  notes: "notes", eorzeadb: "GarlandDB", browser: "browser",
};

// The chat column never shrinks below this. Paired with a `minmax(0, rightW)` right
// column, dragging the right panel wider shrinks ITSELF once the chat hits this floor,
// instead of squeezing the chat until its composer overflows into the panel.
const MIN_CHAT_W = 380;
// Floor for the right panel. There are six tabs; the strip wraps (see .tabs in
// App.css) so they're never truncated, and this stops the pane being dragged so
// narrow that the wrapped rows become unreadable. Deliberately a clamp on the DRAG
// rather than a grid min: a hard grid minimum would overflow the whole layout on a
// small window, which is a worse failure than a cramped panel.
const MIN_PANEL_W = 240;
// In split view the centre column carries BOTH the chat and a doc. Each half needs
// enough room to stay usable — a doc squeezed to 200px is worse than no split.
const MIN_SPLIT_W = 430;   // the doc half — wide enough for real editing, not just reading
const MIN_SPLIT_CHAT_W = 340;   // the chat half

// Theme-colored line icons (inherit currentColor from their button).
const svg = { viewBox: "0 0 24 24", width: 17, height: 17, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
const IconPaperclip = () => (
  <svg {...svg}><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" /></svg>
);
const IconFolder = () => (
  <svg {...svg}><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>
);
const IconMic = () => (
  <svg {...svg}><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v1a7 7 0 0 1-14 0v-1" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="8" y1="22" x2="16" y2="22" /></svg>
);
const IconTrash = () => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /></svg>
);

// Per-chat runtime state, so each chat streams independently in the background.
type ChatRuntime = {
  messages: Message[];
  streaming: boolean;
  draft: string;
  activity: Activity[];
  sources: Source[];
  assets: string[];
  sharedAssets: string[];   // asset names findable from your other profiles
  notes: DocItem[];
  docs: DocItem[];
  attachments: Attachment[];
  pendingAsk: PendingAsk | null;
  suggestions: string[];
  docLinks: { id: string; title: string; draft: boolean }[];
};
const EMPTY_RT: ChatRuntime = {
  messages: [], streaming: false, draft: "", activity: [], sources: [], assets: [], sharedAssets: [],
  notes: [], docs: [], attachments: [], pendingAsk: null, suggestions: [], docLinks: [],
};

// A short id for a new doc/note card.
function newId(): string {
  return Math.random().toString(36).slice(2, 10);
}
// Derive a card title from its markdown content (first non-empty line, de-marked).
function cardTitle(content: string): string {
  const line = content.split("\n").map((l) => l.trim()).find((l) => l.length > 0) || "";
  const t = line.replace(/^#+\s*/, "").replace(/^[-*>]\s*/, "").replace(/[*_`#]/g, "").trim();
  return t.slice(0, 70) || "Untitled";
}

// Human-readable label for a tool step shown in the live activity trail.
function stepLabel(tool: string, args?: Record<string, unknown>): string {
  const a = (args || {}) as Record<string, any>;
  switch (tool) {
    case "search_wiki":
      return `Searching ${a.wiki || "wiki"}${a.query ? `: “${a.query}”` : ""}`;
    case "get_market_price":
      return `Checking market price${a.item ? `: ${a.item}` : ""}`;
    case "whats_new":
      return "Fetching the latest FFXIV news";
    case "open_zone_map":
      return `Opening zone map${a.zone ? `: ${a.zone}` : ""}`;
    case "pin_on_map":
      return `Pinning ${a.place || "location"}${a.x != null ? ` (${a.x}, ${a.y})` : ""}`;
    case "show_image":
      return `Showing picture${a.label ? `: ${a.label}` : ""}`;
    case "annotate_image":
      return "Annotating image";
    default:
      return tool;
  }
}

// Approx context window per model (tokens). Used for the composer meter.
const CTX_MAX: Record<string, number> = {
  "claude-opus-4-8": 200000, "claude-sonnet-5": 200000,
  "gpt-5": 256000, "gemini/gemini-2.5-pro": 1000000, "xai/grok-4": 256000,
};
const fmtK = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`);

const THEMES = [
  { id: "eorzean-night", name: "Eorzean Night", dots: ["#0b1220", "#7fb3e6", "#c9a86a"] },
  { id: "crystarium", name: "Crystarium", dots: ["#e9f0f8", "#3f86c9", "#b9862f"] },
  { id: "garlean-steel", name: "Garlean Steel", dots: ["#0a0a0b", "#c0303a", "#9aa0a6"] },
  { id: "sharlayan-ivory", name: "Sharlayan Ivory", dots: ["#ece0c2", "#a9803f", "#29384f"] },
  { id: "astral-aether", name: "Astral Aether", dots: ["#14122b", "#c9a86a", "#9a8fd0"] },
];

export default function App() {
  const [models, setModels] = useState<Model[]>([]);
  // Size of the system prompt, from the backend. It's re-sent every turn, so it's a
  // real and otherwise invisible part of what a chat costs.
  const [systemTokens, setSystemTokens] = useState(0);
  const [sourceCatalog, setSourceCatalog] = useState<SourceInfo[]>([]);
  const [model, setModel] = useState<string>("");
  const [auth, setAuth] = useState<Auth>("api");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [theme, setTheme] = useState<string>(
    () => localStorage.getItem("theme") || "eorzean-night",
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  }, [theme]);

  // Text density: "compact" (default, the current tight layout) or "comfortable"
  // (roomier line-height and spacing). Applied as a root attribute the CSS keys off.
  const [density, setDensity] = useState<string>(
    () => localStorage.getItem("density") || "compact",
  );
  useEffect(() => {
    document.documentElement.setAttribute("data-density", density);
    localStorage.setItem("density", density);
  }, [density]);

  // Re-import each bound Lodestone character when the app launches. The backend
  // reads this at startup and only re-scrapes profiles that are over 24h stale, so
  // leaving it on doesn't hammer the Lodestone on every restart.
  const [refreshOnStart, setRefreshOnStart] = useState<boolean>(true);
  // Closing the main window hides it to the system tray — the overlay and
  // backend keep working; reopen from the tray icon. The Rust shell owns the
  // actual close behavior, so every change is pushed to it (set_close_to_tray).
  const [closeToTray, setCloseToTray] = useState<boolean>(false);
  // "Keep overlay surfaces open" — the overlay window reads this from
  // localStorage; the event is the live-update signal across windows.
  const [overlayKeepOpen, setOverlayKeepOpen] = useState<boolean>(
    () => localStorage.getItem("ov-keep-open") === "1");
  useEffect(() => {
    localStorage.setItem("ov-keep-open", overlayKeepOpen ? "1" : "0");
    import("@tauri-apps/api/event")
      .then(({ emit }) => emit("overlay://keep-open", overlayKeepOpen))
      .catch(() => { /* plain-web dev */ });
  }, [overlayKeepOpen]);

  // Updates: check GitHub on launch (on by default — being told a fix exists
  // is the point); installing without asking stays opt-in.
  const [autoCheckUpdates, setAutoCheckUpdates] = useState<boolean>(true);
  const [autoInstallUpdates, setAutoInstallUpdates] = useState<boolean>(false);
  const changeCloseToTray = (v: boolean) => {
    setCloseToTray(v);
    import("@tauri-apps/api/core")
      .then(({ invoke }) => invoke("set_close_to_tray", { enabled: v }))
      .catch(() => { /* plain-web dev */ });
  };

  // Overlay hotkeys — editable in Settings, applied live by the Rust shell.
  // Values are global-shortcut accelerator strings ("Alt+Backquote").
  const [overlayHotkeys, setOverlayHotkeys] = useState<OverlayHotkeySet>(OVERLAY_HOTKEY_DEFAULTS);
  const applyOverlayHotkeys = async (next: OverlayHotkeySet): Promise<string> => {
    const prev = overlayHotkeys;
    setOverlayHotkeys(next);
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("set_overlay_hotkeys", next as unknown as Record<string, string>);
      return "";
    } catch (e) {
      // Web dev has no shell — keep the state; a real bind error reverts.
      const msg = String(e);
      if (msg.includes("Can't parse") || msg.includes("must be different")
          || msg.includes("RegisterHotKey") || msg.includes("hotkey")) {
        setOverlayHotkeys(prev);
        return msg;
      }
      return "";
    }
  };

  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [chatId, setChatId] = useState<string>("");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  // "" until the backend tells us the profiles; there is no global workspace, so the
  // first profile is the fallback (the backend auto-creates one on a fresh install).
  const [activeWs, setActiveWs] = useState<string>(() => localStorage.getItem("activeWs") || "");
  const [moveMenu, setMoveMenu] = useState<string | null>(null); // chat id whose move menu is open
  useEffect(() => localStorage.setItem("activeWs", activeWs), [activeWs]);
  const activeWsMeta = workspaces.find((w) => w.slug === activeWs);
  const [runtimes, setRuntimes] = useState<Record<string, ChatRuntime>>({});
  const chatIdRef = useRef(chatId);
  const [input, setInput] = useState("");
  const [askDraft, setAskDraft] = useState(""); // free-text answer to a pending question
  const [editing, setEditing] = useState<{ index: number; text: string } | null>(null);
  // Doc/note editor tabs in the center pane; activeTab is "chat" or "kind:id".
  // "file" tabs edit backend-persisted markdown that isn't a chat doc: the agent
  // preferences file ("prefs") and character profiles ("profile:<slug>").
  const [editorTabs, setEditorTabs] = useState<{ kind: "docs" | "notes" | "file"; id: string }[]>([]);
  const [fileDocs, setFileDocs] = useState<Record<string, { title: string; content: string }>>({});
  const [activeTab, setActiveTab] = useState<string>("chat");
  const [fontScale, setFontScale] = useState<number>(() => {
    const v = parseFloat(localStorage.getItem("fontScale") || "1");
    return isNaN(v) ? 1 : Math.min(1.8, Math.max(0.7, v));
  });
  const bumpFont = (d: number) =>
    setFontScale((s) => Math.min(1.8, Math.max(0.7, +(s + d).toFixed(2))));

  // Apply + persist the text-size zoom. Use the native WebView zoom (real
  // browser-style zoom) so the WHOLE UI scales and reflows — CSS `zoom` on the
  // root breaks 100vh/100vw layouts and cuts panes off.
  useEffect(() => {
    localStorage.setItem("fontScale", String(fontScale));
    import("@tauri-apps/api/webviewWindow")
      .then(({ getCurrentWebviewWindow }) => getCurrentWebviewWindow().setZoom(fontScale))
      .catch(() => {
        // Non-Tauri fallback (e.g. dev browser).
        (document.documentElement.style as any).zoom = String(fontScale);
      });
  }, [fontScale]);

  // Overlay widget scale. Edited only here in the main app's Settings; the
  // overlay window picks it up from localStorage on mount and live via the
  // overlay://scale broadcast (a storage event alone isn't guaranteed to fire
  // across Tauri windows).
  const [overlayScale, setOverlayScale] = useState<number>(() => {
    const v = parseFloat(localStorage.getItem("overlayScale") || "1");
    return isNaN(v) ? 1 : Math.min(1.6, Math.max(0.7, v));
  });
  const bumpOverlayScale = (d: number) =>
    setOverlayScale((s) => Math.min(1.6, Math.max(0.7, +(s + d).toFixed(2))));
  useEffect(() => {
    localStorage.setItem("overlayScale", String(overlayScale));
    import("@tauri-apps/api/event")
      .then(({ emit }) => emit("overlay://scale", overlayScale))
      .catch(() => { /* plain-web dev: the overlay tab hears the storage event */ });
  }, [overlayScale]);

  // Startup update check. Only ever SAYS a release exists (or, if the player
  // opted in, fetches and launches the installer) — never silently swaps the
  // app out from under them mid-session.
  const [updateReady, setUpdateReady] = useState<{ version: string } | null>(null);
  useEffect(() => {
    if (!autoCheckUpdates || !settingsHydrated.current) return;
    let cancelled = false;
    const t = window.setTimeout(async () => {
      try {
        const { getVersion } = await import("@tauri-apps/api/app");
        const cur = await getVersion();
        const info = await api.updateCheck(cur);
        if (cancelled || !info.found || !info.newer || !info.version) return;
        setUpdateReady({ version: info.version });
        if (!autoInstallUpdates) return;
        await api.updateDownload();
        const poll = window.setInterval(async () => {
          const s = await api.updateStatus().catch(() => null);
          if (!s) return;
          if (s.status === "ready") {
            window.clearInterval(poll);
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("install_update", { path: s.path, silent: false })
              .catch(() => {});
          } else if (s.status === "error") {
            window.clearInterval(poll);
          }
        }, 1000);
      } catch { /* offline, or no shell in web dev — stay quiet */ }
    }, 4000);   // let the app finish booting first
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [autoCheckUpdates, autoInstallUpdates]);

  // The overlay drawer's "Open in app": land on that database record.
  useEffect(() => {
    let un: (() => void) | undefined;
    import("@tauri-apps/api/event")
      .then(({ listen }) =>
        listen("overlay://open-db", (e) => {
          const p = e.payload as { kind?: string; id?: string };
          if (p?.kind && p?.id) {
            setTab("eorzeadb");
            void openDbKind(p.kind, p.id);
          }
        }))
      .then((u) => { un = u; })
      .catch(() => { /* plain-web dev */ });
    return () => { if (un) un(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // The overlay's "Open map" card action: the Rust side focuses this window
  // and forwards the map payload (same shape as a chat `map` event). Global
  // listen() + emit_to("main") — the proven embed-nav pattern.
  useEffect(() => {
    let un: (() => void) | undefined;
    import("@tauri-apps/api/event")
      .then(({ listen }) =>
        listen("overlay://open-map", (e) => {
          const p = e.payload as {
            zone?: string;
            focus?: { x: number; y: number } | null;
            pin?: { x: number; y: number; label: string; icon?: string;
                    radius_px?: number; space?: "map" | "game" } | null;
            pins?: { x: number; y: number; label?: string }[] | null;
            category?: string;
            icon?: string;
          };
          if (p?.zone) {
            void openZoneMap(p.zone, "", true, p.focus ?? null,
                             // Chat map events are 2048 map space; node
                             // watches carry GAME coords and say so.
                             p.pin ? { ...p.pin, space: p.pin.space === "game" ? "game" : "map" } : null,
                             p.pins?.length
                               ? { category: p.category || "Points", icon: p.icon || undefined,
                                   space: "map",
                                   pins: p.pins.map((x) => ({ ...x, label: x.label || "" })) }
                               : null);
          }
        }))
      .then((u) => { un = u; })
      .catch(() => { /* plain-web dev: no overlay window exists */ });
    return () => { if (un) un(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Ctrl/Cmd + ] increases text size, Ctrl/Cmd + [ decreases it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "]") { e.preventDefault(); bumpFont(0.1); }
      else if (e.key === "[") { e.preventDefault(); bumpFont(-0.1); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // A pinned-map image (from pin_on_map) shown in the Map view.
  const [mapImg, setMapImg] = useState<string>("");
  const [mapImgZone, setMapImgZone] = useState<string>("");
  // The rebuilt in-game map. Null shows the zone picker; mapErr holds the zone
  // whose load failed, which renders an explicit error + Retry — never a fallback
  // site (the old ARR iframe just dumped the player on someone's homepage).
  const [gameMap, setGameMap] = useState<ZoneMap | null>(null);
  const [mapBusy, setMapBusy] = useState("");   // zone currently loading, "" = idle
  const [mapErr, setMapErr] = useState("");     // zone whose load failed
  // WHY it failed, shown on the error card — chasing a "won't load" report without
  // this meant guessing between the data fetch, the texture, and the webview cache.
  const [mapErrDetail, setMapErrDetail] = useState("");
  // A TEMPORARY pin (agent answer / chat map-link). Lives only until the next
  // zone opens — deliberately never written into the player's pin store.
  // icon: a named game symbol; radiusPx: an AREA circle (2048 map space) —
  // both optional, both temporary like the pin itself. kind is what the pin
  // becomes if the player KEEPS it (its toolbar group, e.g. "gathering").
  const [tempPin, setTempPin] = useState<{
    x: number; y: number; label: string; icon?: string; radiusPx?: number; kind?: string;
  } | null>(null);
  // A whole CATEGORY of temporary pins at once ("pin all the aether currents").
  // Cleared on every zone open like tempPin; the map's 💾 button saves the set
  // as "Custom – <category>". Coordinates in 2048 map space.
  const [tempGroup, setTempGroup] = useState<{
    category: string; icon?: string;
    pins: { x: number; y: number; label: string }[];
  } | null>(null);
  const [zoneRegions, setZoneRegions] = useState<{ region: string; zones: string[] }[]>([]);
  const [zoneRegion, setZoneRegion] = useState("");
  const [zoneQuery, setZoneQuery] = useState("");
  const mapAutoLoaded = useRef(false);
  // False until a COMPLETE zone list arrives — an incomplete one (flaky fetch)
  // must keep being refetched, or zones silently vanish from the picker.
  const zonesComplete = useRef(false);
  const [annotating, setAnnotating] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<string | null>(null); // asset shown full-screen
  const [lbZoom, setLbZoom] = useState(false);
  const [leftW, setLeftW] = useState<number>(() => +(localStorage.getItem("leftW") || 200));
  const [rightW, setRightW] = useState<number>(
    () => Math.max(MIN_PANEL_W, +(localStorage.getItem("rightW") || 300)),
  );
  const gutter = useRef<null | "left" | "right">(null);

  useEffect(() => localStorage.setItem("leftW", String(leftW)), [leftW]);
  useEffect(() => localStorage.setItem("rightW", String(rightW)), [rightW]);
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (gutter.current === "left") setLeftW(Math.min(400, Math.max(150, e.clientX)));
      else if (gutter.current === "right")
        setRightW(Math.min(820, Math.max(MIN_PANEL_W, window.innerWidth - e.clientX)));
    };
    const up = () => {
      gutter.current = null;
      document.body.style.cursor = "";
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, []);
  const startDrag = (which: "left" | "right") => (e: React.PointerEvent) => {
    e.preventDefault();
    gutter.current = which;
    document.body.style.cursor = "col-resize";
  };
  // Dockable tabs: each tab lives in the "right" panel or a "bottom" panel.
  const [dock, setDock] = useState<Record<string, "right" | "bottom">>(() => {
    const s = localStorage.getItem("tabDock");
    if (s) { try { return JSON.parse(s); } catch { /* ignore */ } }
    return { map: "right", assets: "right", docs: "right", sources: "right", notes: "right" };
  });
  const [actRight, setActRight] = useState<TabId>("sources");
  const [actBottom, setActBottom] = useState<TabId>("map");
  const [bottomH, setBottomH] = useState<number>(() => +(localStorage.getItem("bottomH") || 280));

  // --- Split view: chat and an open doc side by side in the centre column ---
  // Persisted, so a workflow you set up survives a restart.
  const [splitView, setSplitView] = useState(false);
  const [splitW, setSplitW] = useState<number>(() =>
    Math.max(MIN_SPLIT_W, +(localStorage.getItem("splitW") || 460)),
  );
  useEffect(() => localStorage.setItem("splitW", String(splitW)), [splitW]);
  const vgutter = useRef(false);

  // --- Durable UI settings ---
  // Persist prefs in the backend's per-user data dir so they survive reinstalls
  // (WebView localStorage gets wiped by the installer). Hydrate once on launch,
  // then write through on change; localStorage stays as a fast no-flash cache.
  const settingsHydrated = useRef(false);
  useEffect(() => {
    (async () => {
      try {
        await api.ready();
        const s = await api.getAppSettings();
        if (s && typeof s === "object") {
          if (typeof s.theme === "string") setTheme(s.theme);
          if (typeof s.density === "string") setDensity(s.density);
          if (typeof s.refresh_profile_on_start === "boolean")
            setRefreshOnStart(s.refresh_profile_on_start);
          if (typeof s.overlayKeepOpen === "boolean") setOverlayKeepOpen(s.overlayKeepOpen);
          if (typeof s.autoCheckUpdates === "boolean") setAutoCheckUpdates(s.autoCheckUpdates);
          if (typeof s.autoInstallUpdates === "boolean") setAutoInstallUpdates(s.autoInstallUpdates);
          if (typeof s.closeToTray === "boolean") {
            setCloseToTray(s.closeToTray);
            // Push to the Rust shell, which owns the close behavior.
            import("@tauri-apps/api/core")
              .then(({ invoke }) => invoke("set_close_to_tray", { enabled: s.closeToTray }))
              .catch(() => { /* plain-web dev */ });
          }
          // V2 on purpose: the V1 bindings could end up with the ambient
          // combo shadowing the ask pill — everyone migrates to fresh
          // defaults once (ask=Alt+`, ambient=Alt+Shift+`), then edits stick.
          if (s.overlayHotkeysV2 && typeof s.overlayHotkeysV2 === "object") {
            const saved = { ...OVERLAY_HOTKEY_DEFAULTS,
                            ...(s.overlayHotkeysV2 as Partial<OverlayHotkeySet>) };
            setOverlayHotkeys(saved);
            import("@tauri-apps/api/core")
              .then(({ invoke }) =>
                invoke("set_overlay_hotkeys", saved as unknown as Record<string, string>))
              .catch(() => { /* plain-web dev, or a taken combo — defaults stay */ });
          }
          if (typeof s.splitView === "boolean") setSplitView(s.splitView);
          if (typeof s.splitW === "number") setSplitW(Math.max(MIN_SPLIT_W, s.splitW));
          if (typeof s.fontScale === "number") setFontScale(s.fontScale);
          if (typeof s.overlayScale === "number")
            setOverlayScale(Math.min(1.6, Math.max(0.7, s.overlayScale)));
          if (typeof s.activeWs === "string") setActiveWs(s.activeWs);
          if (typeof s.leftW === "number") setLeftW(s.leftW);
          if (typeof s.rightW === "number") setRightW(Math.max(MIN_PANEL_W, s.rightW));
          if (typeof s.bottomH === "number") setBottomH(s.bottomH);
          if (s.dock && typeof s.dock === "object")
            setDock(s.dock as Record<string, "right" | "bottom">);
        }
      } catch {
        /* backend unreachable — fall back to localStorage/defaults */
      } finally {
        settingsHydrated.current = true;
      }
    })();
  }, []);
  useEffect(() => {
    // Wait for the model to resolve as well as for settings to hydrate. PUT
    // replaces the whole settings object, so writing while model is still "" would
    // blank the saved defaultModel we're about to read back.
    if (!settingsHydrated.current || !model) return;
    const t = window.setTimeout(() => {
      api
        .putAppSettings({
          theme, density, fontScale, overlayScale, activeWs, leftW, rightW, bottomH, dock,
          refresh_profile_on_start: refreshOnStart,
          closeToTray, overlayHotkeysV2: overlayHotkeys,
          autoCheckUpdates, autoInstallUpdates, overlayKeepOpen,
          defaultModel: model, defaultAuth: auth,
          splitView, splitW,
        })
        .catch(() => {});
    }, 400);
    return () => window.clearTimeout(t);
  }, [theme, density, fontScale, overlayScale, activeWs, leftW, rightW, bottomH, dock,
      refreshOnStart, closeToTray, overlayHotkeys, autoCheckUpdates, autoInstallUpdates,
      overlayKeepOpen, model, auth, splitView, splitW]);

  const dragTab = useRef<TabId | null>(null);
  const hgutter = useRef(false);
  const [dragging, setDragging] = useState(false);
  // The tab currently being dragged, as STATE (dragTab is a ref, which can't
  // re-render). This is what lets the tab visibly lift while you hold it.
  const [heldTab, setHeldTab] = useState<TabId | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  // Same idea for the centre editor tab strip (Chat | doc | note).
  const dragEditorTab = useRef<string | null>(null);
  const [heldEditorTab, setHeldEditorTab] = useState<string | null>(null);
  // Player-defined tab order. Panel tabs used to render straight from ALL_TABS, so
  // their order was a constant and couldn't be changed; this is the persisted
  // override. Unknown/new tabs fall back to ALL_TABS order via `tabOrderOf`.
  const [tabOrder, setTabOrder] = useState<TabId[]>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("tabOrder") || "null");
      if (Array.isArray(saved)) {
        const kept = saved.filter((t: TabId) => ALL_TABS.includes(t));
        return [...kept, ...ALL_TABS.filter((t) => !kept.includes(t))];
      }
    } catch { /* fall through to default */ }
    return ALL_TABS;
  });
  useEffect(() => localStorage.setItem("tabOrder", JSON.stringify(tabOrder)), [tabOrder]);

  useEffect(() => localStorage.setItem("tabDock", JSON.stringify(dock)), [dock]);
  useEffect(() => localStorage.setItem("bottomH", String(bottomH)), [bottomH]);
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (vgutter.current) {
        // Width of the DOC half = distance from the pointer to the chat column's
        // right edge. Clamped both ways so neither half collapses.
        const box = document.querySelector(".chat")?.getBoundingClientRect();
        if (box) {
          const want = box.right - e.clientX;
          const max = Math.max(MIN_SPLIT_W, box.width - MIN_SPLIT_CHAT_W - 6);
          setSplitW(Math.min(max, Math.max(MIN_SPLIT_W, want)));
        }
      }
      if (hgutter.current) {
        const chatBox = document.querySelector(".chat")?.getBoundingClientRect();
        if (chatBox) setBottomH(Math.min(chatBox.height - 120, Math.max(120, chatBox.bottom - e.clientY)));
      }
    };
    const up = () => {
      hgutter.current = false;
      vgutter.current = false;
      document.body.style.cursor = "";
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  }, []);

  const rightTabs = tabOrder.filter((t) => (dock[t] || "right") === "right");
  const bottomTabs = tabOrder.filter((t) => dock[t] === "bottom");
  const activate = (t: TabId) => (dock[t] === "bottom" ? setActBottom(t) : setActRight(t));
  function setTab(t: TabId) { activate(t); }
  function moveTab(t: TabId, zone: "right" | "bottom") {
    setDock((d) => ({ ...d, [t]: zone }));
    zone === "bottom" ? setActBottom(t) : setActRight(t);
  }

  // --- Embedded browser tab ---
  // A link click anywhere in the app lands here: bump seq so clicking the SAME
  // link twice still (re)navigates, then bring the browser tab forward. Kept in
  // a render-refreshed ref because the click interceptor below is a mount-once
  // listener whose closures would otherwise hold a stale `dock`.
  const [browserReq, setBrowserReq] = useState<BrowserReq>(null);
  const browserSeq = useRef(0);
  const openInBrowserRef = useRef<(url: string) => void>(() => {});
  openInBrowserRef.current = (url: string) => {
    browserSeq.current += 1;
    setBrowserReq({ url, seq: browserSeq.current });
    setTab("browser");
  };

  /** Move `t` so it sits immediately before `before` in the shared tab order.
   *  One list drives both strips, so a reorder within a strip and a move between
   *  strips are the same operation — the dock map decides which strip renders it. */
  function reorderTab(t: TabId, before: TabId | null) {
    setTabOrder((order) => {
      const next = order.filter((x) => x !== t);
      const at = before ? next.indexOf(before) : -1;
      if (at < 0) next.push(t);
      else next.splice(at, 0, t);
      return next;
    });
  }

  function endDrag() {
    dragTab.current = null;
    setHeldTab(null);
    setDropTarget(null);
    setDragging(false);
  }

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    chatIdRef.current = chatId;
  }, [chatId]);

  // Editor tabs belong to the open chat — reset them when the chat changes.
  useEffect(() => {
    setEditorTabs([]);
    setActiveTab("chat");
  }, [chatId]);

  // Patch one chat's runtime by id (works even if it's not the active chat —
  // that's what lets background streams keep updating while you're elsewhere).
  function patchRt(
    id: string,
    patch: Partial<ChatRuntime> | ((r: ChatRuntime) => Partial<ChatRuntime>),
  ) {
    setRuntimes((all) => {
      const cur = all[id] || EMPTY_RT;
      const p = typeof patch === "function" ? patch(cur) : patch;
      return { ...all, [id]: { ...cur, ...p } };
    });
  }

  // The active chat's state, destructured so existing render code is untouched.
  const rt = runtimes[chatId] || EMPTY_RT;
  const { messages, streaming, draft, activity, sources, assets, sharedAssets, notes, docs, attachments, pendingAsk, suggestions, docLinks } = rt;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    folderInputRef.current?.setAttribute("webkitdirectory", "");
    folderInputRef.current?.setAttribute("directory", "");
  }, []);

  async function uploadFiles(files: FileList | null) {
    if (!files || !files.length) return;
    let id = chatId;
    if (!id) {
      const c = await api.createChat(activeWs);
      id = c.id;
      setChatId(id);
      patchRt(id, { ...EMPTY_RT });
    }
    const r = await api.attachFiles(id, Array.from(files));
    patchRt(id, { attachments: r.attachments });
  }
  async function removeAttachment(name: string) {
    const r = await api.deleteAttachment(chatId, name);
    patchRt(chatId, { attachments: r.attachments });
  }

  useEffect(() => {
    // Gate initial loads on the backend being up — a cold launch starts the
    // Python sidecar a few seconds after the window, and un-gated fetches would
    // race it, fail, and leave the chat list empty until the next action.
    (async () => {
      await api.ready();
      try {
        const r = await api.models();
        setModels(r.models);
        setSystemTokens(r.system_tokens || 0);
        // Resolve the startup model HERE rather than in the settings-hydration
        // effect: the two run independently, and whichever landed second would win.
        // Reading the saved default in this effect makes the order irrelevant.
        let saved: Record<string, unknown> = {};
        try { saved = await api.getAppSettings(); } catch { /* use server default */ }
        const wanted = typeof saved.defaultModel === "string" ? saved.defaultModel : "";
        const hit = r.models.find((m) => m.id === wanted && m.available);
        if (hit) {
          // Honour the saved auth only if that model still offers it — a removed
          // key or token must not strand the picker on an unusable pairing.
          const a = saved.defaultAuth as Auth | undefined;
          setModel(hit.id);
          setAuth(a && hit.auth_options.includes(a) ? a : (hit.default_auth ?? "api"));
        } else if (r.default) {
          setModel(r.default.id);
          setAuth(r.default.auth);
        } else if (r.models[0]) {
          setModel(r.models[0].id);
          setAuth(r.models[0].default_auth ?? "api");
        }
      } catch { /* leave defaults */ }
      // Static catalog — fetch once so the Sources tab can offer a "Support" link
      // for whichever project a citation came from.
      try { setSourceCatalog((await api.sources()).sources); } catch { /* no support links */ }
      await refreshChats();
      await refreshWorkspaces();
      // WebView2 (Windows) can skip painting DOM updates that happen right after
      // the window first shows, until a user interaction — which is why the loaded
      // chat list stayed invisible until you clicked. Force a composite so it
      // appears on its own.
      nudgeRepaint();
      // Warm the last-viewed zone map in the background — the window is already up,
      // so this costs launch nothing, and the Map tab is drawn by the time it's
      // opened. switchTab=false: warming must not steal the active tab.
      if (!mapAutoLoaded.current) {
        mapAutoLoaded.current = true;
        const last = localStorage.getItem("lastMapZone") || DEFAULT_MAP_ZONE;
        openZoneMap(last, "", false).catch(() => {});
      }
    })();
  }, []);

  // EVERY http(s) link in the app — chat markdown, source cards, doc/note editor
  // content, map credits, asset links — opens in the in-app BROWSER TAB in the
  // right pane. Letting one navigate the webview would replace the app itself.
  // One delegated listener covers all of them, including links inside
  // contentEditable content that no component renders.
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const a = (e.target as HTMLElement | null)?.closest?.("a");
      const href = a?.getAttribute("href") || "";
      if (!a) return;
      // `map:<zone>` opens that zone on the interactive Map tab; an optional
      // `?x=&y=&label=` query (in-game flag coords) shows a TEMPORARY pin there —
      // this is how a pin the agent placed stays one click away in the chat.
      // Handled BEFORE the http(s) filter — a custom scheme would otherwise fall
      // through to the webview's default navigation and go nowhere.
      if (href.startsWith("map:")) {
        e.preventDefault();
        const raw = decodeURIComponent(href.slice(4)).trim();
        const qi = raw.indexOf("?");
        const zone = (qi < 0 ? raw : raw.slice(0, qi)).trim();
        let pin: { x: number; y: number; label: string; space: "game";
                   icon?: string; r?: number } | null = null;
        if (qi >= 0) {
          const q = new URLSearchParams(raw.slice(qi + 1));
          const x = parseFloat(q.get("x") || ""), y = parseFloat(q.get("y") || "");
          if (isFinite(x) && isFinite(y)) {
            pin = { x, y, label: q.get("label") || `(${x}, ${y})`, space: "game" };
            // Optional typed marker: &icon=<name> and &r=<radius in map coords>
            // (an AREA — mob zone, FATE circle, fishing hole).
            const icon = (q.get("icon") || "").trim();
            if (icon) pin.icon = icon;
            const r = parseFloat(q.get("r") || "");
            if (isFinite(r) && r > 0) pin.r = r;
          }
          // A CATEGORY of points: &cat=<name>&pts=x,y,label|x,y,label…
          // (how "pin all the aether currents" survives as a clickable link).
          const pts = q.get("pts");
          if (pts) {
            const pinsList = pts.split("|").map((s) => {
              const [px, py, ...rest] = s.split(",");
              return { x: parseFloat(px), y: parseFloat(py),
                       label: rest.join(",").trim() };
            }).filter((p) => isFinite(p.x) && isFinite(p.y));
            if (pinsList.length && zone) {
              openZoneMap(zone, "", true, null, null, {
                category: q.get("cat") || "Points",
                icon: (q.get("icon") || "").trim() || undefined,
                space: "game", pins: pinsList,
              });
              return;
            }
          }
        }
        if (zone) openZoneMap(zone, "", true, null, pin);
        return;
      }
      if (!/^https?:\/\//i.test(href)) return;
      e.preventDefault();
      // data-external = "this link's job is to LEAVE the app": support/donation
      // pages (♥ Support, ♥ Support Me) and the "View on Garland Tools" button
      // open in the player's DEFAULT BROWSER, not the in-app pane — a Ko-fi
      // login or payment belongs in their real browser session.
      if (a.hasAttribute("data-external")) {
        openUrl(href).catch(() => window.open(href, "_blank"));
        return;
      }
      // Garland database links stay INSIDE the app — they open in the GarlandDB tab
      // rather than the browser pane, which can't show them half as well.
      if (/garlandtools\.org\/db\/#/i.test(href)) {
        openDbUrl(href);
        return;
      }
      // Everything else (wikis, the Lodestone…) → the in-app browser tab.
      openInBrowserRef.current(href);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, draft, activity]);

  async function refreshChats() {
    try {
      const r = await api.listChats();
      setChats(r.chats);
    } catch { /* backend not ready yet; a later action will refresh */ }
  }

  async function refreshWorkspaces() {
    try {
      const r = await api.workspaces();
      setWorkspaces(r.workspaces);
      // If the active workspace vanished (deleted), fall back to global.
      // Fall back to the first profile if the active one vanished (or on first run).
      if (!r.workspaces.some((w) => w.slug === activeWs) && r.workspaces[0])
        setActiveWs(r.workspaces[0].slug);
    } catch { /* backend not ready */ }
  }

  async function openChat(id: string) {
    setChatId(id);
    if (runtimes[id]) return; // already loaded (or streaming) — keep its state
    const [c, a, at] = await Promise.all([
      api.getChat(id), api.listAssets(id), api.listAttachments(id),
    ]);
    patchRt(id, {
      messages: c.messages, assets: a.assets, notes: c.notes || [], docs: c.docs || [],
      attachments: at.attachments,
      sources: c.sources || [], // persisted server-side, so the tab survives a restart
      sharedAssets: c.shared_assets || [],
    });
  }

  async function newChat() {
    const c = await api.createChat(activeWs);
    await refreshChats();
    setChatId(c.id);
    patchRt(c.id, { ...EMPTY_RT });
  }

  async function deleteChat(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!confirm("Delete this chat and its attachments?")) return;
    await api.deleteChat(id);
    setRuntimes((r) => { const copy = { ...r }; delete copy[id]; return copy; });
    if (chatId === id) setChatId("");
    await refreshChats();
  }

  // --- profile workspaces ---
  function switchWs(slug: string) {
    setActiveWs(slug);
    setMoveMenu(null);
  }
  async function newProfile() {
    const name = window.prompt("New profile name (e.g. “WHM on Cactuar”):");
    if (!name || !name.trim()) return;
    const w = await api.createWorkspace(name.trim());
    await refreshWorkspaces();
    setActiveWs(w.slug);
    setSettingsOpen(true); // jump to settings to bind a character / edit the profile
  }
  async function moveChatTo(id: string, owner: string, e: React.MouseEvent) {
    e.stopPropagation();
    setMoveMenu(null);
    await api.moveChat(id, owner);
    await refreshChats();
  }
  const wsName = (slug: string) => workspaces.find((w) => w.slug === slug)?.display_name || slug;
  const profileWorkspaces = workspaces.filter((w) => w.kind === "profile");
  const allScopedChats = chats.filter((c) => c.owner === activeWs);
  // The in-game Ask pill's chats sit in their own sidebar section — they're a
  // running log of play-time questions, not something to mix into the list.
  const scopedChats = allScopedChats.filter((c) => c.surface !== "overlay");
  const overlayChats = allScopedChats.filter((c) => c.surface === "overlay");
  // Collapsed by default; the choice sticks for the session.
  const [overlayChatsOpen, setOverlayChatsOpen] = useState(false);

  function chatRow(c: ChatSummary) {
    return (
      <div
        key={c.id}
        className={"chat-item" + (c.id === chatId ? " active" : "")}
        onClick={() => openChat(c.id)}
      >
        {runtimes[c.id]?.streaming && <span className="streaming-dot" />}
        <span className="chat-title">{c.title || "New chat"}</span>
        <button
          className="chat-move" title="Move or share"
          onClick={(e) => { e.stopPropagation(); setMoveMenu(moveMenu === c.id ? null : c.id); }}
        >
          ⋯
        </button>
        <button className="chat-del" title="Delete chat" onClick={(e) => deleteChat(c.id, e)}>
          <IconTrash />
        </button>
        {moveMenu === c.id && (
          <div className="move-menu" onClick={(e) => e.stopPropagation()}>
            {profileWorkspaces
              .filter((w) => w.slug !== c.owner)
              .map((w) => (
                <button key={w.slug} onClick={(e) => moveChatTo(c.id, w.slug, e)}>
                  Move to {w.display_name}
                </button>
              ))}
            <div className="move-cur">In: {wsName(c.owner)}</div>
          </div>
        )}
      </div>
    );
  }

  const activeModel = models.find((m) => m.id === model);
  const canSend = !!activeModel?.available && !streaming && input.trim().length > 0;

  // Approximate context-window usage for the current chat (chars/4 heuristic).
  const ctxMax = CTX_MAX[model] || 200000;
  const ctxUsed = Math.round(
    (messages.reduce((n, m) => n + m.content.length, 0) + draft.length) / 4,
  );
  const ctxPct = Math.min(100, (ctxUsed / ctxMax) * 100);

  // Rough $ spent on this chat so far.
  //
  // NOT (total tokens x price): the API is stateless, so every turn re-sends the
  // system prompt AND the whole conversation so far. A 10-turn chat pays for turn 1's
  // text ten times over. So walk the turns and charge each one for the history it
  // actually carried — a flat sum would understate a long chat several-fold.
  //
  // Still an ESTIMATE: chars/4 is not a real tokenizer, tool results and images sent
  // mid-turn aren't in `messages`, and prompt caching (which the agent path uses)
  // makes repeated prefixes far cheaper than list price. It reads high more often
  // than low — treat it as an upper bound, not an invoice.
  const chatCost = (() => {
    const m = models.find((x) => x.id === model);
    const inPrice = m?.input_cost_per_token || 0;
    const outPrice = m?.output_cost_per_token || 0;
    if (!inPrice && !outPrice) return null;         // unknown pricing -> show nothing
    let history = systemTokens;                     // re-sent on every request
    let total = 0;
    for (const msg of messages) {
      const t = Math.round(msg.content.length / 4);
      if (msg.role === "user") {
        history += t;
      } else {
        total += history * inPrice + t * outPrice;  // this turn's bill
        history += t;                               // ...and it joins the history
      }
    }
    return total;
  })();

  // Sub-cent figures are the common case; "$0.00" would read as free.
  const fmtCost = (c: number) =>
    c >= 1 ? `$${c.toFixed(2)}` : c >= 0.01 ? `$${c.toFixed(2)}` : c > 0 ? "<$0.01" : "$0.00";

  // Dictation via the browser Speech Recognition API.
  const [listening, setListening] = useState(false);
  // The in-flight chat request, so the Stop button can cancel it.
  const chatAbort = useRef<AbortController | null>(null);
  // The composer's "ignore my profile" switch: answer without the player's
  // personal profile in context. Persisted — it's a mode, not a per-message whim.
  const [ignoreProfile, setIgnoreProfile] = useState(
    () => localStorage.getItem("ignoreProfile") === "1");
  useEffect(() => {
    localStorage.setItem("ignoreProfile", ignoreProfile ? "1" : "0");
  }, [ignoreProfile]);
  const recogRef = useRef<any>(null);
  function toggleDictation() {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) {
      alert("Dictation isn't available in this window. You can use Windows dictation (Win + H).");
      return;
    }
    if (listening) {
      recogRef.current?.stop();
      return;
    }
    const r = new SR();
    r.continuous = true;
    r.interimResults = true;
    r.lang = "en-US";
    const base = input ? input + " " : "";
    r.onresult = (e: any) => {
      let txt = "";
      for (let i = e.resultIndex; i < e.results.length; i++) txt += e.results[i][0].transcript;
      setInput((base + txt).replace(/\s+/g, " ").trimStart());
    };
    r.onerror = () => setListening(false);
    r.onend = () => setListening(false);
    recogRef.current = r;
    r.start();
    setListening(true);
  }

  async function send() {
    if (!canSend) return;
    let id = chatId;
    if (!id) {
      const c = await api.createChat(activeWs);
      id = c.id;
      setChatId(id);
    }
    const text = input.trim();
    setInput("");
    await runTurn(id, text);
  }

  // Suggestion chips and edit-resend both drive a turn programmatically.
  function sendSuggestion(text: string) {
    if (!chatId || rt.streaming || !activeModel?.available) return;
    runTurn(chatId, text);
  }

  // Stream one assistant turn for `text` in chat `id`.
  async function runTurn(id: string, text: string) {
    patchRt(id, (r) => ({
      messages: [...r.messages, { role: "user", content: text }],
      streaming: true, draft: "", activity: [], pendingAsk: null, suggestions: [],
    }));

    // The Stop button aborts this: the fetch cancels, the backend generator is
    // cancelled with it (stopping the model run), and the partial answer is
    // kept on both sides.
    const controller = new AbortController();
    chatAbort.current = controller;

    let acc = "";
    const turnDocs: { id: string; title: string; draft: boolean }[] = [];
    try {
      await api.streamChat(id, model, auth, text, (e) => {
        if (e.type === "token") {
          acc += e.text;
          patchRt(id, { draft: acc });
        } else if (e.type === "tool") {
          patchRt(id, (r) => ({ activity: [...r.activity, { tool: e.name, args: e.args }] }));
        } else if (e.type === "ask") {
          patchRt(id, {
            pendingAsk: { id: e.id, question: e.question, options: e.options, header: e.header },
          });
        } else if (e.type === "doc") {
          turnDocs.push({ id: e.id, title: e.title || "Draft doc", draft: e.draft });
          patchRt(id, (r) => ({
            docs: r.docs.some((d) => d.id === e.id)
              ? r.docs
              : [...r.docs, { id: e.id, content: e.content, title: e.title, draft: e.draft }],
            // Show the current turn's links under the live streaming block; they
            // get attached to the finalized message below.
            docLinks: [...turnDocs],
          }));
        } else if (e.type === "tool_result") {
          patchRt(id, (r) => {
            const copy = [...r.activity];
            for (let i = copy.length - 1; i >= 0; i--)
              if (copy[i].tool === e.name && copy[i].ok === undefined) {
                copy[i] = { ...copy[i], ok: e.ok };
                break;
              }
            return { activity: copy };
          });
        } else if (e.type === "source") {
          patchRt(id, (r) =>
            r.sources.some((x) => x.label === e.label)
              ? {}
              : { sources: [...r.sources, { label: e.label, url: e.url }] },
          );
        } else if (e.type === "asset") {
          // Agent-fetched images are TEMPORARY (tmp_*): they render inline in
          // the chat with a hover "Add to Assets" button, and never land on the
          // shelf — or yank the player to the Assets tab — by themselves.
          const temp = e.name.startsWith("tmp_");
          if (!temp) {
            patchRt(id, (r) => (r.assets.includes(e.name) ? {} : { assets: [...r.assets, e.name] }));
          }
          if (e.kind === "map") {
            // A pinned static map (non-drawable zone fallback): still shown in
            // the Map view — that's navigation, not asset hoarding.
            setGameMap(null);   // the agent pinned a static image; show that, not a stale zone
            setMapImg(api.assetUrl(id, e.name));
            setMapImgZone(e.zone || "");
            if (chatIdRef.current === id) setTab("map");
          }
        } else if (e.type === "map") {
          // The agent opened a zone map — with a TEMPORARY pin when it pointed
          // at an exact spot, or a whole CATEGORY of temp pins (pin_points_on_map).
          // Both clear as soon as another zone opens.
          openZoneMap(e.zone, "", chatIdRef.current === id, e.focus,
                      e.pin ? { ...e.pin, space: "map" } : null,
                      e.pins?.length ? { category: e.category || "Points",
                                         icon: e.icon || undefined,
                                         space: "map", pins: e.pins } : null);
        } else if (e.type === "error") {
          acc += `\n\n⚠️ ${e.message}`;
          patchRt(id, { draft: acc });
        }
      }, ignoreProfile, controller.signal);
    } catch (err) {
      // A Stop-button abort is the player's choice, not a failure — keep the
      // partial text without a scary warning banner.
      if ((err as Error)?.name !== "AbortError") {
        acc += `\n\n⚠️ ${String(err)}`;
      }
    } finally {
      if (chatAbort.current === controller) chatAbort.current = null;
    }
    patchRt(id, (r) => ({
      messages: [...r.messages, {
        role: "assistant", content: acc,
        ...(turnDocs.length ? { docLinks: turnDocs } : {}),
      }],
      streaming: false, draft: "", pendingAsk: null,
      docLinks: [], // the turn's links now live on the message, not the chat tail
    }));
    refreshChats();
    fetchSuggestions(id);
  }

  // Ask the backend for a few short follow-ups the player might send next.
  async function fetchSuggestions(id: string) {
    try {
      const r = await api.suggestions(id, model, auth);
      patchRt(id, { suggestions: r.suggestions || [] });
    } catch {
      /* suggestions are best-effort */
    }
  }

  // Add a new card to a chat's docs or notes list (and persist).
  function addCard(kind: "docs" | "notes", content: string): string {
    const id = newId();
    const cur = (runtimes[chatId]?.[kind] as DocItem[]) || [];
    const next = [...cur, { id, content }];
    patchRt(chatId, { [kind]: next } as any);
    if (chatId) (kind === "docs" ? api.putDocs : api.putNotes)(chatId, next).catch(() => {});
    return id;
  }
  // Message-action helpers: save a reply as a doc (AI-referable) or a private note.
  function saveToDocs(text: string) {
    if (!chatId) return;
    addCard("docs", text);
    setTab("docs");
  }
  function addToNotes(text: string) {
    if (!chatId) return;
    addCard("notes", text);
    setTab("notes");
  }
  // Manually create a blank doc/note, opened straight into edit mode.
  function newCard(kind: "docs" | "notes") {
    if (!chatId) return;
    // Straight into the editor pane — a brand-new empty doc opens in edit
    // mode there (the card list is now read/preview only).
    const id = addCard(kind, "");
    setTab(kind);
    openEditor(kind, { id, content: "" });
  }
  // Copy a note into docs so the AI can reference it (the note stays too).
  function saveNoteAsDoc(item: DocItem) {
    if (!chatId) return;
    addCard("docs", item.content);
    setTab("docs");
  }

  // Edit a user message and roll the conversation back to that point, then resend.
  async function saveEdit() {
    if (!editing || !chatId) return;
    const { index, text } = editing;
    const trimmed = text.trim();
    setEditing(null);
    if (!trimmed) return;
    const kept = messages.slice(0, index); // everything before the edited turn
    patchRt(chatId, { messages: kept, suggestions: [] });
    try { await api.putMessages(chatId, kept); } catch { /* best-effort */ }
    await runTurn(chatId, trimmed);
  }

  // Answer a pending clarifying question; the open stream resumes on the backend.
  async function answerAsk(cid: string, ask: PendingAsk, answer: string) {
    if (!answer.trim()) return;
    patchRt(cid, { pendingAsk: null });
    setAskDraft("");
    try {
      await api.answer(ask.id, answer.trim());
    } catch {
      /* if it fails, the backend ask times out and the agent proceeds on its own */
    }
  }

  const effRight: TabId | undefined = rightTabs.includes(actRight) ? actRight : rightTabs[0];
  const effBottom: TabId | undefined = bottomTabs.includes(actBottom) ? actBottom : bottomTabs[0];

  // A draggable tab strip for a dock zone. Doubles as a drop target: dropping ON a
  // tab reorders before it, dropping on empty strip space appends to that zone.
  function dockStrip(zone: "right" | "bottom", tabs: TabId[], active: TabId | undefined) {
    const dropHere = (before: TabId | null) => {
      const t = dragTab.current;
      if (!t) return;
      moveTab(t, zone);        // may be a no-op if it's already in this zone
      reorderTab(t, before);
      endDrag();
    };
    return (
      <div
        className={"tabs" + (dropTarget === `zone:${zone}` ? " drop-zone" : "")}
        onDragOver={(e) => { e.preventDefault(); setDropTarget(`zone:${zone}`); }}
        onDragLeave={() => setDropTarget((d) => (d === `zone:${zone}` ? null : d))}
        onDrop={(e) => { e.preventDefault(); dropHere(null); }}
      >
        {tabs.map((t) => (
          <button
            key={t}
            draggable
            title="Drag to reorder, or onto the other panel to move it there"
            className={
              "tab" + (t === active ? " active" : "") +
              (heldTab === t ? " held" : "") +
              (dropTarget === `tab:${t}` ? " drop-before" : "")
            }
            onClick={() => activate(t)}
            onDoubleClick={() => moveTab(t, zone === "right" ? "bottom" : "right")}
            onDragStart={(e) => {
              dragTab.current = t;
              setHeldTab(t);
              setDragging(true);
              e.dataTransfer.effectAllowed = "move";
              // Firefox refuses to start a drag without payload; harmless elsewhere.
              e.dataTransfer.setData("text/plain", t);
            }}
            onDragEnd={endDrag}
            onDragOver={(e) => {
              e.preventDefault();
              e.stopPropagation();   // a tab is a finer target than its strip
              if (dragTab.current && dragTab.current !== t) setDropTarget(`tab:${t}`);
            }}
            onDragLeave={() => setDropTarget((d) => (d === `tab:${t}` ? null : d))}
            onDrop={(e) => { e.preventDefault(); e.stopPropagation(); dropHere(t); }}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>
    );
  }

  // --- Eorzea Database tab ---
  // `dbUrl` is the page being viewed; setting it (from the tab's own search, or from
  // a link clicked anywhere in the app) is what drives the tab.
  const [dbKind, setDbKind] = useState<DbKind>("item");
  const [dbQuery, setDbQuery] = useState("");
  const [dbHits, setDbHits] = useState<DbHit[]>([]);
  const [dbUrl, setDbUrl] = useState<string>("");
  const [dbItem, setDbItem] = useState<DbItem | null>(null);
  // (nodeWatchId + its sync effect live below, after dbDoc is declared.)
  // Non-item records (duty, quest, NPC, fate…) render from this uniform shape.
  const [dbDoc, setDbDoc] = useState<DbDetailDoc | null>(null);
  const [dbBusy, setDbBusy] = useState(false);
  // The open node's overlay watch id ("" = not watched). Derived from the real
  // watch list on detail open, so the button is a true TOGGLE: click to watch,
  // click again to stop.
  const [nodeWatchId, setNodeWatchId] = useState("");
  useEffect(() => {
    setNodeWatchId("");
    if (dbDoc?.kind !== "node" || !dbDoc.id) return;
    api.overlayWatches()
      .then((r) => {
        const w = r.watches.find((x) => x.kind === "node" && x.ref === dbDoc.id);
        if (w) setNodeWatchId(w.id);
      })
      .catch(() => {});
  }, [dbDoc]); // eslint-disable-line react-hooks/exhaustive-deps
  const [dbErr, setDbErr] = useState("");
  // --- Browse tool (the tab's empty state, like Garland's own Browse) ---
  const [browseKind, setBrowseKind] = useState("instance");
  const [browseData, setBrowseData] = useState<Record<string, DbBrowse>>({});
  const [browseBusy, setBrowseBusy] = useState(false);
  const [browseOpen, setBrowseOpen] = useState<string>("");   // expanded group label

  async function loadBrowse(kind: string) {
    setBrowseKind(kind);
    setBrowseOpen("");
    if (browseData[kind]) return;
    setBrowseBusy(true);
    try {
      const b = await api.dbBrowse(kind);
      setBrowseData((d) => ({ ...d, [kind]: b }));
    } catch { /* the pane shows search + chips regardless */ }
    setBrowseBusy(false);
  }

  /** "Add to Assets" on an agent-shown (temporary) chat image: the backend
   *  copies tmp_x → x, and the shelf picks the copy up. */
  async function keepChatAsset(name: string) {
    if (!chatId) return;
    const r = await api.keepAsset(chatId, name);
    patchRt(chatId, (rt) =>
      rt.assets.includes(r.asset_id) ? {} : { assets: [...rt.assets, r.asset_id] });
  }

  // First time the tab is on screen with nothing open: load the default
  // catalogue so the resting state is a browse tool, not an empty note.
  const dbVisible = (dock["eorzeadb"] === "bottom" ? actBottom : actRight) === "eorzeadb";
  useEffect(() => {
    if (dbVisible && !dbHits.length && !dbItem && !browseData[browseKind] && !browseBusy)
      loadBrowse(browseKind);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dbVisible]);

  async function runDbSearch(q: string, kind: DbKind) {
    if (!q.trim()) return;
    setDbBusy(true); setDbErr(""); setDbItem(null); setDbDoc(null); setDbUrl("");
    try {
      const r = await api.dbSearch(q, kind);
      setDbHits(r.hits);
      if (r.hits.length === 0) setDbErr(`Nothing in the ${kind} database matches “${q}”.`);
    } catch (e) {
      setDbErr(String(e).includes("503") ? WAF_HINT : String(e));
    }
    setDbBusy(false);
  }

  /** Open one database page in the tab. Called by the tab's result list AND by the
   *  global link handler, which is what makes an Eorzea DB link in a chat reply or a
   *  guide land here instead of in the browser. */
  // Open a zone on the rebuilt in-game map, optionally pinning one gathering node.
  // switchTab=false is for the boot preload, which must not steal the active tab.
  // focus (2048-space) centres the view. `pin` shows a TEMPORARY marker: the agent
  // supplies map-space coords, chat map-links supply in-game flag coords ("game")
  // which convert once the zone's size factor is known. Every call replaces the
  // previous temp pin, so navigating away naturally drops it.
  // The toolbar group a KEPT pin lands in: every gathering-type icon folds into
  // one "gathering" group (the ex the feature was asked with); any other icon
  // names its own group; no icon = a plain "My pins" pin.
  const GATHER_ICONS = new Set(["mining", "quarrying", "logging", "harvesting",
                                "fishing", "spearfishing"]);
  const kindForIcon = (icon?: string) =>
    !icon ? "" : GATHER_ICONS.has(icon) ? "gathering" : icon;

  async function openZoneMap(zone: string, nodeId = "", switchTab = true,
                             focus?: { x: number; y: number } | null,
                             pin?: { x: number; y: number; label: string; space: "map" | "game";
                                     icon?: string; r?: number; radius_px?: number } | null,
                             group?: { category: string; icon?: string; space: "map" | "game";
                                       pins: { x: number; y: number; label?: string }[] } | null) {
    if (switchTab) setTab("map");
    setMapImg("");        // a stale pinned image would otherwise win over the loader
    setGameMap(null);
    setMapErr("");
    setMapErrDetail("");
    setTempPin(null);
    setTempGroup(null);
    setMapBusy(zone);
    try {
      const z = await api.zoneMap(zone, nodeId);
      if (group && group.pins.length) {
        // A CATEGORY of temp pins ("all the aether currents"): convert each,
        // centre the view on the set's bounding box.
        const toMap = (v: number) =>
          ((v - 1) * (z.size_factor || 100)) / 100 / 41 * (z.coord_space || 2048);
        const pts = group.pins.map((p) => ({
          x: group.space === "game" ? toMap(p.x) : p.x,
          y: group.space === "game" ? toMap(p.y) : p.y,
          label: p.label || "",
        }));
        setTempGroup({ category: group.category, icon: group.icon, pins: pts });
        if (!focus) {
          focus = { x: pts.reduce((s, p) => s + p.x, 0) / pts.length,
                    y: pts.reduce((s, p) => s + p.y, 0) / pts.length };
        }
      }
      if (pin) {
        const toMap = (v: number) =>
          ((v - 1) * (z.size_factor || 100)) / 100 / 41 * (z.coord_space || 2048);
        // Length (not point) conversion for an area radius — no -1 origin shift.
        const pxPerCoord = ((z.size_factor || 100) / 100) / 41 * (z.coord_space || 2048);
        const radiusPx = pin.space === "game"
          ? (pin.r ? pin.r * pxPerCoord : undefined)
          : pin.radius_px || undefined;
        const p = pin.space === "game"
          ? { x: toMap(pin.x), y: toMap(pin.y), label: pin.label, icon: pin.icon, radiusPx,
              kind: kindForIcon(pin.icon) }
          : { x: pin.x, y: pin.y, label: pin.label, icon: pin.icon, radiusPx,
              kind: kindForIcon(pin.icon) };
        setTempPin(p);
        if (!focus) focus = { x: p.x, y: p.y };
      } else if (nodeId && z.node) {
        // A GarlandDB node link: the backend hands the node back as `node` (not
        // as a marker — that drew the same label twice). Show it as the usual
        // TEMPORARY pin, typed so 📌 Keep can save it under Custom – Gathering.
        setTempPin({ x: z.node.x, y: z.node.y, label: z.node.label,
                     icon: z.node.icon, kind: z.node.kind || kindForIcon(z.node.icon) });
      }
      if (focus) z.focus = focus;
      setGameMap(z);
      // Store the CANONICAL name the backend resolved, not the link's spelling —
      // it's what the region/zone dropdowns match against on reopen.
      localStorage.setItem("lastMapZone", z.zone || zone);
    } catch (e) {
      if (String(e).startsWith("Error: 404")) {
        // Not a drawable zone (a dungeon interior, or a stale lastMapZone from
        // before the picker was filtered). Forget it and land on the picker.
        if (localStorage.getItem("lastMapZone") === zone) {
          localStorage.removeItem("lastMapZone");
        }
      } else {
        // A real failure (cold cache, network blip). Say so and offer a retry —
        // on a cold start the first load builds the zone index and pulls a ~2.5MB
        // texture, and one slow request must not strand the whole tab.
        setMapErr(zone);
        setMapErrDetail(`zone data: ${String(e).slice(0, 120)}`);
      }
    }
    setMapBusy("");
  }

  // First time the Map tab is actually ON SCREEN with nothing loaded: fetch the
  // drawable-zone list for the picker and reopen the last-viewed zone, so the tab
  // lands on the rebuilt map — not the old ARR iframe — without a manual pick.
  // Gating on visibility matters: auto-loading at mount would call setTab("map")
  // and hijack whatever tab the user opened the app on.
  const mapVisible = effRight === "map" || effBottom === "map";
  useEffect(() => {
    if (!mapVisible) return;
    // Fetch the zone list for the picker AND the map bar's dropdowns — and keep
    // refetching until a COMPLETE list lands. Caching a truncated one for the
    // session is how Yak T'el once vanished from the picker until restart.
    if (!zoneRegions.length || !zonesComplete.current) {
      api.mapZones().then((r) => {
        setZoneRegions(r.regions);
        zonesComplete.current = r.complete !== false;
        // Open the picker on the region you were last in, like the in-game map.
        const last = localStorage.getItem("lastMapZone");
        const home = last && r.regions.find((g) => g.zones.includes(last));
        setZoneRegion((home || r.regions[0])?.region || "");
      }).catch(() => {});
    }
    if (gameMap || mapBusy || mapImg) return;
    // The boot sequence normally preloads a zone; this covers the first open in a
    // dev browser, where boot skipped it.
    if (!mapAutoLoaded.current) {
      mapAutoLoaded.current = true;
      openZoneMap(localStorage.getItem("lastMapZone") || DEFAULT_MAP_ZONE, "", false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapVisible, gameMap, mapBusy, mapImg]);

  async function openDbUrl(url: string) {
    // Route by the TYPE in the hash — #instance/35007 is a duty, not item 35007.
    // (Treating every number as an item id is how "A Chorus Slime" once rendered
    // as a necklace that shared its id.)
    const m = /#(\w+)\/([\w.]+)/.exec(url);
    if (m && m[1] !== "item") {
      openDbKind(m[1], m[2]);
      return;
    }
    setTab("eorzeadb");
    setDbUrl(url); setDbItem(null); setDbDoc(null); setDbErr(""); setDbBusy(true);
    try {
      const d = await api.dbItem(url);
      setDbItem(d);
      if (!d.found) setDbErr("");   // handled in the view: offer the official page
    } catch (e) {
      setDbErr(String(e).includes("503") ? WAF_HINT : String(e));
    }
    setDbBusy(false);
  }

  /** Open one record of any kind — from a search hit, a browse row, or a
   *  cross-reference inside another record. */
  async function openDbKind(kind: string, id: string) {
    if (kind === "item") {
      openDbUrl(`https://garlandtools.org/db/#item/${id}`);
      return;
    }
    setTab("eorzeadb");
    setDbUrl(`https://garlandtools.org/db/#${kind}/${id}`);
    setDbItem(null); setDbDoc(null); setDbErr(""); setDbBusy(true);
    try {
      setDbDoc(await api.dbDetail(kind, id));
    } catch (e) {
      setDbErr(String(e).includes("503") ? WAF_HINT : String(e));
    }
    setDbBusy(false);
  }

  // --- A doc's side thread ("subchat") ---
  // Keyed by "kind:id" so every open doc keeps its own conversation, and it survives
  // a restart because the backend stores it on the chat.
  const [docAsk, setDocAsk] = useState<{ busy: boolean; status: string }>({
    busy: false, status: "",
  });
  const [threads, setThreads] = useState<Record<string, Message[]>>({});

  async function loadThread(kind: "docs" | "notes", id: string) {
    if (!chatId) return;
    const k = `${kind}:${id}`;
    try {
      const r = await api.subchat(chatId, kind, id);
      setThreads((t) => ({ ...t, [k]: r.messages }));
    } catch { /* no thread yet */ }
  }

  async function clearThread(kind: "docs" | "notes", id: string) {
    if (!chatId) return;
    const k = `${kind}:${id}`;
    try { await api.clearSubchat(chatId, kind, id); } catch { /* ignore */ }
    setThreads((t) => ({ ...t, [k]: [] }));
  }

  /** One turn of a doc's side thread. The agent edits via its update_doc tool and
   *  replies conversationally — that reply is what lands in the thread. */
  async function askDocEdit(kind: "docs" | "notes", id: string, instruction: string) {
    if (!chatId || !model) return;
    const k = `${kind}:${id}`;
    // Show the question straight away; the backend is the source of truth and we
    // re-read the thread when the turn ends.
    setThreads((t) => ({ ...t, [k]: [...(t[k] || []), { role: "user", content: instruction }] }));
    setDocAsk({ busy: true, status: "Reading the doc…" });
    try {
      await api.editDoc(chatId, kind, id, instruction, model, auth, (ev) => {
        if (ev.type === "tool") {
          setDocAsk({ busy: true, status: `${stepLabel(ev.name, ev.args)}…` });
        } else if (ev.type === "doc_edited") {
          setEditorContent(kind, id, ev.content);
          setRuntimes((r) => {
            const cur = r[chatId!] || {};
            const list = ((cur[kind] as DocItem[]) || []).map((d) =>
              d.id === id ? { ...d, content: ev.content, draft: false } : d);
            return { ...r, [chatId!]: { ...cur, [kind]: list } };
          });
        } else if (ev.type === "error") {
          alert(ev.message);
        }
      });
    } catch (e) {
      alert(String(e));
    }
    setDocAsk({ busy: false, status: "" });
    await loadThread(kind, id);   // pull the persisted thread back, reply included
  }

  // --- Doc/Note editor tabs (center pane) ---
  const editorKey = (t: { kind: string; id: string }) => `${t.kind}:${t.id}`;

  /** Reorder the open doc/note tabs by dragging one onto another. These live in one
   *  column, so there's no dock to change — only position. */
  function reorderEditorTab(fromKey: string | null, beforeKey: string) {
    if (!fromKey || fromKey === beforeKey) return;
    setEditorTabs((tabs) => {
      const from = tabs.find((x) => editorKey(x) === fromKey);
      if (!from) return tabs;
      const next = tabs.filter((x) => editorKey(x) !== fromKey);
      const at = next.findIndex((x) => editorKey(x) === beforeKey);
      if (at < 0) next.push(from);
      else next.splice(at, 0, from);
      return next;
    });
  }
  function openEditor(kind: "docs" | "notes", item: DocItem) {
    loadThread(kind, item.id);   // bring back this doc's conversation
    setEditorTabs((tabs) =>
      tabs.some((t) => t.kind === kind && t.id === item.id) ? tabs : [...tabs, { kind, id: item.id }],
    );
    setActiveTab(`${kind}:${item.id}`);
  }
  // Open a backend-persisted file (agent preferences / a character profile) in the
  // editor — same surface the player already uses for docs, so editing is familiar.
  async function openFileEditor(id: string) {
    const isPrefs = id === "prefs";
    const slug = isPrefs ? "" : id.slice("profile:".length);
    try {
      const content = isPrefs ? await api.getPreferences() : await api.getWsProfile(slug);
      const title = isPrefs ? "Agent Preferences"
        : `Profile — ${workspaces.find((w) => w.slug === slug)?.display_name || slug}`;
      setFileDocs((m) => ({ ...m, [id]: { title, content } }));
      setEditorTabs((tabs) =>
        tabs.some((t) => t.kind === "file" && t.id === id) ? tabs : [...tabs, { kind: "file", id }]);
      setActiveTab(`file:${id}`);
    } catch { /* backend not up — nothing to open */ }
  }
  function saveFileDoc(id: string) {
    const d = fileDocs[id];
    if (!d) return;
    (id === "prefs"
      ? api.putPreferences(d.content)
      : api.putWsProfile(id.slice("profile:".length), d.content)
    ).catch(() => {});
  }
  function closeEditor(t: { kind: "docs" | "notes" | "file"; id: string }) {
    const k = editorKey(t);
    setEditorTabs((tabs) => tabs.filter((x) => editorKey(x) !== k));
    setActiveTab((a) => (a === k ? "chat" : a));
  }
  // The tab shows the doc's own title when it has one (the AI sets it via create_doc,
  // and it's editable in the editor); otherwise fall back to the first line.
  function editorTabTitle(t: { kind: "docs" | "notes" | "file"; id: string }) {
    if (t.kind === "file") return fileDocs[t.id]?.title || "File";
    const it = (t.kind === "docs" ? docs : notes).find((x) => x.id === t.id);
    if (!it) return "(deleted)";
    return (it.title || "").trim() || cardTitle(it.content);
  }
  function setEditorContent(kind: "docs" | "notes", id: string, md: string) {
    const items = (runtimes[chatId]?.[kind] as DocItem[]) || [];
    const next = items.map((x) => (x.id === id ? { ...x, content: md } : x));
    patchRt(chatId, { [kind]: next } as Partial<ChatRuntime>);
    // Commit immediately, not just on blur: in the editor's READ mode a
    // checkbox tick changes content but the non-editable area never blurs,
    // so a blur-only save would silently drop ticks.
    if (chatId) (kind === "docs" ? api.putDocs : api.putNotes)(chatId, next).catch(() => {});
  }
  function setEditorTitle(kind: "docs" | "notes", id: string, title: string) {
    patchRt(chatId, (r) => ({
      [kind]: (r[kind] as DocItem[]).map((x) => (x.id === id ? { ...x, title } : x)),
    }) as Partial<ChatRuntime>);
  }
  function setEditorShared(kind: "docs" | "notes", id: string, shared: boolean) {
    patchRt(chatId, (r) => ({
      [kind]: (r[kind] as DocItem[]).map((x) => (x.id === id ? { ...x, shared } : x)),
    }) as Partial<ChatRuntime>);
    window.setTimeout(() => persistCards(kind), 0); // let the patch land, then save
  }
  // Assets are files, so "shared" is tracked on the chat rather than the file.
  function toggleAssetShared(name: string, shared: boolean) {
    if (!chatId) return;
    patchRt(chatId, (r) => ({
      sharedAssets: shared
        ? [...r.sharedAssets, name]
        : r.sharedAssets.filter((n) => n !== name),
    }));
    api.setAssetShared(chatId, name, shared).catch(() => {});
  }
  // Which doc is showing on the in-game overlay's checklist widget ("" = none).
  const [overlayDoc, setOverlayDoc] = useState("");
  useEffect(() => {
    api.checklistGet().then((c) => setOverlayDoc(c.pinned ? c.doc_id || "" : ""))
      .catch(() => {});
  }, []);
  /** Pin this doc's checklist to the overlay, or unpin it if it's already the
   *  pinned one. Only docs are pinnable — notes are private. */
  async function pinDocToOverlay(kind: "docs" | "notes", id: string) {
    if (kind !== "docs" || !chatId) return;
    try {
      if (overlayDoc === id) {
        await api.checklistUnpin();
        setOverlayDoc("");
      } else {
        await api.checklistPin(chatId, id);
        setOverlayDoc(id);
      }
    } catch { /* backend away — leave the button as it was */ }
  }

  function persistCards(kind: "docs" | "notes") {
    const items = (runtimes[chatId]?.[kind] as DocItem[]) || [];
    if (chatId) (kind === "docs" ? api.putDocs : api.putNotes)(chatId, items).catch(() => {});
  }
  // An explicit Save in the editor is the review step: it persists AND clears
  // the Draft badge (implicit blur-saves keep the badge — see persistCards).
  function saveEditorDoc(kind: "docs" | "notes", id: string) {
    const items = (runtimes[chatId]?.[kind] as DocItem[]) || [];
    const next = items.map((x) => (x.id === id ? { ...x, draft: false } : x));
    patchRt(chatId, { [kind]: next } as Partial<ChatRuntime>);
    if (chatId) (kind === "docs" ? api.putDocs : api.putNotes)(chatId, next).catch(() => {});
  }
  // Draft-doc links, rendered inline under the assistant message that produced
  // them (and under the live streaming block for the in-progress turn).
  function renderDocLinks(links: { id: string; title: string; draft: boolean }[]) {
    if (!links.length) return null;
    return (
      <div className="doc-links">
        {links.map((d, i) => (
          <button
            key={d.id + i}
            className="doc-link"
            onClick={() => {
              const item = (runtimes[chatId]?.docs || []).find((x) => x.id === d.id);
              if (item) openEditor("docs", item);
              setTab("docs");
            }}
          >
            <span className="doc-link-ico">📄</span>
            <span className="doc-link-title">{d.title}</span>
            {d.draft && <span className="doc-badge">Draft</span>}
            <span className="doc-link-cta">Review →</span>
          </button>
        ))}
      </div>
    );
  }
  /** The editor for one tab key. Parameterised (rather than reading activeTab) so
   *  split view can show a doc on the right while the chat stays on the left. */
  function editorNode(key: string = activeTab) {
    const ref = editorTabs.find((t) => editorKey(t) === key);
    if (!ref) return null;
    if (ref.kind === "file") {
      const d = fileDocs[ref.id];
      if (!d) return <div className="editor-missing muted">Loading…</div>;
      return (
        <Editor
          docKey={key}
          markdown={d.content}
          title={d.title}
          onChange={(md) => setFileDocs((m) => ({ ...m, [ref.id]: { ...m[ref.id], content: md } }))}
          onBlur={() => saveFileDoc(ref.id)}
          onSave={() => saveFileDoc(ref.id)}
        />
      );
    }
    // Narrowed binding: closures below capture this, and TS can't carry the
    // "not file" narrowing of ref.kind into them on its own.
    const kind = ref.kind as "docs" | "notes";
    const item = (kind === "docs" ? docs : notes).find((x) => x.id === ref.id);
    if (!item) return <div className="editor-missing muted">This item was deleted.</div>;
    return (
      <Editor
        docKey={key}
        markdown={item.content}
        chatId={chatId}
        title={(item.title || "").trim() || cardTitle(item.content)}
        onTitleChange={(t) => setEditorTitle(kind, ref.id, t)}
        shared={!!item.shared}
        onSharedChange={(s) => setEditorShared(kind, ref.id, s)}
        onChange={(md) => setEditorContent(kind, ref.id, md)}
        onBlur={() => persistCards(kind)}
        onSave={() => saveEditorDoc(kind, ref.id)}
        pinnedToOverlay={overlayDoc === ref.id}
        onPinToOverlay={() => pinDocToOverlay(kind, ref.id)}
        onImageClick={(name) => { setLightbox(name); setLbZoom(false); }}
        onAsk={(instruction) => askDocEdit(kind, ref.id, instruction)}
        askBusy={docAsk.busy}
        askStatus={docAsk.status}
        thread={threads[`${kind}:${ref.id}`] || []}
        onClearThread={() => clearThread(kind, ref.id)}
      />
    );
  }
  // Drop editor tabs whose doc/note was deleted, and fall back to Chat if needed.
  useEffect(() => {
    setEditorTabs((tabs) => {
      const kept = tabs.filter((t) =>
        t.kind === "file" ||   // files aren't chat cards; deletion cleanup can't apply
        (t.kind === "docs" ? docs : notes).some((x) => x.id === t.id));
      return kept.length === tabs.length ? tabs : kept;
    });
  }, [docs, notes]);
  useEffect(() => {
    if (activeTab !== "chat" && !editorTabs.some((t) => editorKey(t) === activeTab)) setActiveTab("chat");
  }, [editorTabs, activeTab]);

  // Renders a tab's content (shared between the right and bottom dock zones).
  function renderTab(t: TabId) {
    if (t === "eorzeadb")
      return (
        <div className="db-pane">
          <form
            className="db-search"
            onSubmit={(e) => { e.preventDefault(); runDbSearch(dbQuery, dbKind); }}
          >
            <KindSelect
              value={dbKind}
              onChange={(k) => {
                setDbKind(k);
                if (dbQuery.trim()) runDbSearch(dbQuery, k);
              }}
            />
            <input
              className="db-q"
              value={dbQuery}
              placeholder="Search the database…"
              onChange={(e) => {
                setDbQuery(e.target.value);
                // Clearing the box returns to the browse view IMMEDIATELY —
                // stale results used to sit there until the next search.
                if (!e.target.value.trim()) {
                  setDbHits([]);
                  setDbErr("");
                }
              }}
            />
            <button className="db-go" type="submit" disabled={dbBusy || !dbQuery.trim()}>
              {dbBusy ? "…" : "Search"}
            </button>
          </form>

          {dbErr && <div className="db-err">{dbErr}</div>}

          {/* Detail view for the open page, else the result list. */}
          {dbItem ? (
            <div className="db-detail">
              <button className="db-back" onClick={() => { setDbItem(null); setDbDoc(null); setDbUrl(""); }}>
                ← Results
              </button>
              {dbItem.found ? (
                <>
                  <div className="db-head">
                    {dbItem.icon && <img className="db-icon" src={dbItem.icon} alt="" />}
                    <div className="db-head-text">
                      <div className="db-name">{dbItem.name}</div>
                      <div className="db-sub">
                        {dbItem.category}
                        {dbItem.item_level && ` · Item Level ${dbItem.item_level}`}
                      </div>
                    </div>
                  </div>
                  {dbItem.description && <p className="db-desc">{dbItem.description}</p>}
                  {!!dbItem.attributes && Object.keys(dbItem.attributes).length > 0 && (
                    <div className="db-attrs">
                      {Object.entries(dbItem.attributes).map(([k, v]) => (
                        <span key={k} className="db-attr">
                          <span className="db-attr-k">{k}</span>
                          <span className="db-attr-v">{v}</span>
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Sources & Uses — the half of a database entry that answers
                      "how do I get one?". */}
                  <div className="db-sources">
                    <div className="db-sec-h">Sources &amp; Uses</div>

                    {dbItem.tradeable === false ? (
                      <div className="db-row muted">
                        <span className="db-row-ico">🚫</span> Untradable — no market board
                      </div>
                    ) : dbItem.market ? (
                      <div className="db-row">
                        <span className="db-row-ico">🏷</span>
                        <span className="db-row-main">
                          Market <b>{dbItem.market.lowest.toLocaleString()}</b> gil
                          {dbItem.market.world && ` · ${dbItem.market.world}`}
                          <span className="db-row-sub">
                            {" "}({dbItem.market.listings} listings on {dbItem.market.world_or_dc})
                          </span>
                        </span>
                      </div>
                    ) : (
                      <div className="db-row muted">
                        <span className="db-row-ico">🏷</span> No current listings
                      </div>
                    )}

                    {!!dbItem.sell_price && (
                      <div className="db-row">
                        <span className="db-row-ico">🪙</span>
                        Sells to vendors for <b>{dbItem.sell_price.toLocaleString()}</b> gil
                      </div>
                    )}

                    {dbItem.nodes?.map((n) => (
                      <div className="db-row" key={n.id}>
                        <span className="db-row-ico">⛏</span>
                        <span className="db-row-main">
                          Gathering:{" "}
                          <button className="db-zone-link" title="Open this node's database entry"
                                  onClick={() => openDbKind("node", n.id)}>
                            <b>{n.name}</b>
                          </button>
                          {/* The zone opens the rebuilt in-game map, pinned on THIS
                              node by its id. Every zone is covered now, so it's always
                              a link — no more dead-link gating. */}
                          {n.zone && (
                            <>
                              {" in "}
                              <button className="db-zone-link"
                                      title={`Open ${n.zone} on the map, pinned here`}
                                      onClick={() => openZoneMap(n.zone!, n.id)}>
                                {n.zone} 🗺
                              </button>
                            </>
                          )}
                          <span className="db-row-sub"> · Lv {n.level}{n.type && ` ${n.type}`}</span>
                        </span>
                      </div>
                    ))}

                    {!!dbItem.ventures?.length && (
                      <div className="db-row">
                        <span className="db-row-ico">🧭</span>
                        Retainer venture ({dbItem.ventures.length})
                      </div>
                    )}

                    {dbItem.vendors?.filter((v) => v.name).map((v) => (
                      <div className="db-row" key={v.id}>
                        <span className="db-row-ico">🛒</span> Vendor:{" "}
                        <button className="db-zone-link"
                                title="Open this NPC's database entry (location, quests)"
                                onClick={() => openDbKind("npc", v.id)}>
                          <b>{v.name}</b>
                        </button>
                      </div>
                    ))}

                    {dbItem.ingredient_of?.map((g) => (
                      <div className="db-row" key={g.id}>
                        <span className="db-row-ico">⚒</span>
                        <span className="db-row-main">
                          Ingredient in{" "}
                          <button className="db-zone-link" title="Open this item"
                                  onClick={() => openDbKind("item", g.id)}>
                            <b>{g.name}</b>
                          </button>
                          {!!g.qty && <span className="db-row-sub"> ×{g.qty}</span>}
                        </span>
                      </div>
                    ))}

                    {/* The progression chain — the thing the official DB couldn't answer. */}
                    {dbItem.upgrades?.filter((u) => u.name).map((u) => (
                      <div className="db-row" key={u.id}>
                        <span className="db-row-ico">⬆</span>
                        <span className="db-row-main">
                          Upgrades to{" "}
                          <button className="db-zone-link" title="Open this item"
                                  onClick={() => openDbKind("item", u.id)}>
                            <b>{u.name}</b>
                          </button>
                          {!!u.item_level && <span className="db-row-sub"> · i{u.item_level}</span>}
                        </span>
                      </div>
                    ))}
                    {dbItem.downgrades?.filter((u) => u.name).map((u) => (
                      <div className="db-row" key={u.id}>
                        <span className="db-row-ico">⬇</span>
                        <span className="db-row-main">
                          Replaces{" "}
                          <button className="db-zone-link" title="Open this item"
                                  onClick={() => openDbKind("item", u.id)}>
                            <b>{u.name}</b>
                          </button>
                          {!!u.item_level && <span className="db-row-sub"> · i{u.item_level}</span>}
                        </span>
                      </div>
                    ))}
                  </div>
                  {!!dbItem.comments?.length && (
                    <div className="db-comments">
                      <div className="db-comments-h">Player comments ({dbItem.comments.length})</div>
                      {dbItem.comments.map((c, i) => (
                        <div className="db-comment" key={i}>
                          <div className="db-comment-meta">{c.author}{c.date && ` · ${c.date}`}</div>
                          <div className="db-comment-text">{c.text}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                // Only item pages are parsed in depth — duty/quest/shop pages have a
                // different layout, so hand those to the official site rather than
                // render a half-empty panel.
                <div className="db-unparsed">
                  This page isn’t one the app renders in detail yet.
                  <a href={dbItem.url} target="_blank" rel="noreferrer" data-external>
                    Open on Garland Tools ↗
                  </a>
                </div>
              )}
              <div className="db-footer">
                <a className="db-official" href={dbUrl || dbItem.url} target="_blank"
                   rel="noreferrer" data-external>
                  View on Garland Tools ↗
                </a>
              </div>
            </div>
          ) : dbDoc ? (
            // Non-item record (duty, quest, NPC, fate, leve, node…): one uniform
            // layout — header, description, facts, a map link when the record has
            // a place in the world, and clickable cross-references.
            <div className="db-detail">
              <button className="db-back" onClick={() => { setDbDoc(null); setDbUrl(""); }}>
                ← Back
              </button>
              {dbDoc.found ? (
                <>
                  <div className="db-head">
                    {(dbDoc.icon || dbDoc.icon_name) && (
                      <img className="db-icon"
                           src={dbDoc.icon || api.iconByName(dbDoc.icon_name!)} alt=""
                           onError={retryImg} />
                    )}
                    <div className="db-head-text">
                      <div className="db-name">{dbDoc.name}</div>
                      {dbDoc.sub && <div className="db-sub">{dbDoc.sub}</div>}
                    </div>
                  </div>
                  {/* Big art when Garland has it: NPC portrait render, duty banner. */}
                  {dbDoc.image && (
                    <img className="db-photo" src={dbDoc.image} alt="" loading="lazy"
                         onError={retryImg} />
                  )}
                  {dbDoc.description && <p className="db-desc">{dbDoc.description}</p>}
                  {!!dbDoc.fields?.length && (
                    <div className="db-attrs">
                      {dbDoc.fields.map((f) => (
                        <span key={f.label} className="db-attr">
                          <span className="db-attr-k">{f.label}</span>
                          <span className="db-attr-v">{f.value}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  {(() => {
                    const loc = dbDoc.location;
                    if (!loc) return null;
                    return (
                      <div className="db-row">
                        <span className="db-row-ico">📍</span>
                        <span className="db-row-main">
                          {loc.label && loc.label !== dbDoc.name ? `${loc.label} — ` : ""}
                          <button className="db-zone-link"
                                  title={`Open ${loc.zone} on the map` +
                                         (loc.x ? ", pinned here (temporary)" : "")}
                                  onClick={() => openZoneMap(loc.zone, "", true, null,
                                    loc.x ? { x: loc.x, y: loc.y, label: loc.label || dbDoc.name || "",
                                              space: "game", icon: loc.icon,
                                              r: loc.radius || undefined } : null)}>
                            {loc.zone} 🗺
                          </button>
                          {loc.x ? (
                            <span className="db-row-sub"> ({loc.x.toFixed(1)}, {loc.y.toFixed(1)})</span>
                          ) : null}
                        </span>
                      </div>
                    );
                  })()}
                  {/* Gathering nodes can live on the in-game overlay as a chip —
                      timed nodes get a spawn countdown (the backend derives the
                      windows from the node id). A true toggle: click again to
                      stop watching. */}
                  {dbDoc.kind === "node" && (
                    <div className="db-row">
                      <span className="db-row-ico">⏱</span>
                      <span className="db-row-main">
                        <button
                          className="db-zone-link"
                          title={nodeWatchId
                            ? "Click to stop showing this node on the in-game overlay"
                            : "Show this node as a chip on the in-game overlay"}
                          onClick={async () => {
                            try {
                              if (nodeWatchId) {
                                await api.overlayWatchRemove(nodeWatchId);
                                setNodeWatchId("");
                              } else {
                                const r = await api.overlayWatchAdd(
                                  { kind: "node", ref: dbDoc.id, label: "" });
                                setNodeWatchId(r.watch.id);
                              }
                            } catch { /* backend away — leave the button */ }
                          }}
                        >
                          {nodeWatchId ? "Watching on overlay ✓ (click to stop)" : "Watch on overlay"}
                        </button>
                      </span>
                    </div>
                  )}
                  {dbDoc.links?.map((g) => (
                    <div className="db-sources" key={g.group}>
                      <div className="db-sec-h">{g.group}</div>
                      <div className="db-reflist">
                        {g.refs.map((r) => (
                          <button key={r.kind + r.id} className="db-ref"
                                  onClick={() => openDbKind(r.kind, r.id)}>
                            {r.icon ? (
                              <img className="db-ref-icon" src={r.icon} alt="" loading="lazy"
                                   onError={retryImg} />
                            ) : (
                              <span>{kindIcon(r.kind)}</span>
                            )}
                            {" "}{r.name}
                            {r.sub && <span className="db-row-sub"> · {r.sub}</span>}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </>
              ) : (
                <div className="db-unparsed">
                  This page isn’t one the app renders in detail yet.
                  <a href={dbDoc.url} target="_blank" rel="noreferrer" data-external>
                    Open on Garland Tools ↗
                  </a>
                </div>
              )}
              <div className="db-footer">
                <a className="db-official" href={dbDoc.url} target="_blank"
                   rel="noreferrer" data-external>
                  View on Garland Tools ↗
                </a>
              </div>
            </div>
          ) : (
            <div className="db-hits">
              {/* Catalogue chips stay at the top in BOTH states — during a
                  result list they double as the way back to browsing (one
                  click clears the search and opens that catalogue). */}
              <div className="db-browse-chips">
                {BROWSE_UI.map((b) => (
                  <button key={b.id}
                          className={"db-chip" +
                            (browseKind === b.id && !dbHits.length ? " on" : "")}
                          onClick={() => {
                            setDbHits([]);
                            setDbErr("");
                            setDbQuery("");
                            loadBrowse(b.id);
                          }}>
                    <TypeIcon id={b.id} emoji={b.icon} /> {b.label}
                  </button>
                ))}
              </div>
              {dbHits.map((h) => (
                <button key={h.url} className="db-hit" onClick={() => openDbUrl(h.url)}>
                  {h.icon ? (
                    <img className="db-hit-icon" src={h.icon} alt="" loading="lazy" />
                  ) : (
                    <span className="db-hit-icon db-hit-glyph">{kindIcon(h.type)}</span>
                  )}
                  <span className="db-hit-name">{h.name}</span>
                  {!!h.item_level && <span className="db-hit-ilvl">i{h.item_level}</span>}
                </button>
              ))}
              {!dbHits.length && !dbBusy && (
                // The tab's resting state is a BROWSE TOOL, like Garland's own:
                // pick a catalogue, expand a group, click a record.
                <div className="db-browse">
                  {browseBusy && !browseData[browseKind] && (
                    <div className="muted small">Loading…</div>
                  )}
                  {browseData[browseKind]?.groups.map((g) => (
                    <div key={g.label} className="db-group">
                      <button className="db-group-h"
                              onClick={() => setBrowseOpen((o) => (o === g.label ? "" : g.label))}>
                        <span className="db-group-arrow">{browseOpen === g.label ? "▾" : "▸"}</span>
                        <span className="db-group-label">{g.label}</span>
                        <span className="db-group-count">{g.count}</span>
                      </button>
                      {browseOpen === g.label && (
                        <div className="db-group-rows">
                          {g.rows.map((r) => (
                            <button key={r.id} className="db-browse-row"
                                    onClick={() => openDbKind(browseKind, r.id)}>
                              {r.icon && (
                                <img className="db-browse-ico" src={r.icon} alt=""
                                     loading="lazy"
                                     onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />
                              )}
                              <span className="db-browse-name">{r.name}</span>
                              {r.sub && <span className="db-row-sub">{r.sub}</span>}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      );
    if (t === "browser") {
      // The native browser view paints ON TOP of the app's webview, so it must
      // yield (hide) whenever a modal would overlap it — the pane component
      // handles the hiding; this just tells it when.
      const shown =
        (effRight === "browser" || effBottom === "browser") &&
        !settingsOpen && !searchOpen && !lightbox;
      return <BrowserPane visible={shown} req={browserReq} />;
    }
    if (t === "map") {
      // Primary: the rebuilt in-game map (every zone incl. Dawntrail). The picker is
      // the tab's empty state; the ARR iframe appears ONLY when a zone load failed.
      if (gameMap)
        return (
          <GameMap map={gameMap} onClose={() => setGameMap(null)}
                   regions={zoneRegions}
                   onOpenZone={(z, focus) => openZoneMap(z, "", true, focus)}
                   tempPin={tempPin}
                   onTempPinKept={() => setTempPin(null)}
                   tempGroup={tempGroup}
                   onTempGroupKept={() => setTempGroup(null)}
                   onTextureError={() => {
                     // The texture failed to decode mid-view — surface the error +
                     // Retry rather than leave markers floating on nothing. Probe
                     // the same URL with fetch so the card can say WHAT failed.
                     const z = gameMap.zone;
                     const tex = gameMap.texture;
                     setGameMap(null);
                     setMapErr(z);
                     setMapErrDetail("texture: probing…");
                     fetch(tex)
                       .then((r) => setMapErrDetail(
                         `texture: HTTP ${r.status}${r.ok ? " (fetch ok — image decode failed)" : ""}`))
                       .catch((e) => setMapErrDetail(`texture fetch failed: ${String(e).slice(0, 100)}`));
                   }}
                   // Screenshot → asset in the CURRENT chat, same shelf as the
                   // agent's pinned maps. No open chat = no shelf, so no button.
                   onSaveShot={chatId ? async (blob, caption) => {
                     const r = await api.uploadAsset(
                       chatId, blob, caption || `${gameMap.zone} map`);
                     patchRt(chatId, (rt) => rt.assets.includes(r.asset_id)
                       ? {} : { assets: [...rt.assets, r.asset_id] });
                   } : undefined} />
        );
      // The nav dropdowns stay on top through loading AND failure — an error must
      // never strand the player without a way to open a different zone.
      if (mapBusy)
        return (
          <>
            <MapNavBar regions={zoneRegions} current={mapBusy} onOpenZone={openZoneMap} />
            <div className="map-loading">
              <div className="map-spinner" />
              <div>Loading {mapBusy}…</div>
              <div className="muted small">
                First visit fetches the map once — after that it loads from disk.
              </div>
            </div>
          </>
        );
      if (mapErr)
        return (
          <>
            <MapNavBar regions={zoneRegions} current={mapErr} onOpenZone={openZoneMap} />
            <div className="map-loading">
              <div>Couldn’t load the map for <b>{mapErr}</b>.</div>
              <div className="muted small">Usually a brief network hiccup on first fetch.</div>
              {mapErrDetail && <div className="muted small map-err-detail">{mapErrDetail}</div>}
              <div className="map-err-actions">
                <button className="map-toggle" onClick={() => openZoneMap(mapErr)}>Retry</button>
                <button className="map-toggle" onClick={() => setMapErr("")}>Choose another zone</button>
              </div>
            </div>
          </>
        );
      if (mapImg)
        return (
          <>
            <div className="map-zone">
              📍 {mapImgZone || "Pinned location"}
              {mapImgZone && (
                <button className="map-toggle" onClick={() => openZoneMap(mapImgZone)}>
                  Interactive map
                </button>
              )}
            </div>
            <div className="map-img-wrap">
              <img className="map-img" src={mapImg} alt={mapImgZone || "Pinned map"} />
            </div>
            <div className="map-credit">Pin placed by exact coordinates</div>
          </>
        );
      // Region-first navigation, like the in-game map: pick a region, get its
      // zones. Typing in the search overrides the region and looks everywhere.
      const zq = zoneQuery.trim().toLowerCase();
      const activeRegion = zoneRegions.find((g) => g.region === zoneRegion) || zoneRegions[0];
      const zoneHits = zq
        ? zoneRegions.flatMap((g) => g.zones).filter((z) => z.toLowerCase().includes(zq))
        : activeRegion?.zones || [];
      return (
        <div className="zp">
          <div className="zp-controls">
            <select className="zp-region" value={activeRegion?.region || ""}
                    onChange={(e) => { setZoneRegion(e.target.value); setZoneQuery(""); }}>
              {zoneRegions.map((g) => (
                <option key={g.region} value={g.region}>{g.region}</option>
              ))}
            </select>
            <input className="zp-search" placeholder="Search all zones…"
                   value={zoneQuery} onChange={(e) => setZoneQuery(e.target.value)} />
          </div>
          <div className="zp-list">
            {zoneHits.map((z) => (
              <button key={z} className="zp-zone" onClick={() => openZoneMap(z)}>🗺 {z}</button>
            ))}
            {!zoneRegions.length && <div className="muted small">Loading zones…</div>}
            {!!zoneRegions.length && !zoneHits.length && (
              <div className="muted small">No zone matches “{zoneQuery}”.</div>
            )}
          </div>
        </div>
      );
    }
    if (t === "sources")
      return sources.length ? (
        sources.map((s, i) => {
          // Nearly every source here is a volunteer project handing us its data for
          // free. When one has a funding page, put it one click from the citation.
          const support = matchSource(s, sourceCatalog)?.support;
          return (
            // A div, not an <a>: the support link nests inside, and <a> inside <a>
            // is invalid HTML — the browser silently unnests it and the card breaks.
            <div key={i} className="source-card">
              <a className="source-main" href={s.url} target="_blank">
                <div className="source-label">{s.label}</div>
                <div className="source-url">{s.url}</div>
              </a>
              {support && (
                <a className="source-support" href={support} target="_blank" data-external
                   title={`Support ${s.label} — this app reads their data for free`}>
                  ♥ Support
                </a>
              )}
            </div>
          );
        })
      ) : (
        <div className="muted small">Sources cited in this chat appear here.</div>
      );
    if (t === "assets")
      return assets.length ? (
        <div className="asset-gallery">
          {assets.map((name) => (
            <div key={name} className="asset-card">
              <img
                src={api.assetUrl(chatId, name)}
                alt={name}
                title="Click to expand"
                onClick={() => { setLightbox(name); setLbZoom(false); }}
              />
              {sharedAssets.includes(name) && <span className="asset-shared">shared</span>}
              <div className="asset-actions">
                <button title="Expand" onClick={() => { setLightbox(name); setLbZoom(false); }}>⤢</button>
                <button title="Annotate" onClick={() => setAnnotating(name)}>✎</button>
                <label
                  className="asset-share"
                  title="Share across your profiles (stays in this chat, findable from any profile)"
                  onClick={(e) => e.stopPropagation()}
                >
                  <input
                    type="checkbox"
                    checked={sharedAssets.includes(name)}
                    onChange={(e) => toggleAssetShared(name, e.target.checked)}
                  />
                </label>
                <a title="Open in browser" href={api.assetUrl(chatId, name)} target="_blank">↗</a>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="muted small">Annotated images and maps for this chat appear here.</div>
      );
    if (t === "docs")
      return (
        <div className="panel-cards">
          <div className="panel-cards-head">
            <span className="panel-hint">Docs are markdown cards the AI can reference.</span>
            <button className="card-add" onClick={() => newCard("docs")} disabled={!chatId}>＋ New doc</button>
          </div>
          <CardList
            items={docs}
            onChange={(it) => patchRt(chatId, { docs: it })}
            onCommit={(it) => { if (chatId) api.putDocs(chatId, it).catch(() => {}); }}
            onOpen={(item) => openEditor("docs", item)}
            chatId={chatId}
            emptyText="Save a reply as a doc, or add one — they show up here as cards."
          />
        </div>
      );
    return (
      <div className="panel-cards">
        <div className="panel-cards-head">
          <span className="panel-hint">Notes are private — the AI never reads them.</span>
          <button className="card-add" onClick={() => newCard("notes")} disabled={!chatId}>＋ Add note</button>
        </div>
        <CardList
          items={notes}
          onChange={(it) => patchRt(chatId, { notes: it })}
          onCommit={(it) => { if (chatId) api.putNotes(chatId, it).catch(() => {}); }}
          onOpen={(item) => openEditor("notes", item)}
          promote={saveNoteAsDoc} promoteLabel="Save as doc"
          chatId={chatId}
          emptyText="Add a note, or send a reply here — notes show up as cards."
        />
      </div>
    );
  }

  // The single live status line: whatever the agent is doing RIGHT NOW. Prefers the
  // running tool, then "writing" once tokens arrive, then the step just finished (so
  // the line doesn't blank out between tool calls).
  const runningStep = activity.find((a) => a.ok === undefined);
  const lastStep = activity.length ? activity[activity.length - 1] : undefined;
  const statusText = runningStep
    ? `${stepLabel(runningStep.tool, runningStep.args)}…`
    : draft
      ? "Writing response…"
      : lastStep
        ? `${stepLabel(lastStep.tool, lastStep.args)} ✓`
        : "Thinking…";

  // Split only makes sense with a doc to put beside the chat. Which doc: the active
  // tab, or — when you're sitting on the Chat tab — the first one open, so toggling
  // split always shows something rather than an empty half.
  const splitDocKey =
    activeTab !== "chat" ? activeTab : (editorTabs[0] ? editorKey(editorTabs[0]) : "");
  const splitOn = splitView && !!splitDocKey;

  // The chat itself, extracted so split view can render it beside a doc rather
  // than instead of one.
  const chatPane = (
    <>
          {/* Subtle Eorzean-compass watermark, centered behind the chat. Uses the
              theme text color at low opacity, so it adapts to every theme. */}
          <div className="chat-watermark" aria-hidden="true">
            <svg viewBox="0 0 240 240" fill="none" stroke="currentColor" strokeWidth={4} strokeLinejoin="round">
              <circle cx="120" cy="120" r="106" strokeWidth={3} opacity={0.5} />
              <path d="M120 22 L142 98 L218 120 L142 142 L120 218 L98 142 L22 120 L98 98 Z" />
              <g strokeWidth={2.5} opacity={0.6}>
                <path d="M120 120 L168 72" /><path d="M120 120 L168 168" />
                <path d="M120 120 L72 168" /><path d="M120 120 L72 72" />
              </g>
            </svg>
          </div>
          <div className="messages" ref={scrollRef}>
            {messages.length === 0 && !streaming && (
              <div className="empty">
                Ask about mechanics, gear, prices, lore, or what's new in the game.
                {!activeModel?.available && (
                  <div className="warn">
                    Setup API keys in <b>Settings › API keys &amp; models</b>.
                  </div>
                )}
              </div>
            )}
            {messages.map((m, i) =>
              editing?.index === i ? (
                <div key={i} className="msg-row user">
                  <div className="msg user editing">
                    <textarea
                      className="edit-area"
                      value={editing.text}
                      autoFocus
                      onChange={(e) => setEditing({ index: i, text: e.target.value })}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdit();
                        if (e.key === "Escape") setEditing(null);
                      }}
                    />
                    <div className="edit-actions">
                      <button className="edit-save" onClick={saveEdit}>Save &amp; resend</button>
                      <button className="edit-cancel" onClick={() => setEditing(null)}>Cancel</button>
                    </div>
                  </div>
                </div>
              ) : (
                <div key={i} className={"msg-row " + m.role}>
                  <div className={"msg " + m.role}>
                    {m.role === "assistant" ? <Markdown text={m.content} chatId={chatId} onImageClick={setLightbox} onKeepAsset={keepChatAsset} shelf={assets} /> : m.content}
                  </div>
                  {m.role === "assistant" && m.docLinks && renderDocLinks(m.docLinks)}
                  <div className="msg-actions">
                    <button title="Copy" onClick={() => copyText(m.content)}>⧉ Copy</button>
                    <button title="Save as a doc the AI can reference" onClick={() => saveToDocs(m.content)}>＋ Doc</button>
                    <button title="Add to your private notes" onClick={() => addToNotes(m.content)}>＋ Note</button>
                    {m.role === "user" && (
                      <button
                        title="Edit & roll back from here"
                        disabled={streaming}
                        onClick={() => setEditing({ index: i, text: m.content })}
                      >
                        ✎ Edit
                      </button>
                    )}
                  </div>
                </div>
              ),
            )}
            {/* While streaming, the assistant message isn't finalized yet, so
                show this turn's draft-doc links here; on finalize they attach to
                the message and render inline above. */}
            {streaming && renderDocLinks(docLinks)}
            {!streaming && !editing && suggestions.length > 0 && (
              <div className="suggestions">
                {suggestions.map((s, i) => (
                  <button key={i} className="suggestion" onClick={() => sendSuggestion(s)}>
                    {s}
                  </button>
                ))}
              </div>
            )}
            {streaming && (
              <div className="msg assistant streaming">
                {/* ONE line that overwrites itself with wherever the agent is right
                    now — not an ever-growing list of every tool call it made. */}
                {!pendingAsk && (
                  <div className="status-line">
                    <span className="spinner" />
                    <span className="status-text">{statusText}</span>
                  </div>
                )}
                {draft && <div className="draft"><Markdown text={draft} chatId={chatId} onImageClick={setLightbox} onKeepAsset={keepChatAsset} shelf={assets} /></div>}
                {pendingAsk ? (
                  <div className="ask-card">
                    {pendingAsk.header && <div className="ask-header">{pendingAsk.header}</div>}
                    <div className="ask-q">{pendingAsk.question}</div>
                    <div className="ask-opts">
                      {pendingAsk.options.map((opt, i) => (
                        <button
                          key={i}
                          className="ask-opt"
                          onClick={() => answerAsk(chatId!, pendingAsk, opt)}
                        >
                          {opt}
                        </button>
                      ))}
                    </div>
                    <form
                      className="ask-form"
                      onSubmit={(e) => { e.preventDefault(); answerAsk(chatId!, pendingAsk, askDraft); }}
                    >
                      <input
                        className="ask-input"
                        placeholder="Or type your own answer…"
                        value={askDraft}
                        autoFocus
                        onChange={(e) => setAskDraft(e.target.value)}
                      />
                      <button className="ask-send" type="submit" disabled={!askDraft.trim()}>Send</button>
                    </form>
                  </div>
                ) : null}
              </div>
            )}
          </div>

          <div className="composer">
            <div className="composer-box">
              <input
                ref={fileInputRef} type="file" multiple hidden
                onChange={(e) => { uploadFiles(e.target.files); e.target.value = ""; }}
              />
              <input
                ref={folderInputRef} type="file" multiple hidden
                onChange={(e) => { uploadFiles(e.target.files); e.target.value = ""; }}
              />
              {attachments.length > 0 && (
                <div className="attach-chips">
                  {attachments.map((a) => (
                    <span key={a.name} className={"attach-chip k-" + a.kind}>
                      <span className="attach-ico">
                        {a.kind === "image" ? "🖼" : a.kind === "pdf" ? "📄" : a.kind === "text" ? "📝" : "📎"}
                      </span>
                      <span className="attach-name">{a.name}</span>
                      <button className="attach-x" onClick={() => removeAttachment(a.name)}>✕</button>
                    </span>
                  ))}
                </div>
              )}
              <textarea
                value={input}
                placeholder="Ask about mechanics, gear, prices, lore…"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              <div className="composer-bar">
                <button
                  className="attach-btn" title="Attach files or photos"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <IconPaperclip />
                </button>
                <button
                  className="attach-btn" title="Attach a folder"
                  onClick={() => folderInputRef.current?.click()}
                >
                  <IconFolder />
                </button>
                <span className="model-anchor">
                  <button className="composer-model" onClick={() => setPickerOpen((o) => !o)}>
                    {activeModel ? activeModel.label : "No model"}
                    {activeModel?.available && (
                      <span className="pill-auth">{auth === "subscription" ? "sub" : "API"}</span>
                    )}{" "}
                    ▴
                  </button>
                  {pickerOpen && (
                    <ModelPicker
                      up
                      models={models}
                      current={model}
                      currentAuth={auth}
                      onPick={(id, a) => {
                        setModel(id);
                        setAuth(a);
                        setPickerOpen(false);
                      }}
                      onManage={() => {
                        setPickerOpen(false);
                        setSettingsOpen(true);
                      }}
                    />
                  )}
                </span>
                <span className="ctx-meter" title="Approximate context used for this chat">
                  <span className="ctx-bar">
                    <span className="ctx-fill" style={{ width: `${ctxPct}%` }} />
                  </span>
                  ~{fmtK(ctxUsed)} / {fmtK(ctxMax)}
                </span>
                {/* Cost so far. On the subscription nothing is billed per token —
                    show what the chat WOULD cost at API prices, marked "incl."
                    so the number reads as value covered, not a charge. */}
                {auth === "subscription" ? (
                  <span
                    className="cost-meter included"
                    title={
                      "What this chat would have cost at API prices — covered by " +
                      "your Claude Pro/Max subscription, you pay nothing per token.\n" +
                      "Approximate: character-based token counts, each turn re-sends " +
                      "the system prompt and history."
                    }
                  >
                    {chatCost !== null ? `≈${fmtCost(chatCost)} incl.` : "included"}
                  </span>
                ) : chatCost !== null ? (
                  <span
                    className="cost-meter"
                    title={
                      "Rough estimate of what this chat has cost on your API key.\n" +
                      "Counts each turn re-sending the system prompt and the whole " +
                      "history, which is where most of the cost is.\n" +
                      "Approximate: character-based token counts, and it ignores " +
                      "prompt caching, so the real bill is usually lower."
                    }
                  >
                    ≈{fmtCost(chatCost)}
                  </span>
                ) : null}
                <div className="spacer" />
                <label className="ignore-profile"
                       title="Answer without your character profile in context — general answers, not ones tailored to your jobs and gear. Preferences still apply.">
                  <input type="checkbox" checked={ignoreProfile}
                         onChange={(e) => setIgnoreProfile(e.target.checked)} />
                  ignore my profile
                </label>
                <button
                  className={"mic" + (listening ? " on" : "")}
                  title="Dictate (voice to text)"
                  onClick={toggleDictation}
                >
                  <IconMic />
                </button>
                {streaming ? (
                  <button className="send stop" title="Stop the response"
                          onClick={() => chatAbort.current?.abort()}>
                    ■
                  </button>
                ) : (
                  <button className="send" disabled={!canSend} onClick={send}>
                    ↑
                  </button>
                )}
              </div>
            </div>
          </div>
    </>
  );

  return (
    <div className="app">
      {/* Header */}
      <header className="topbar">
        <span className="brand">
          <img className="brand-icon" src="/brand-icon.png" alt="" /> Aether Intelligence
        </span>
        <div className="spacer" />
        <button
          className="find-btn"
          title="Search docs, notes & assets"
          onClick={() => setSearchOpen(true)}
        >
          🔍 <span className="find-btn-text">Search docs, notes &amp; assets</span>
        </button>
      </header>

      <div
        className="body"
        style={{
          gridTemplateColumns:
            `${leftW}px 6px minmax(${MIN_CHAT_W}px,1fr) 6px minmax(0,${rightW}px)`,
        }}
      >
        {/* History sidebar */}
        <aside className="sidebar">
          <div className="ws-switcher">
            <select className="ws-select" value={activeWs} onChange={(e) => switchWs(e.target.value)}>
              {workspaces.map((w) => (
                <option key={w.slug} value={w.slug}>
                  ● {w.display_name}
                </option>
              ))}
            </select>
            <button className="ws-new" title="New character profile" onClick={newProfile}>+</button>
          </div>
          <button className="new-chat" onClick={newChat}>
            + New chat
          </button>
          <div className="recent-label">{activeWsMeta?.display_name || "Recent"}</div>
          <div className="chat-list">
            {scopedChats.length === 0 && <div className="muted small">No chats yet</div>}
            {scopedChats.map(chatRow)}
          </div>
          {/* In-game overlay chats live at the BOTTOM, folded away — they're a
              running log of play-time questions, not something to scroll past
              to reach your real chats. */}
          {overlayChats.length > 0 && (
            <div className="ov-section">
              <button
                className="ov-section-h"
                onClick={() => setOverlayChatsOpen((o) => !o)}
                title={overlayChatsOpen ? "Hide overlay chats" : "Show overlay chats"}
              >
                <span className="ov-section-arrow">{overlayChatsOpen ? "▾" : "▸"}</span>
                ✦ Overlay
                <span className="ov-section-count">{overlayChats.length}</span>
              </button>
              {overlayChatsOpen && (
                <div className="chat-list">{overlayChats.map(chatRow)}</div>
              )}
            </div>
          )}
          <UsageMeter />
          {/* Plain anchors: the delegated link interceptor routes both into the
              in-app browser tab, same as every other external link. */}
          <a className="support-me" href="https://ko-fi.com/missayanight" data-external>♥ Support Me</a>
          {updateReady && (
            <button
              className="upd-banner"
              title={`Version ${updateReady.version} is available — open Settings to install it`}
              onClick={() => { setUpdateReady(null); setSettingsOpen(true); }}
            >
              ⬆ Update {updateReady.version} available
            </button>
          )}
          <button className="settings-btn" onClick={() => setSettingsOpen(true)}>
            ⚙ Settings
          </button>
          <div className="made-by">
            Created by{" "}
            <a href="https://na.finalfantasyxiv.com/lodestone/character/20920231/">Aya Night</a>
          </div>
        </aside>

        <div className="gutter" onPointerDown={startDrag("left")} />

        {/* Chat column */}
        <main className="chat">
          {editorTabs.length > 0 && (
            <div className="center-tabs">
              <button
                className={"ct" + (activeTab === "chat" ? " active" : "")}
                onClick={() => setActiveTab("chat")}
              >
                Chat
              </button>
              {editorTabs.map((t) => {
                const k = editorKey(t);
                return (
                  <button
                    key={k}
                    draggable
                    title="Drag to reorder"
                    className={
                      "ct" + (activeTab === k ? " active" : "") +
                      (heldEditorTab === k ? " held" : "") +
                      (dropTarget === `ct:${k}` ? " drop-before" : "")
                    }
                    onClick={() => setActiveTab(k)}
                    onDragStart={(e) => {
                      dragEditorTab.current = k;
                      setHeldEditorTab(k);
                      e.dataTransfer.effectAllowed = "move";
                      e.dataTransfer.setData("text/plain", k);
                    }}
                    onDragEnd={() => { dragEditorTab.current = null; setHeldEditorTab(null); setDropTarget(null); }}
                    onDragOver={(e) => {
                      e.preventDefault();
                      if (dragEditorTab.current && dragEditorTab.current !== k) setDropTarget(`ct:${k}`);
                    }}
                    onDragLeave={() => setDropTarget((d) => (d === `ct:${k}` ? null : d))}
                    onDrop={(e) => {
                      e.preventDefault();
                      reorderEditorTab(dragEditorTab.current, k);
                      dragEditorTab.current = null;
                      setHeldEditorTab(null);
                      setDropTarget(null);
                    }}
                  >
                    <span className="ct-kind">{t.kind === "docs" ? "◆" : "✎"}</span>
                    <span className="ct-label">{editorTabTitle(t)}</span>
                    <span className="ct-close" onClick={(e) => { e.stopPropagation(); closeEditor(t); }}>×</span>
                  </button>
                );
              })}
              {/* Split the centre column: chat on the left, the open doc on the
                  right. Only offered when a doc is actually open — a split with
                  nothing in it is just a narrower chat. */}
              <button
                className={"ct-split" + (splitView ? " on" : "")}
                title={splitView ? "Close split view" : "Split: chat beside the doc"}
                onClick={() => setSplitView((v) => !v)}
              >
                ⫿
              </button>
            </div>
          )}
          {splitOn ? (
            <div
              className="chat-split"
              style={{ gridTemplateColumns: `minmax(0,1fr) 6px ${splitW}px` }}
            >
              <div className="split-chat">{chatPane}</div>
              <div
                className="vgutter"
                title="Drag to resize"
                onPointerDown={() => { vgutter.current = true; document.body.style.cursor = "col-resize"; }}
              />
              <div className="split-doc">{editorNode(splitDocKey)}</div>
            </div>
          ) : activeTab !== "chat" ? (
            editorNode()
          ) : (
            chatPane
          )}

          {/* Bottom dock zone (VS Code-style panel; drag tabs here) */}
          {bottomTabs.length > 0 && (
            <>
              <div
                className="hgutter"
                onPointerDown={() => { hgutter.current = true; document.body.style.cursor = "row-resize"; }}
              />
              <div className="dock-bottom" style={{ height: bottomH }}>
                {dockStrip("bottom", bottomTabs, effBottom)}
                <div className={"tab-body" + (effBottom === "map" || effBottom === "browser" ? " nopad" : "")}>
                  {effBottom && renderTab(effBottom)}
                </div>
              </div>
            </>
          )}
          {dragging && bottomTabs.length === 0 && (
            <div
              className="dock-dropzone"
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => { if (dragTab.current) moveTab(dragTab.current, "bottom"); setDragging(false); }}
            >
              Drop here to dock at the bottom
            </div>
          )}
        </main>

        <div className="gutter" onPointerDown={startDrag("right")} />

        {/* Reference panel (right dock zone) */}
        <aside className="refpanel">
          {rightTabs.length > 0 ? (
            <>
              {dockStrip("right", rightTabs, effRight)}
              <div className={"tab-body" + (effRight === "map" || effRight === "browser" ? " nopad" : "")}>
                {effRight && renderTab(effRight)}
              </div>
            </>
          ) : (
            <div
              className="dock-empty"
              onDragOver={(e) => e.preventDefault()}
              onDrop={() => { if (dragTab.current) moveTab(dragTab.current, "right"); }}
            >
              Drag a tab here
            </div>
          )}
        </aside>
      </div>

      {searchOpen && (
        <SearchModal
          activeWs={activeWs}
          onClose={() => setSearchOpen(false)}
          onOpenHit={async (hit) => {
            setSearchOpen(false);
            await openChat(hit.chat_id);
            if (hit.kind === "asset") { setTab("assets"); return; }
            setTab(hit.kind === "doc" ? "docs" : "notes");
          }}
        />
      )}

      {settingsOpen && (
        <Settings
          onClose={() => setSettingsOpen(false)}
          onSaved={reloadModels}
          theme={theme}
          onTheme={setTheme}
          density={density}
          onDensity={setDensity}
          refreshOnStart={refreshOnStart}
          onRefreshOnStart={setRefreshOnStart}
          closeToTray={closeToTray}
          onCloseToTray={changeCloseToTray}
          autoCheckUpdates={autoCheckUpdates}
          onAutoCheckUpdates={setAutoCheckUpdates}
          autoInstallUpdates={autoInstallUpdates}
          onAutoInstallUpdates={setAutoInstallUpdates}
          overlayKeepOpen={overlayKeepOpen}
          onOverlayKeepOpen={setOverlayKeepOpen}
          overlayHotkeys={overlayHotkeys}
          onOverlayHotkeys={applyOverlayHotkeys}
          models={models}
          model={model}
          auth={auth}
          onPickModel={(id, a) => { setModel(id); setAuth(a); }}
          onSaveSettings={saveSettingsNow}
          fontScale={fontScale}
          onFont={bumpFont}
          onFontReset={() => setFontScale(1)}
          overlayScale={overlayScale}
          onOverlayScale={bumpOverlayScale}
          onOverlayReset={() => setOverlayScale(1)}
          activeWs={activeWs}
          workspace={activeWsMeta}
          onWsChanged={refreshWorkspaces}
          onWsDeleted={() => { setActiveWs(""); refreshWorkspaces(); refreshChats(); setSettingsOpen(false); }}
          onOpenFile={(id) => { setSettingsOpen(false); openFileEditor(id); }}
        />
      )}

      {annotating && (
        <AnnotationEditor
          chatId={chatId}
          assetName={annotating}
          onClose={() => setAnnotating(null)}
          onExported={(n) => {
            setAnnotating(null);
            patchRt(chatId, (r) => (r.assets.includes(n) ? {} : { assets: [...r.assets, n] }));
            setTab("assets");
          }}
        />
      )}

      {lightbox && (
        <div className="lightbox" onClick={() => { setLightbox(null); setLbZoom(false); }}>
          <div className="lightbox-bar" onClick={(e) => e.stopPropagation()}>
            <button onClick={() => setLbZoom((z) => !z)}>{lbZoom ? "Fit to screen" : "Actual size"}</button>
            {/* Jump from a picture embedded in a guide to that same file in Assets —
                the doc shows it in context, Assets is where you manage/share it. */}
            <button
              title="Reveal this image in the Assets tab"
              onClick={() => { setTab("assets"); setLightbox(null); setLbZoom(false); }}
            >
              🖼 Show in Assets
            </button>
            <a href={api.assetUrl(chatId, lightbox)} target="_blank" rel="noreferrer">Open in browser ↗</a>
            <button onClick={() => { setLightbox(null); setLbZoom(false); }}>✕ Close</button>
          </div>
          <div className={"lightbox-scroll" + (lbZoom ? " zoom" : "")} onClick={(e) => e.stopPropagation()}>
            <img
              src={api.assetUrl(chatId, lightbox)}
              alt=""
              title={lbZoom ? "Click to fit" : "Click to zoom"}
              onClick={() => setLbZoom((z) => !z)}
            />
          </div>
        </div>
      )}
    </div>
  );

  // Settings already write through on change; this is the explicit "Save settings"
  // action so the player gets a definite confirmation that they're stored.
  async function saveSettingsNow() {
    await api.putAppSettings({
      theme, density, fontScale, overlayScale, activeWs, leftW, rightW, bottomH, dock,
      refresh_profile_on_start: refreshOnStart,
      defaultModel: model, defaultAuth: auth,
      splitView, splitW,
    });
  }

  async function reloadModels() {
    try {
      const r = await api.models();
      setModels(r.models);
        setSystemTokens(r.system_tokens || 0);
      // If the current selection just became unavailable (key/token removed) or
      // nothing is selected, snap to the server's default.
      const stillOk = r.models.find((m) => m.id === model)?.available;
      if ((!model || !stillOk) && r.default) {
        setModel(r.default.id);
        setAuth(r.default.auth);
      }
    } catch {
      /* leave the current selection; the Settings panel surfaces its own errors */
    }
    // WebView2 can skip repainting state updates that land while a modal is open,
    // so a freshly enabled model wouldn't appear until the next interaction (which
    // read like "needs an app restart"). Force a composite so it updates now.
    nudgeRepaint();
  }
}

function ModelPicker(props: {
  models: Model[];
  current: string;
  currentAuth: Auth;
  onPick: (id: string, auth: Auth) => void;
  onManage: () => void;
  up?: boolean;
}) {
  const byProvider: Record<string, Model[]> = {};
  for (const m of props.models) (byProvider[m.provider_label] ||= []).push(m);
  const authLabel = (a: Auth) => (a === "subscription" ? "subscription" : "API key");
  return (
    <div className={"picker" + (props.up ? " up" : "")}>
      <div className="picker-head">Model — credentials stored in your OS keychain</div>
      {Object.entries(byProvider).map(([label, ms]) => (
        <div key={label}>
          <div className="picker-group">{label}</div>
          {ms.map((m) =>
            m.available ? (
              // One selectable row per available auth mode (subscription / API).
              m.auth_options.map((a) => (
                <div
                  key={m.id + a}
                  className="picker-item"
                  onClick={() => props.onPick(m.id, a)}
                >
                  <span>
                    {m.id === props.current && a === props.currentAuth ? "● " : ""}
                    {m.label}
                    <span className="auth-note"> · {authLabel(a)}</span>
                  </span>
                  {m.recommended && a === m.default_auth && (
                    <span className="tag">recommended</span>
                  )}
                </div>
              ))
            ) : (
              <div key={m.id} className="picker-item locked">
                <span>{m.label}</span>
                <span className="tag add" onClick={props.onManage}>
                  + add key
                </span>
              </div>
            ),
          )}
        </div>
      ))}
    </div>
  );
}

// Per-workspace profile editor + Lodestone character binding.
function WorkspaceSettings({ slug, workspace, onChanged, onDeleted, onOpenFile }: {
  slug: string;
  workspace: Workspace | undefined;
  onChanged: () => void;
  onDeleted: () => void;
  onOpenFile: (id: string) => void;
}) {
  const isProfile = workspace?.kind === "profile";
  const [profile, setProfile] = useState("");
  const [bound, setBound] = useState<BoundCharacter | null>(null);
  const [query, setQuery] = useState("");
  const [world, setWorld] = useState("");
  const [results, setResults] = useState<CharacterHit[]>([]);
  const [busy, setBusy] = useState("");

  const reloadProfile = () => api.getWsProfile(slug).then(setProfile);
  useEffect(() => {
    setResults([]); setQuery("");
    api.getWsProfile(slug).then(setProfile).catch(() => {});
    if (isProfile) api.getWsCharacter(slug).then((r) => setBound(r.character)).catch(() => {});
    else setBound(null);
  }, [slug]); // eslint-disable-line react-hooks/exhaustive-deps

  async function find() {
    const q = query.trim();
    if (!q) return;
    setBusy("find");
    try {
      if (/\/character\/\d+/.test(q) || /^\d+$/.test(q)) {
        const c = await api.charBind(slug, /^\d+$/.test(q) ? { id: q } : { url: q });
        setBound(c); setResults([]); setQuery(""); await reloadProfile(); onChanged();
      } else {
        const r = await api.charSearch(slug, q, world.trim());
        setResults(r.results);
        if (!r.results.length) alert("No characters found. Try adding the home world.");
      }
    } catch (e) { alert(String(e)); }
    setBusy("");
  }
  async function bindId(id: string) {
    setBusy("bind");
    try {
      const c = await api.charBind(slug, { id });
      setBound(c); setResults([]); setQuery(""); await reloadProfile(); onChanged();
    } catch (e) { alert(String(e)); }
    setBusy("");
  }
  async function refresh() {
    setBusy("refresh");
    try { setBound(await api.charRefresh(slug)); await reloadProfile(); }
    catch (e) { alert(String(e)); }
    setBusy("");
  }
  async function del() {
    if (!confirm(`Delete profile “${workspace?.display_name}”? Its chats move to your other profile — they aren't deleted.`)) return;
    await api.deleteWorkspace(slug);
    onDeleted();
  }

  return (
    <div className="ws-settings">
      {isProfile && (
        <>
          <div className="section-label">Bound character</div>
          {bound ? (
            <div className="bound-char">
              <span>{bound.name} · {bound.world} [{bound.data_center}]</span>
              <button disabled={busy === "refresh"} onClick={refresh}>
                {busy === "refresh" ? "Refreshing…" : "↻ Refresh"}
              </button>
            </div>
          ) : (
            <div className="muted small">No character bound. Paste a Lodestone link or search by name.</div>
          )}
          <div className="bind-form">
            <input
              placeholder="Lodestone URL, id, or character name"
              value={query} onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && find()}
            />
            <input className="world-in" placeholder="World (optional)" value={world}
              onChange={(e) => setWorld(e.target.value)} />
            <button disabled={busy === "find" || !query.trim()} onClick={find}>
              {busy === "find" ? "…" : "Find"}
            </button>
          </div>
          {results.length > 0 && (
            <div className="char-results">
              {results.map((h) => (
                <button key={h.id} className="char-hit" disabled={busy === "bind"} onClick={() => bindId(h.id)}>
                  {h.name} · {h.world}
                </button>
              ))}
            </div>
          )}
        </>
      )}
      <div className="section-label">{isProfile ? "Profile (this character)" : "General profile"}</div>
      <div className="prefs-row">
        <span className="toggle-hint">
          {profile.trim()
            ? profile.trim().split("\n")[0].replace(/^#+\s*/, "").slice(0, 80)
            : "Goals, playstyle, preferences…"}
        </span>
        <button className="open-file-btn" onClick={() => onOpenFile(`profile:${slug}`)}>
          ✎ Open in editor
        </button>
      </div>
      {isProfile && <button className="ws-delete" onClick={del}>Delete this profile</button>}
    </div>
  );
}

// Everything armed on the in-game overlay (chips) — see it all, stop any of
// it. Lives in Settings under "Background & overlay"; the same list the
// overlay's own ✕ buttons and the arm points write to.
function OverlayWatchList() {
  const [watches, setWatches] = useState<OverlayWatch[]>([]);
  useEffect(() => {
    api.overlayWatches().then((r) => setWatches(r.watches)).catch(() => {});
  }, []);
  const kindMark = (w: OverlayWatch) =>
    w.kind === "node" ? "⏱" : w.kind === "pinset" ? `📍×${w.pins?.length || 0}` : "📍";
  return (
    <div className="ovw-list">
      <span className="toggle-name">Watching on overlay</span>
      {watches.length === 0 ? (
        <span className="toggle-hint">
          Nothing armed. Arm chips from an answer card's "Arm chips", a map
          pin's "⏱ Watch", or a node page's "Watch on overlay".
        </span>
      ) : (
        watches.map((w) => (
          <div key={w.id} className="ovw-row">
            <span className="ovw-kind">{kindMark(w)}</span>
            <span className="ovw-label">{w.label}</span>
            {w.zone && <span className="ovw-zone">{w.zone}</span>}
            <button
              className="ovw-x"
              title="Stop watching"
              onClick={() => {
                setWatches((ws) => ws.filter((x) => x.id !== w.id));
                api.overlayWatchRemove(w.id).catch(() => {});
              }}
            >
              ✕
            </button>
          </div>
        ))
      )}
    </div>
  );
}

/** Check GitHub Releases, download the installer, run it. Download and install
 * stay two explicit steps unless "install automatically" is on — this replaces
 * the running program, so it shouldn't happen by surprise. */
function UpdatePanel(props: {
  autoCheck: boolean;
  onAutoCheck: (v: boolean) => void;
  autoInstall: boolean;
  onAutoInstall: (v: boolean) => void;
}) {
  const [current, setCurrent] = useState("");
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    import("@tauri-apps/api/app")
      .then(({ getVersion }) => getVersion())
      .then(setCurrent)
      .catch(() => setCurrent(""));   // plain-web dev: no shell to ask
  }, []);

  const install = async (path: string) => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      // Not silent: the installer's own window is the last confirmation before
      // it replaces the app, and it restarts cleanly afterwards.
      await invoke("install_update", { path, silent: false });
    } catch (e) {
      setErr(String(e));
    }
  };

  const check = async () => {
    setBusy(true); setErr(""); setInfo(null);
    try {
      setInfo(await api.updateCheck(current));
    } catch (e) {
      setErr(String(e));
    }
    setBusy(false);
  };

  const download = async () => {
    setErr("");
    try {
      await api.updateDownload();
      setStatus(await api.updateStatus());
    } catch (e) {
      setErr(String(e));
    }
  };

  // Poll while a download runs; install when ready if the user opted in.
  useEffect(() => {
    if (status?.status !== "downloading") return;
    const t = window.setInterval(async () => {
      try {
        const s = await api.updateStatus();
        setStatus(s);
        if (s.status === "ready" && props.autoInstall) void install(s.path);
      } catch { /* backend busy */ }
    }, 700);
    return () => window.clearInterval(t);
  }, [status?.status, props.autoInstall]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="upd">
      <div className="upd-row">
        <span className="toggle-name">
          Version {current || "—"}
          {info?.found && info.version && (
            <span className="upd-latest">
              {info.newer ? ` · ${info.version} available` : " · up to date"}
            </span>
          )}
        </span>
        <button className="ovk-reset" disabled={busy} onClick={() => void check()}>
          {busy ? "Checking…" : "Check for updates"}
        </button>
      </div>

      {info?.found && info.newer && (
        <div className="upd-rel">
          <div className="upd-name">{info.name || info.tag}</div>
          {info.notes && <div className="upd-notes">{info.notes.slice(0, 400)}</div>}
          {status?.status === "downloading" ? (
            <div className="upd-prog"><div style={{ width: `${status.pct}%` }} /></div>
          ) : status?.status === "ready" ? (
            <button className="ovk-reset" onClick={() => void install(status.path)}>
              Install {status.version} and restart
            </button>
          ) : (
            <button className="ovk-reset" onClick={() => void download()}>
              Download {info.version}
              {info.size ? ` (${(info.size / 1048576).toFixed(0)} MB)` : ""}
            </button>
          )}
        </div>
      )}
      {(err || status?.error) && <div className="ovk-err">{err || status?.error}</div>}

      <label className="toggle-row">
        <input type="checkbox" checked={props.autoCheck}
               onChange={(e) => props.onAutoCheck(e.target.checked)} />
        <span className="toggle-text">
          <span className="toggle-name">Check for updates on startup</span>
          <span className="toggle-hint">
            Asks GitHub once per launch whether a newer release exists. Nothing
            downloads until you say so.
          </span>
        </span>
      </label>
      <label className="toggle-row">
        <input type="checkbox" checked={props.autoInstall}
               onChange={(e) => props.onAutoInstall(e.target.checked)} />
        <span className="toggle-text">
          <span className="toggle-name">Download and install automatically</span>
          <span className="toggle-hint">
            When a newer release is found on startup, fetch it and launch the
            installer without asking. The app closes to be replaced, so leave
            this off if you'd rather pick the moment.
          </span>
        </span>
      </label>
    </div>
  );
}

/** Records one hotkey: focus the field, press the combo. Requires a modifier
 * (a bare global "E" would eat the key everywhere in Windows); Esc cancels. */
function HotkeyField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [rec, setRec] = useState(false);
  const pretty = (acc: string) =>
    acc.replace("Backquote", "`").replace("Backslash", "\\")
       .replace("Control", "Ctrl").replace("Super", "Win");
  return (
    <input
      className="ovk-field"
      readOnly
      value={rec ? "Press keys…" : pretty(value)}
      title="Click, then press the key combination (Esc cancels)"
      onFocus={() => setRec(true)}
      onBlur={() => setRec(false)}
      onKeyDown={(e) => {
        e.preventDefault();
        e.stopPropagation();
        if (e.key === "Escape") { e.currentTarget.blur(); return; }
        if (/^(Alt|Control|Shift|Meta)/.test(e.code)) return; // modifiers alone: keep waiting
        const mods = [
          e.ctrlKey && "Ctrl", e.altKey && "Alt",
          e.shiftKey && "Shift", e.metaKey && "Super",
        ].filter(Boolean) as string[];
        if (!mods.length) return;
        const key = e.code.replace(/^Key/, "").replace(/^Digit/, "");
        onChange([...mods, key].join("+"));
        e.currentTarget.blur();
      }}
    />
  );
}

function Settings(props: {
  onClose: () => void;
  onSaved: () => void;
  theme: string;
  onTheme: (id: string) => void;
  density: string;
  onDensity: (id: string) => void;
  refreshOnStart: boolean;
  onRefreshOnStart: (on: boolean) => void;
  closeToTray: boolean;
  onCloseToTray: (on: boolean) => void;
  autoCheckUpdates: boolean;
  onAutoCheckUpdates: (on: boolean) => void;
  autoInstallUpdates: boolean;
  onAutoInstallUpdates: (on: boolean) => void;
  overlayKeepOpen: boolean;
  onOverlayKeepOpen: (on: boolean) => void;
  overlayHotkeys: OverlayHotkeySet;
  // Applies + persists; resolves to "" on success or a bind error to show.
  onOverlayHotkeys: (s: OverlayHotkeySet) => Promise<string>;
  models: Model[];
  model: string;
  auth: Auth;
  onPickModel: (id: string, auth: Auth) => void;
  onSaveSettings: () => Promise<void>;
  fontScale: number;
  onFont: (delta: number) => void;
  onFontReset: () => void;
  overlayScale: number;
  onOverlayScale: (delta: number) => void;
  onOverlayReset: () => void;
  activeWs: string;
  workspace: Workspace | undefined;
  onWsChanged: () => void;
  onWsDeleted: () => void;
  onOpenFile: (id: string) => void;
}) {
  const providers = [
    { id: "anthropic", label: "Anthropic (Claude)" },
    { id: "openai", label: "OpenAI (GPT)" },
    { id: "gemini", label: "Google (Gemini)" },
    { id: "xai", label: "xAI (Grok)" },
  ];
  const [keys, setKeys] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sub, setSub] = useState<SubStatus | null>(null);
  const [subDraft, setSubDraft] = useState("");
  const [saveErr, setSaveErr] = useState("");
  const [settingsSaved, setSettingsSaved] = useState(false);
  const [hkErr, setHkErr] = useState("");   // overlay hotkey bind error, shown inline
  const [sharedCtx, setSharedCtx] = useState("");

  async function saveSettings() {
    setSaveErr("");
    try {
      await props.onSaveSettings();
      setSettingsSaved(true);
      window.setTimeout(() => setSettingsSaved(false), 2200);
    } catch (e) {
      setSaveErr(`Couldn't save settings: ${String(e)}`);
    }
  }

  useEffect(() => {
    // Gate on the backend being up so a cold launch doesn't show the subscription
    // as un-set-up (the token is safe in the OS keychain; the status call just
    // raced the backend start).
    (async () => {
      await api.ready();
      api.keys().then(setKeys).catch(() => {});
      api.subStatus().then(setSub).catch(() => {});
      api.sharedProfile().then(setSharedCtx).catch(() => {});
    })();
  }, []);

  // All four wrap their backend calls so a failure shows a clear message instead
  // of an unhandled rejection, and call onSaved() (which reloads models + forces a
  // repaint) so a newly added key takes effect immediately — no app restart.
  async function saveSub() {
    if (!subDraft.trim()) return;
    setSaveErr("");
    try {
      await api.setSubToken(subDraft.trim());
      setSubDraft("");
      setSub(await api.subStatus());
      props.onSaved();
    } catch (e) {
      setSaveErr(`Couldn't save the subscription token: ${String(e)}`);
    }
  }
  async function removeSub() {
    setSaveErr("");
    try {
      await api.deleteSubToken();
      setSub(await api.subStatus());
      props.onSaved();
    } catch (e) {
      setSaveErr(`Couldn't remove the token: ${String(e)}`);
    }
  }

  async function save(p: string) {
    if (!drafts[p]) return;
    setSaveErr("");
    try {
      await api.setKey(p, drafts[p]);
      setDrafts((d) => ({ ...d, [p]: "" }));
      setKeys(await api.keys());
      props.onSaved();
    } catch (e) {
      setSaveErr(`Couldn't save the ${p} key: ${String(e)}`);
    }
  }
  async function remove(p: string) {
    setSaveErr("");
    try {
      await api.deleteKey(p);
      setKeys(await api.keys());
      props.onSaved();
    } catch (e) {
      setSaveErr(`Couldn't remove the ${p} key: ${String(e)}`);
    }
  }

  return (
    <div className="modal-bg" onClick={props.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          Settings
          <button className="x" onClick={props.onClose}>
            ✕
          </button>
        </div>

        <div className="section-label ws-head">
          Character profile
          <span className="ws-head-name">{props.workspace?.display_name || props.activeWs}</span>
        </div>
        <WorkspaceSettings
          slug={props.activeWs}
          workspace={props.workspace}
          onChanged={props.onWsChanged}
          onDeleted={props.onWsDeleted}
          onOpenFile={props.onOpenFile}
        />

        <div className="section-label">Agent preferences</div>
        <div className="prefs-row">
          <span className="toggle-hint">
            Standing instructions the assistant follows in every chat — it adds a
            line when you ask it to remember something "from now on". Yours to edit.
          </span>
          <button className="open-file-btn" onClick={() => props.onOpenFile("prefs")}>
            ✎ Open in editor
          </button>
        </div>

        <div className="section-label">Text size</div>
        <div className="fontsize-row">
          <button className="fs-btn" onClick={() => props.onFont(-0.1)} title="Ctrl + [">A−</button>
          <span className="fs-val">{Math.round(props.fontScale * 100)}%</span>
          <button className="fs-btn" onClick={() => props.onFont(0.1)} title="Ctrl + ]">A+</button>
          <button className="fs-reset" onClick={props.onFontReset}>Reset</button>
          <span className="fs-hint">or press Ctrl + [ / Ctrl + ]</span>
        </div>

        <div className="section-label">Overlay size</div>
        <div className="fontsize-row">
          <button className="fs-btn" onClick={() => props.onOverlayScale(-0.1)}>A−</button>
          <span className="fs-val">{Math.round(props.overlayScale * 100)}%</span>
          <button className="fs-btn" onClick={() => props.onOverlayScale(0.1)}>A+</button>
          <button className="fs-reset" onClick={props.onOverlayReset}>Reset</button>
          <span className="fs-hint">Size of the in-game overlay widgets — pill, cards, chips</span>
        </div>

        <div className="section-label">Density</div>
        <div className="density-row">
          {[
            { id: "compact", name: "Compact", hint: "Tighter spacing, more on screen" },
            { id: "comfortable", name: "Comfortable", hint: "Roomier line spacing" },
          ].map((d) => (
            <button
              key={d.id}
              className={"density-btn" + (props.density === d.id ? " active" : "")}
              onClick={() => props.onDensity(d.id)}
            >
              <span className="density-name">{d.name}</span>
              <span className="density-hint">{d.hint}</span>
            </button>
          ))}
        </div>

        <div className="section-label">Default model</div>
        <div className="default-model-row">
          <select
            className="default-model-select"
            value={props.model}
            onChange={(e) => {
              const m = props.models.find((x) => x.id === e.target.value);
              if (m) props.onPickModel(m.id, m.default_auth ?? "api");
            }}
          >
            {props.models.filter((m) => m.available).map((m) => (
              <option key={m.id} value={m.id}>
                {m.provider_label} — {m.label} ({m.tool_use} tool use)
              </option>
            ))}
          </select>
          <span className="default-model-hint">
            The model each new chat starts on, remembered across restarts. You can
            still switch per-chat from the picker beside the message box — whatever
            you pick there becomes your default too.
            {props.auth === "subscription"
              ? " Running on your Claude subscription (no per-token cost)."
              : " Billed to your API key per token."}
          </span>
        </div>

        <div className="section-label">Character profile</div>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={props.refreshOnStart}
            onChange={(e) => props.onRefreshOnStart(e.target.checked)}
          />
          <span className="toggle-text">
            <span className="toggle-name">Refresh profile on start</span>
            <span className="toggle-hint">
              Re-import your Lodestone character when the app opens. Only refetches
              profiles older than 24h, and runs in the background — it never delays
              startup. Turn off to refresh only via the Refresh button.
            </span>
          </span>
        </label>

        <div className="section-label">Updates</div>
        <UpdatePanel
          autoCheck={props.autoCheckUpdates}
          onAutoCheck={props.onAutoCheckUpdates}
          autoInstall={props.autoInstallUpdates}
          onAutoInstall={props.onAutoInstallUpdates}
        />

        <div className="section-label">Background &amp; overlay</div>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={props.overlayKeepOpen}
            onChange={(e) => props.onOverlayKeepOpen(e.target.checked)}
          />
          <span className="toggle-text">
            <span className="toggle-name">Keep overlay surfaces open</span>
            <span className="toggle-hint">
              Answer cards stop fading out on their own, and clicking away no
              longer closes the Ask pill or the database drawer — it just hands
              the mouse back to the game, leaving them on screen. Esc and the
              kill switch still close them.
            </span>
          </span>
        </label>
        <OverlayWatchList />
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={props.closeToTray}
            onChange={(e) => props.onCloseToTray(e.target.checked)}
          />
          <span className="toggle-text">
            <span className="toggle-name">Keep running in the background</span>
            <span className="toggle-hint">
              Closing this window hides it to the system tray (bottom-right of
              the taskbar) instead of quitting — the in-game overlay keeps
              working. Click the tray icon and choose "Open Aether Intelligence"
              to bring the app back, or "Quit" to exit fully. When off, closing
              the app also closes the overlay.
            </span>
          </span>
        </label>

        <div className="ovk-rows">
          <span className="toggle-name">Overlay shortcuts</span>
          <span className="toggle-hint">
            Click a field, then press the key combination — it must include
            Ctrl, Alt, Shift, or Win. Esc cancels. Applied immediately.
          </span>
          {([["ask", "Open the Ask pill"],
             ["drawer", "Open the database drawer"],
             ["ambient", "Show overlay (widgets only)"],
             ["kill", "Hide / show overlay"]] as const).map(([k, label]) => (
            <div className="ovk-row" key={k}>
              <span className="ovk-label">{label}</span>
              <HotkeyField
                value={props.overlayHotkeys[k]}
                onChange={(v) => {
                  void props.onOverlayHotkeys({ ...props.overlayHotkeys, [k]: v })
                    .then(setHkErr);
                }}
              />
            </div>
          ))}
          {hkErr && <span className="ovk-err">{hkErr}</span>}
          <button
            className="ovk-reset"
            onClick={() => {
              void props.onOverlayHotkeys({ ...OVERLAY_HOTKEY_DEFAULTS }).then(setHkErr);
            }}
          >
            Reset to defaults
          </button>
        </div>

        <div className="section-label">Theme</div>
        <div className="theme-grid">
          {THEMES.map((t) => (
            <button
              key={t.id}
              className={"theme-swatch" + (props.theme === t.id ? " active" : "")}
              onClick={() => props.onTheme(t.id)}
            >
              <span className="theme-dots">
                {t.dots.map((c, i) => (
                  <span key={i} style={{ background: c }} />
                ))}
              </span>
              {t.name}
            </button>
          ))}
        </div>

        <div className="section-label">Shared context (all profiles)</div>
        <p className="muted small">
          The AI reads this in every character profile, alongside that character's own
          profile — server/region, answer style, what you care about.
        </p>
        <textarea
          className="ws-profile"
          value={sharedCtx}
          placeholder="Context that applies to all your characters…"
          onChange={(e) => setSharedCtx(e.target.value)}
          onBlur={() => api.putSharedProfile(sharedCtx).catch(() => {})}
        />

        <div className="settings-save-row">
          <button className="settings-save" onClick={saveSettings}>Save settings</button>
          {settingsSaved ? (
            <span className="settings-saved-msg">✓ Settings saved — they'll persist across updates.</span>
          ) : (
            <span className="muted small">Changes apply instantly; saving stores them for future updates.</span>
          )}
        </div>

        <div className="section-label" style={{ marginTop: 20 }}>
          API keys &amp; models
        </div>
        <p className="muted small">
          Credentials are stored in your OS keychain — never in a file, never logged.
        </p>
        {saveErr && <div className="save-err">{saveErr}</div>}

        {/* Claude subscription — use Pro/Max instead of a billed API key */}
        <div className="sub-box">
          <div className="key-label">
            Claude subscription (Pro / Max){" "}
            {sub?.token_set && <span className="tag ok">token set</span>}
          </div>
          <p className="muted small" style={{ marginTop: 0 }}>
            Use your Claude subscription instead of a billed API key.{" "}
            {sub &&
              (sub.cli_found ? (
                <span className="ok-text">✓ Claude Code detected</span>
              ) : (
                <span className="warn-text">
                  ✕ Claude Code not found — install it to use this path
                </span>
              ))}
          </p>
          <p className="muted small" style={{ marginTop: 0 }}>
            Run <code>{sub?.setup_command || "claude setup-token"}</code> in a terminal,
            then paste the token below.
          </p>
          <div className="key-input">
            <input
              type="password"
              placeholder={sub?.token_set ? "Replace token…" : "Paste subscription token…"}
              value={subDraft}
              onChange={(e) => setSubDraft(e.target.value)}
            />
            <button onClick={saveSub}>Save</button>
            {sub?.token_set && (
              <button className="danger" onClick={removeSub}>
                Remove
              </button>
            )}
          </div>
        </div>

        <div className="section-label">API keys (pay per use)</div>
        {providers.map((p) => (
          <div key={p.id} className="key-row">
            <div className="key-label">
              {p.label} {keys[p.id] && <span className="tag ok">set</span>}
            </div>
            <div className="key-input">
              <input
                type="password"
                placeholder={keys[p.id] ? "Replace key…" : "Paste API key…"}
                value={drafts[p.id] || ""}
                onChange={(e) => setDrafts((d) => ({ ...d, [p.id]: e.target.value }))}
              />
              <button onClick={() => save(p.id)}>Save</button>
              {keys[p.id] && (
                <button className="danger" onClick={() => remove(p.id)}>
                  Remove
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
