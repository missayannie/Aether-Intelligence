import { useEffect, useMemo, useRef, useState } from "react";
import { api, type CustomPin, type MarkerHit, type ZoneMap } from "./api";

/** The in-game map, rebuilt: Garland's texture with the game's own markers on top.
 *
 * Everything is laid out in the map's 2048 coordinate space and scaled by one CSS
 * transform, so markers stay glued to the terrain at any zoom and there is no
 * per-marker math on pan. Marker SIZE is divided back out by the scale, so pins and
 * labels stay legible when you zoom in rather than becoming billboards.
 *
 * The player's own pins live in the same coordinate space (persisted server-side,
 * per zone). Pin mode disables panning so a click can mean "place a pin here";
 * right-clicking a custom pin removes it.
 */
/** Colours a pin can wear. First is the default gold. */
const PIN_COLORS = ["#e6b800", "#e65a5a", "#3ca0ff", "#5adc78", "#c77dff", "#f5f5f5"];

/** The region/zone dropdowns alone — shown while a map is LOADING or FAILED, so
 *  the player can always navigate away instead of being stranded on an error. */
export function MapNavBar({ regions, current, onOpenZone }: {
  regions: { region: string; zones: string[] }[];
  current: string;
  onOpenZone: (zone: string) => void;
}) {
  const [region, setRegion] = useState("");
  useEffect(() => {
    const home = regions.find((g) => g.zones.includes(current));
    if (home) setRegion(home.region);
    else if (regions[0]) setRegion((r) => r || regions[0].region);
  }, [current, regions]);
  const zones = regions.find((g) => g.region === region)?.zones || [];
  if (!regions.length) return null;
  return (
    <div className="gm-bar">
      <span className="gm-nav">
        🗺
        <select className="gm-sel" value={region} title="Region"
                onChange={(e) => setRegion(e.target.value)}>
          {regions.map((g) => (
            <option key={g.region} value={g.region}>{g.region}</option>
          ))}
        </select>
        <select className="gm-sel" title="Zone"
                value={zones.includes(current) ? current : ""}
                onChange={(e) => e.target.value && onOpenZone(e.target.value)}>
          {!zones.includes(current) && <option value="">— zone —</option>}
          {zones.map((z) => (
            <option key={z} value={z}>{z}</option>
          ))}
        </select>
      </span>
    </div>
  );
}

export default function GameMap({ map, onClose, onTextureError, onSaveShot,
                                  regions = [], onOpenZone,
                                  tempPin = null, onTempPinKept,
                                  tempGroup = null, onTempGroupKept }: {
  map: ZoneMap;
  onClose?: () => void;
  /** The texture URL 404'd or failed to decode — the parent should fall back. */
  onTextureError?: () => void;
  /** Save a rendered screenshot as an asset. Absent = hide the camera button. */
  onSaveShot?: (blob: Blob, caption: string) => Promise<void> | void;
  /** Region -> zones, for the in-bar navigation dropdowns (in-game style). */
  regions?: { region: string; zones: string[] }[];
  /** Open another zone (picked from the dropdowns, or a pin-search jump —
   *  focus, in 2048 map space, centres the view on that spot). */
  onOpenZone?: (zone: string, focus?: { x: number; y: number }) => void;
  /** A transient marker (agent answer / chat map-link / GarlandDB node). kind is
   *  the toolbar group it joins if the player KEEPS it (📌), e.g. "gathering". */
  tempPin?: { x: number; y: number; label: string; icon?: string; radiusPx?: number;
              kind?: string } | null;
  /** The temp pin was saved permanently — the parent should clear it, or the
   *  new custom pin and the temp pin render doubled at the same spot. */
  onTempPinKept?: () => void;
  /** A whole CATEGORY of transient pins at once (agent: "pin all the aether
   *  currents"). Save keeps the set as "Custom – <category>". */
  tempGroup?: { category: string; icon?: string;
                pins: { x: number; y: number; label: string }[] } | null;
  onTempGroupKept?: () => void;
}) {
  const SPACE = map.coord_space || 2048;
  const wrap = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const [tx, setTx] = useState(0);
  const [ty, setTy] = useState(0);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null);

  // ---- the player's own pins ----
  const [pins, setPins] = useState<CustomPin[]>([]);
  // Visibility is per pin GROUP: plain pins ("") under "My pins", typed pins
  // (kind "gathering" etc.) each under their own "Custom – …" toolbar toggle.
  const [hiddenPinKinds, setHiddenPinKinds] = useState<Set<string>>(new Set());
  const [pinMode, setPinMode] = useState(false);
  const [draft, setDraft] = useState<{ x: number; y: number } | null>(null);
  const [draftLabel, setDraftLabel] = useState("");
  const [draftColor, setDraftColor] = useState(PIN_COLORS[0]);
  // Clicking one of your pins opens this editor: rename, recolour, or delete.
  const [editing, setEditing] = useState<CustomPin | null>(null);
  const [editLabel, setEditLabel] = useState("");
  const [editColor, setEditColor] = useState(PIN_COLORS[0]);
  // "⏱ Watch" feedback — this pin now lives on the in-game overlay as a chip.
  const [pinWatched, setPinWatched] = useState(false);
  useEffect(() => { setPinWatched(false); }, [editing]);
  // Icon urls that 404'd — those markers fall back to a dot instead of the
  // webview's broken-image glyph.
  const [brokenIcons, setBrokenIcons] = useState<Set<string>>(new Set());

  // ---- in-bar navigation (region -> zone, like the in-game map window) ----
  // navRegion follows the open zone; picking another region just repopulates the
  // zone list, and picking a zone opens it.
  const [navRegion, setNavRegion] = useState("");
  useEffect(() => {
    const home = regions.find((g) => g.zones.includes(map.zone));
    if (home) setNavRegion(home.region);
  }, [map.zone, regions]);
  const navZones = regions.find((g) => g.region === navRegion)?.zones || [];

  // ---- pin search (all zones) ----
  const [pinQuery, setPinQuery] = useState("");
  // The whole pin store, fetched when the search field gains focus — pins are
  // few (a personal map), so one fetch beats a per-keystroke endpoint.
  const [allPins, setAllPins] = useState<Record<string, CustomPin[]> | null>(null);
  // Named game markers (aetherytes, dungeons, landmarks…) matching the query —
  // debounced, since each lookup is a backend/XIVAPI search call.
  const [markerHits, setMarkerHits] = useState<MarkerHit[]>([]);
  useEffect(() => {
    const q = pinQuery.trim();
    if (q.length < 2) {
      setMarkerHits([]);
      return;
    }
    const t = setTimeout(() => {
      api.searchMapMarkers(q)
        .then((r) => setMarkerHits(r.markers))
        .catch(() => setMarkerHits([]));
    }, 250);
    return () => clearTimeout(t);
  }, [pinQuery]);

  // ---- screenshot ----
  const [capOpen, setCapOpen] = useState(false);
  const [caption, setCaption] = useState("");
  const [shotBusy, setShotBusy] = useState(false);
  const [shotDone, setShotDone] = useState("");

  useEffect(() => {
    api.mapPins(map.zone).then((r) => setPins(r.pins)).catch(() => setPins([]));
    setPinMode(false); setDraft(null); setEditing(null);
    setBrokenIcons(new Set());
  }, [map.zone]);

  // Open centred on the thing you came here for (the pinned node), zoomed in enough
  // to read it. Without this a node lands as one dot in a 2048px field.
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const { width: w, height: h } = el.getBoundingClientRect();
    if (map.focus) {
      const s = 2.5;
      setScale(s);
      setTx(w / 2 - (map.focus.x / SPACE) * SPACE * s);
      setTy(h / 2 - (map.focus.y / SPACE) * SPACE * s);
    } else {
      // Fit the whole zone.
      const s = Math.min(w, h) / SPACE;
      setScale(s);
      setTx((w - SPACE * s) / 2);
      setTy((h - SPACE * s) / 2);
    }
  }, [map.zone, map.focus, SPACE]);

  function onWheel(e: React.WheelEvent) {
    const el = wrap.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const next = Math.min(8, Math.max(0.15, scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    // Keep the point under the cursor fixed while zooming — otherwise the map
    // slides away from whatever you were trying to look at.
    setTx(mx - ((mx - tx) / scale) * next);
    setTy(my - ((my - ty) / scale) * next);
    setScale(next);
  }

  function onDown(e: React.MouseEvent) {
    if (pinMode) return;   // pin mode trades panning for "a click means HERE"
    drag.current = { x: e.clientX, y: e.clientY, tx, ty, moved: false };
  }
  function onMove(e: React.MouseEvent) {
    const d = drag.current;
    if (!d) return;
    if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) d.moved = true;
    setTx(d.tx + (e.clientX - d.x));
    setTy(d.ty + (e.clientY - d.y));
  }
  const endDrag = () => { drag.current = null; };

  function onViewClick(e: React.MouseEvent) {
    if (!pinMode || draft) return;
    const el = wrap.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left - tx) / scale;
    const py = (e.clientY - r.top - ty) / scale;
    if (px < 0 || py < 0 || px > SPACE || py > SPACE) return;   // off the parchment
    setDraft({ x: px, y: py });
    setDraftLabel("");
  }

  async function saveDraft() {
    if (!draft) return;
    try {
      const pin = await api.addMapPin(map.zone, draft.x, draft.y, draftLabel, draftColor);
      setPins((p) => [...p, pin]);
      unhideKind("");
    } catch { /* backend hiccup — the draft stays so the player can retry */ return; }
    setDraft(null);
    // Back to drag/pan mode: one press of 📍 = one pin. Staying in pin mode
    // after a save made the next pan click drop an accidental draft; another
    // pin is one more 📍 press away.
    setPinMode(false);
  }

  async function removePin(p: CustomPin) {
    setEditing(null);
    setPins((cur) => cur.filter((x) => x.id !== p.id));   // optimistic
    try { await api.deleteMapPin(map.zone, p.id); }
    catch { setPins((cur) => [...cur, p]); }              // put it back on failure
  }

  function openEditor(p: CustomPin) {
    setEditing(p);
    setEditLabel(p.label);
    setEditColor(p.color || PIN_COLORS[0]);
  }

  // Backspace deletes the SELECTED pin (the one whose editor is open) — but
  // never while the player is typing in a field, where Backspace edits text.
  // Right-click deletion is gone on purpose: one mis-click erased a pin with
  // no confirmation; selection-then-delete can't fire by accident.
  useEffect(() => {
    if (!editing) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.key === "Backspace") {
        e.preventDefault();
        removePin(editing);
      }
      // The input used to own Escape-to-close via its own handler; without
      // autofocus the window needs one for the just-selected (unfocused) state.
      if (e.key === "Escape") setEditing(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  async function saveEdit() {
    if (!editing) return;
    try {
      const upd = await api.updateMapPin(map.zone, editing.id,
                                         { label: editLabel, color: editColor });
      setPins((cur) => cur.map((x) => (x.id === upd.id ? upd : x)));
      setEditing(null);
    } catch { /* keep the editor open so nothing typed is lost */ }
  }

  /** Centre the view on a map-space point, zoomed in enough to read the spot. */
  function centerOn(x: number, y: number) {
    const el = wrap.current;
    if (!el) return;
    const { width: w, height: h } = el.getBoundingClientRect();
    const s = Math.max(scale, 2.5);
    setScale(s);
    setTx(w / 2 - x * s);
    setTy(h / 2 - y * s);
  }

  // Punctuation-insensitive match: FFXIV names are full of apostrophes and
  // dashes ("Ul'dah - Steps of Thal") no player types back exactly — "uldah"
  // must match.
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

  // Pin-search matches across EVERY zone, current zone's pins first. Uses the
  // fetched store, with the live in-memory list standing in for the open zone so
  // a pin kept seconds ago is already findable.
  const pinHits = useMemo(() => {
    const q = norm(pinQuery);
    if (!q) return [];
    const store = { ...(allPins || {}), [map.zone]: pins };
    const out: { zone: string; pin: CustomPin }[] = [];
    for (const zone of Object.keys(store).sort((a, b) =>
      a === map.zone ? -1 : b === map.zone ? 1 : a.localeCompare(b))) {
      for (const pin of store[zone]) {
        if (norm(pin.label || "").includes(q)) out.push({ zone, pin });
      }
    }
    return out.slice(0, 8);
  }, [pinQuery, allPins, pins, map.zone]);

  // Whole ZONES by name — "uldah" should surface Ul'dah - Steps of Thal even
  // though it's a map, not a marker. The regions prop is already in memory.
  const zoneHits = useMemo(() => {
    const q = norm(pinQuery);
    if (q.length < 2) return [];
    const out: string[] = [];
    for (const g of regions) {
      for (const z of g.zones) if (norm(z).includes(q)) out.push(z);
    }
    return out.slice(0, 4);
  }, [pinQuery, regions]);

  function jumpToPin(zone: string, pin: CustomPin) {
    setPinQuery("");
    if (zone === map.zone) {
      unhideKind(pin.kind || "");
      centerOn(pin.x, pin.y);
    } else {
      onOpenZone?.(zone, { x: pin.x, y: pin.y });
    }
  }

  function jumpToMarker(m: MarkerHit) {
    setPinQuery("");
    if (m.zone === map.zone) {
      // The marker may be on a hidden layer — reveal it, or the jump lands on
      // seemingly empty parchment.
      setHidden((prev) => {
        if (!prev.has(m.kind)) return prev;
        const next = new Set(prev);
        next.delete(m.kind);
        return next;
      });
      centerOn(m.x, m.y);
    } else {
      onOpenZone?.(m.zone, { x: m.x, y: m.y });
    }
  }

  /** In-game flag coords for a pin tooltip — the numbers a player would type. */
  function gameCoord(v: number): string {
    const c = (v / SPACE) * 41 / ((map.size_factor || 100) / 100) + 1;
    return c.toFixed(1);
  }

  const shown = useMemo(
    () => map.markers.filter((m) => !hidden.has(m.kind)),
    [map.markers, hidden],
  );

  // Pin groups for the toolbar: "" (plain "My pins") first, then each kind
  // alphabetically — one toggle per group, like the game's own marker layers.
  const pinKinds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of pins) {
      const k = p.kind || "";
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [pins]);
  const visiblePins = useMemo(() => {
    let out = pins.filter((p) => !hiddenPinKinds.has(p.kind || ""));
    // A temp pin often lands exactly on a pin the player already KEPT there
    // (ask about a node you saved → the answer pins the same spot). Drawing both
    // stacks two glyphs and two labels; hide the covered one while the temp pin
    // is up — it's back the moment the temp pin clears. 12 map-px ≈ a quarter of
    // an in-game coord: only true same-spot stacks, never two distinct places.
    if (tempPin) {
      out = out.filter((p) => Math.hypot(p.x - tempPin.x, p.y - tempPin.y) > 12);
    }
    return out;
  }, [pins, hiddenPinKinds, tempPin]);
  const kindLabel = (k: string) =>
    k ? `Custom – ${k.charAt(0).toUpperCase()}${k.slice(1)}` : "My pins";

  function unhideKind(k: string) {
    setHiddenPinKinds((prev) => {
      if (!prev.has(k)) return prev;
      const next = new Set(prev);
      next.delete(k);
      return next;
    });
  }

  /** 📌 Keep: promote the temporary pin to a permanent one UNDER ITS TYPE — a
   *  gathering node stays a gathering pin (icon and all) with its own toggle. */
  async function keepTempPin() {
    if (!tempPin) return;
    try {
      // Keeping the same spot twice REPLACES the earlier pin instead of stacking
      // a duplicate under it (the temp pin usually carries the fresher label/icon).
      const covered = pins.filter(
        (p) => Math.hypot(p.x - tempPin.x, p.y - tempPin.y) <= 12);
      const pin = await api.addMapPin(map.zone, tempPin.x, tempPin.y, tempPin.label,
                                      "", tempPin.kind || "", tempPin.icon || "");
      for (const c of covered) await api.deleteMapPin(map.zone, c.id);
      setPins((p) => [...p.filter((x) => !covered.some((c) => c.id === x.id)), pin]);
      unhideKind(pin.kind || "");
      onTempPinKept?.();   // the temp pin must go, or it doubles the saved one
    } catch { /* keep the temp pin so the player can retry */ }
  }

  function toggle(id: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  // The toolbar-group key a saved category set files under (mappins lowercases
  // and trims the same way — keep in sync with sources/mappins.py).
  const groupKind = (tempGroup?.category || "").trim().toLowerCase().slice(0, 24);

  /** 💾 Save the whole temporary category set as "Custom – <category>" pins. */
  async function keepTempGroup() {
    if (!tempGroup || !tempGroup.pins.length) return;
    try {
      const saved: CustomPin[] = [];
      for (const p of tempGroup.pins) {
        saved.push(await api.addMapPin(map.zone, p.x, p.y, p.label, "",
                                       groupKind, tempGroup.icon || ""));
      }
      setPins((prev) => [...prev, ...saved]);
      unhideKind(groupKind);
      onTempGroupKept?.();   // clear the temps, or every pin renders doubled
    } catch { /* partial saves stay; the player can press Save again */ }
  }

  /** Draw what's on screen — texture, visible markers, custom pins — to a PNG.
   *  Reuses the DOM's already-loaded images (they carry crossOrigin="anonymous",
   *  and the backend answers CORS, so the canvas stays untainted). */
  async function renderShot(cap: string): Promise<Blob | null> {
    const view = wrap.current;
    const tex = view?.querySelector<HTMLImageElement>("img.gm-tex");
    if (!view || !tex || !tex.complete || !tex.naturalWidth) return null;
    const W = view.clientWidth, H = view.clientHeight;
    const CAP = cap ? 44 : 0;
    // Capture at the SCREEN's pixel density, not a fixed 2x: oversampling the
    // texture skipped the smoothing the on-screen render gets, and the saved shot
    // came out visibly harsher/more contrasty than what the player was looking at.
    const S = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    const canvas = document.createElement("canvas");
    canvas.width = W * S; canvas.height = (H + CAP) * S;
    const g = canvas.getContext("2d");
    if (!g) return null;
    g.scale(S, S);
    g.fillStyle = "#0b1220"; g.fillRect(0, 0, W, H + CAP);
    g.drawImage(tex, tx, ty, SPACE * scale, SPACE * scale);

    const icons = new Map<string, HTMLImageElement>();
    // gm-ico are the game's markers; gm-pin-icon are typed pins (kept + temp) —
    // both already loaded in the DOM, so the shot can reuse them untainted.
    view.querySelectorAll<HTMLImageElement>("img.gm-ico, img.gm-pin-icon")
        .forEach((i) => icons.set(i.src, i));
    const label = (text: string, x: number, y: number, italic = false) => {
      g.font = `${italic ? "italic " : ""}600 12px "Segoe UI", system-ui, sans-serif`;
      g.lineJoin = "round"; g.lineWidth = 3; g.strokeStyle = "#ffffff";
      g.strokeText(text, x, y);
      g.fillStyle = "#1a1a1a"; g.fillText(text, x, y);
    };

    for (const m of shown) {
      const sx = tx + m.x * scale, sy = ty + m.y * scale;
      if (sx < -80 || sy < -80 || sx > W + 80 || sy > H + 80) continue;
      if (m.kind === "area") {
        g.textAlign = "center";
        label(m.label, sx, sy, true);
        g.textAlign = "left";
        continue;
      }
      const ico = m.icon ? icons.get(m.icon) : undefined;
      if (ico && ico.complete && ico.naturalWidth) {
        g.drawImage(ico, sx - 11, sy - 11, 22, 22);
      } else {
        // No icon: same tiny black dot the screen shows.
        g.beginPath(); g.arc(sx, sy, 3, 0, Math.PI * 2);
        g.fillStyle = "#1a1a1a"; g.fill();
        g.lineWidth = 1; g.strokeStyle = "#ffffffcc"; g.stroke();
      }
      if (m.label) label(m.label, sx + 12, sy + 4);
    }
    for (const p of visiblePins) {
      const sx = tx + p.x * scale, sy = ty + p.y * scale;
      if (sx < -80 || sy < -80 || sx > W + 80 || sy > H + 80) continue;
      const ico = p.icon ? icons.get(api.iconByName(p.icon)) : undefined;
      if (ico && ico.complete && ico.naturalWidth) {
        g.drawImage(ico, sx - 13, sy - 13, 26, 26);
      } else {
        g.beginPath(); g.arc(sx, sy, 7, 0, Math.PI * 2);
        g.fillStyle = p.color || PIN_COLORS[0]; g.fill();
        g.lineWidth = 2; g.strokeStyle = "#1a1a1a"; g.stroke();
      }
      if (p.label) label(p.label, sx + 11, sy + 4);
    }
    if (tempPin) {
      const sx = tx + tempPin.x * scale, sy = ty + tempPin.y * scale;
      // Area circle first, so the pin sits on top of it in the shot too.
      if (tempPin.radiusPx) {
        const rr = tempPin.radiusPx * scale;
        g.beginPath(); g.arc(sx, sy, rr, 0, Math.PI * 2);
        g.fillStyle = "rgba(240,150,60,.22)"; g.fill();
        g.setLineDash([6, 5]);
        g.lineWidth = 2; g.strokeStyle = "rgba(220,110,40,.85)"; g.stroke();
        g.setLineDash([]);
      }
      if (sx > -80 && sy > -80 && sx < W + 80 && sy < H + 80) {
        const ico = tempPin.icon ? icons.get(api.iconByName(tempPin.icon)) : undefined;
        if (ico && ico.complete && ico.naturalWidth) {
          g.drawImage(ico, sx - 13, sy - 13, 26, 26);
        } else {
          g.beginPath(); g.arc(sx, sy, 7, 0, Math.PI * 2);
          g.fillStyle = PIN_COLORS[0]; g.fill();
          g.lineWidth = 2; g.strokeStyle = "#1a1a1a"; g.stroke();
        }
        if (tempPin.label) label(tempPin.label, sx + 11, sy + 4);
      }
    }
    if (tempGroup) {
      const ico = tempGroup.icon ? icons.get(api.iconByName(tempGroup.icon)) : undefined;
      for (const p of tempGroup.pins) {
        const sx = tx + p.x * scale, sy = ty + p.y * scale;
        if (sx < -80 || sy < -80 || sx > W + 80 || sy > H + 80) continue;
        if (ico && ico.complete && ico.naturalWidth) {
          g.drawImage(ico, sx - 13, sy - 13, 26, 26);
        } else {
          g.beginPath(); g.arc(sx, sy, 7, 0, Math.PI * 2);
          g.fillStyle = PIN_COLORS[0]; g.fill();
          g.lineWidth = 2; g.strokeStyle = "#1a1a1a"; g.stroke();
        }
        if (p.label) label(p.label, sx + 11, sy + 4);
      }
    }
    if (cap) {
      g.fillStyle = "rgba(8,10,18,.92)"; g.fillRect(0, H, W, CAP);
      g.fillStyle = "#e6ecf5"; g.font = `600 14px "Segoe UI", system-ui, sans-serif`;
      g.fillText(cap, 12, H + 27);
      g.textAlign = "right";
      g.fillStyle = "#8fa0bd"; g.font = `11px "Segoe UI", system-ui, sans-serif`;
      g.fillText(map.zone, W - 12, H + 27);
      g.textAlign = "left";
    }
    return new Promise((res) => canvas.toBlob(res, "image/png"));
  }

  async function takeShot() {
    if (!onSaveShot) return;
    setShotBusy(true);
    try {
      const blob = await renderShot(caption.trim());
      if (blob) {
        await onSaveShot(blob, caption.trim());
        setShotDone("Saved to Assets ✓");
        setTimeout(() => setShotDone(""), 2500);
      }
    } finally {
      setShotBusy(false);
      setCapOpen(false);
      setCaption("");
    }
  }

  return (
    <div className="gm">
      <div className="gm-bar">
        {regions.length && onOpenZone ? (
          <span className="gm-nav">
            🗺
            <select className="gm-sel" value={navRegion}
                    onChange={(e) => setNavRegion(e.target.value)}
                    title="Region">
              {regions.map((g) => (
                <option key={g.region} value={g.region}>{g.region}</option>
              ))}
            </select>
            <select className="gm-sel" title="Zone"
                    value={navZones.includes(map.zone) ? map.zone : ""}
                    onChange={(e) => e.target.value && onOpenZone(e.target.value)}>
              {!navZones.includes(map.zone) && <option value="">— zone —</option>}
              {navZones.map((z) => (
                <option key={z} value={z}>{z}</option>
              ))}
            </select>
          </span>
        ) : (
          <span className="gm-zone">🗺 {map.zone}</span>
        )}
        <span className="gm-pin-search">
          <input className="gm-search-input" placeholder="Find pin…" value={pinQuery}
                 onChange={(e) => setPinQuery(e.target.value)}
                 onFocus={() => api.allMapPins().then((r) => setAllPins(r.zones)).catch(() => {})}
                 onKeyDown={(e) => {
                   if (e.key === "Escape") setPinQuery("");
                   if (e.key === "Enter") {
                     if (pinHits[0]) jumpToPin(pinHits[0].zone, pinHits[0].pin);
                     else if (zoneHits[0]) { setPinQuery(""); onOpenZone?.(zoneHits[0]); }
                     else if (markerHits[0]) jumpToMarker(markerHits[0]);
                   }
                 }} />
          {(pinHits.length > 0 || zoneHits.length > 0 || markerHits.length > 0) && (
            <div className="gm-search-drop">
              {pinHits.map(({ zone, pin }) => (
                // onMouseDown, not onClick — a click would blur the input first
                // and anything keyed to blur would close the list under the cursor.
                <button key={pin.id} className="gm-search-hit"
                        onMouseDown={(e) => { e.preventDefault(); jumpToPin(zone, pin); }}>
                  {pin.icon ? (
                    <img className="gm-search-ico" src={api.iconByName(pin.icon)} alt="" />
                  ) : (
                    <span className="gm-search-dot"
                          style={{ background: pin.color || PIN_COLORS[0] }} />
                  )}
                  <span className="gm-search-label">{pin.label || "(unlabeled)"}</span>
                  <span className="gm-search-zone">{zone}</span>
                </button>
              ))}
              {zoneHits.map((z) => (
                <button key={`z${z}`} className="gm-search-hit"
                        onMouseDown={(e) => { e.preventDefault(); setPinQuery(""); onOpenZone?.(z); }}>
                  <span className="gm-search-glyph">🗺</span>
                  <span className="gm-search-label">{z}</span>
                  <span className="gm-search-zone">zone</span>
                </button>
              ))}
              {markerHits.map((m, i) => (
                <button key={`m${i}`} className="gm-search-hit"
                        onMouseDown={(e) => { e.preventDefault(); jumpToMarker(m); }}>
                  {m.icon ? (
                    <img className="gm-search-ico" src={m.icon} alt="" />
                  ) : (
                    // Area names have no icon in the game data — the layer glyph.
                    <span className="gm-search-glyph">𝘈</span>
                  )}
                  <span className="gm-search-label">{m.label}</span>
                  <span className="gm-search-zone">{m.zone}</span>
                </button>
              ))}
            </div>
          )}
        </span>
        {map.layers.map((l) => (
          <button key={l.id}
                  className={"gm-layer" + (hidden.has(l.id) ? " off" : "")}
                  onClick={() => toggle(l.id)}
                  title={`Show/hide ${l.label}`}>
            <span className={"gm-dot k-" + l.id} /> {l.label}
          </button>
        ))}
        {pinKinds.map(([k, count]) => (
          <button key={k || "own"}
                  className={"gm-layer" + (hiddenPinKinds.has(k) ? " off" : "")}
                  onClick={() => setHiddenPinKinds((prev) => {
                    const next = new Set(prev);
                    next.has(k) ? next.delete(k) : next.add(k);
                    return next;
                  })}
                  title={`Show/hide ${kindLabel(k).toLowerCase()}`}>
            <span className="gm-dot k-custom" /> {kindLabel(k)} ({count})
          </button>
        ))}
        <span className="gm-spacer" />
        {shotDone && <span className="gm-done">{shotDone}</span>}
        {tempPin && (
          <button className="gm-btn wide" onClick={keepTempPin}
                  title={`Save this pin permanently${tempPin.kind ? ` under ${kindLabel(tempPin.kind)}` : ""}`}>
            📌 Keep pin
          </button>
        )}
        {tempGroup && tempGroup.pins.length > 0 && (
          <button className="gm-btn wide" onClick={keepTempGroup}
                  title={`Save all ${tempGroup.pins.length} pins permanently as ${kindLabel(groupKind)}`}>
            💾 Save {tempGroup.pins.length} as {kindLabel(groupKind)}
          </button>
        )}
        <button className={"gm-btn wide" + (pinMode ? " on" : "")}
                title={pinMode ? "Done placing pins" : "Add a pin: click the map"}
                onClick={() => { setPinMode((v) => !v); setDraft(null); }}>
          📍{pinMode ? " done" : ""}
        </button>
        {onSaveShot && (
          <button className="gm-btn wide" title="Save this view to Assets"
                  onClick={() => setCapOpen((v) => !v)}>📷</button>
        )}
        <button className="gm-btn" onClick={() => setScale((s) => Math.min(8, s * 1.3))}>＋</button>
        <button className="gm-btn" onClick={() => setScale((s) => Math.max(0.15, s / 1.3))}>－</button>
        {onClose && <button className="gm-btn" onClick={onClose} title="Back">✕</button>}
      </div>

      {pinMode && !draft && (
        <div className="gm-hint">Click the map to place a pin · click one of your pins to edit it · Delete button or Backspace removes the selected pin</div>
      )}

      {capOpen && (
        <div className="gm-cap">
          <input autoFocus placeholder="Caption (optional)" value={caption}
                 onChange={(e) => setCaption(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") takeShot();
                   if (e.key === "Escape") { setCapOpen(false); setCaption(""); }
                 }} />
          <button className="gm-btn wide" disabled={shotBusy} onClick={takeShot}>
            {shotBusy ? "Saving…" : "Save shot"}
          </button>
          <button className="gm-btn" onClick={() => { setCapOpen(false); setCaption(""); }}>✕</button>
        </div>
      )}

      <div ref={wrap} className={"gm-view" + (pinMode ? " pinning" : "")}
           onWheel={onWheel} onMouseDown={onDown} onMouseMove={onMove}
           onMouseUp={endDrag} onMouseLeave={endDrag} onClick={onViewClick}>
        <div className="gm-plane"
             style={{ width: SPACE, height: SPACE,
                      transform: `translate(${tx}px, ${ty}px) scale(${scale})` }}>
          <img className="gm-tex" src={map.texture} alt={map.zone} draggable={false}
               crossOrigin="anonymous"
               onError={(e) => {
                 // Retry once with a cache-buster (a stale webview cache entry or
                 // one-off hiccup shouldn't kill the whole map) — then report.
                 const img = e.currentTarget;
                 if (!img.src.includes("rty=")) {
                   img.src = map.texture + (map.texture.includes("?") ? "&" : "?") + "rty=1";
                   return;
                 }
                 onTextureError?.();
               }} />
          {shown.map((m, i) => (
            <div key={i} className={"gm-marker k-" + m.kind}
                 style={{ left: m.x, top: m.y, transform: `scale(${1 / scale})` }}
                 title={m.detail || m.label}>
              {m.kind === "area" ? null : m.icon && !brokenIcons.has(m.icon) ? (
                <img className="gm-ico" src={m.icon} alt="" draggable={false}
                     crossOrigin="anonymous"
                     onError={(e) => {
                       // One transient 404 shouldn't cost the icon: retry once
                       // with a cache-buster (the backend refetches upstream),
                       // THEN fall back to a tiny dot — never a broken-image square.
                       const img = e.currentTarget;
                       if (!img.src.includes("rty=")) {
                         img.src = m.icon + (m.icon.includes("?") ? "&" : "?") + "rty=1";
                         return;
                       }
                       setBrokenIcons((prev) => new Set(prev).add(m.icon));
                     }} />
              ) : (
                <span className="gm-fb" />
              )}
              {m.label && <span className="gm-label">{m.label}</span>}
            </div>
          ))}
          {/* Temporary AREA: a translucent dashed circle (mob zone, FATE ring,
              node cluster, fishing hole) — Garland-style. In map space, so it
              zooms with the map; NOT counter-scaled like the pin glyph. */}
          {tempPin?.radiusPx ? (
            <div
              className={"gm-area" + (/(fishing|spearfishing)/.test(tempPin.icon || "") ? " water" : "")}
              style={{
                left: tempPin.x - tempPin.radiusPx,
                top: tempPin.y - tempPin.radiusPx,
                width: tempPin.radiusPx * 2,
                height: tempPin.radiusPx * 2,
                // Dashed border thickness must stay readable at any zoom.
                borderWidth: Math.max(1.5, 2.5 / scale),
              }}
            />
          ) : null}
          {tempPin && (
            <div className="gm-marker k-custom"
                 style={{ left: tempPin.x, top: tempPin.y, transform: `scale(${1 / scale})` }}
                 title={`${tempPin.label} — temporary pin; opens another zone and it's gone (📌 Keep pin saves it)`}>
              {tempPin.icon ? (
                <img className="gm-pin-icon" src={api.iconByName(tempPin.icon)} alt=""
                     onError={(e) => { e.currentTarget.style.display = "none"; }} />
              ) : (
                <span className="gm-pin temp" />
              )}
              {tempPin.label && <span className="gm-label">{tempPin.label}</span>}
            </div>
          )}
          {/* A CATEGORY of temporary pins ("all the aether currents") — same
              transient treatment as the single temp pin, one glyph per point. */}
          {tempGroup?.pins.map((p, i) => (
            <div key={"tg" + i} className="gm-marker k-custom"
                 style={{ left: p.x, top: p.y, transform: `scale(${1 / scale})` }}
                 title={`${p.label || tempGroup.category} — temporary; 💾 Save keeps the whole set`}>
              {tempGroup.icon ? (
                <img className="gm-pin-icon" src={api.iconByName(tempGroup.icon)} alt=""
                     onError={(e) => { e.currentTarget.style.display = "none"; }} />
              ) : (
                <span className="gm-pin temp" />
              )}
              {p.label && <span className="gm-label">{p.label}</span>}
            </div>
          ))}
          {visiblePins.map((p) => (
            <div key={p.id} className="gm-marker k-custom"
                 style={{ left: p.x, top: p.y, transform: `scale(${1 / scale})` }}
                 title={`${p.label || "Your pin"} (${gameCoord(p.x)}, ${gameCoord(p.y)}) — click to select; Delete button or Backspace removes`}
                 onClick={(e) => { e.stopPropagation(); openEditor(p); }}>
              {p.icon ? (
                // A KEPT typed pin wears its game symbol (a mining pickaxe stays a
                // pickaxe) — falls back to the coloured dot if the icon 404s.
                <img className="gm-pin-icon" src={api.iconByName(p.icon)} alt=""
                     onError={(e) => { e.currentTarget.style.display = "none"; }} />
              ) : (
                <span className="gm-pin custom"
                      style={{ background: p.color || PIN_COLORS[0] }} />
              )}
              {p.label && <span className="gm-label">{p.label}</span>}
            </div>
          ))}
          {draft && (
            <div className="gm-marker k-custom"
                 style={{ left: draft.x, top: draft.y, transform: `scale(${1 / scale})` }}>
              <span className="gm-pin custom" style={{ background: draftColor }} />
              <div className="gm-pin-form" onClick={(e) => e.stopPropagation()}>
                <input className="gm-pin-input" autoFocus placeholder="Label this pin…"
                       value={draftLabel}
                       onChange={(e) => setDraftLabel(e.target.value)}
                       onKeyDown={(e) => {
                         if (e.key === "Enter") saveDraft();
                         if (e.key === "Escape") setDraft(null);
                       }} />
                <div className="gm-swatches">
                  {PIN_COLORS.map((c) => (
                    <button key={c} className={"gm-swatch" + (c === draftColor ? " on" : "")}
                            style={{ background: c }} title={c}
                            onClick={() => setDraftColor(c)} />
                  ))}
                </div>
              </div>
            </div>
          )}
          {editing && (
            <div className="gm-marker k-custom"
                 style={{ left: editing.x, top: editing.y, transform: `scale(${1 / scale})` }}>
              <div className="gm-pin-form" onClick={(e) => e.stopPropagation()}>
                {/* NOT autoFocus: a just-selected pin must answer to Backspace
                    (delete) — focusing the input would make Backspace edit the
                    label instead. Click the field to rename. */}
                <input className="gm-pin-input" placeholder="Label this pin…"
                       value={editLabel}
                       onChange={(e) => setEditLabel(e.target.value)}
                       onKeyDown={(e) => {
                         if (e.key === "Enter") saveEdit();
                         if (e.key === "Escape") setEditing(null);
                       }} />
                <div className="gm-swatches">
                  {PIN_COLORS.map((c) => (
                    <button key={c} className={"gm-swatch" + (c === editColor ? " on" : "")}
                            style={{ background: c }} title={c}
                            onClick={() => setEditColor(c)} />
                  ))}
                  <span className="gm-form-spacer" />
                  <button className="gm-form-btn"
                          disabled={pinWatched}
                          title="Show this pin as a chip on the in-game overlay"
                          onClick={() => {
                            if (!editing) return;
                            const label = editLabel || "Map pin";
                            void api.overlayWatchAdd({
                              kind: "pin", label, zone: map.zone,
                              place: { zone: map.zone,
                                       pin: { x: editing.x, y: editing.y, label, space: "map" } },
                            }).then(() => setPinWatched(true)).catch(() => {});
                          }}>
                    {pinWatched ? "⏱ ✓" : "⏱ Watch"}
                  </button>
                  <button className="gm-form-btn" onClick={saveEdit}>Save</button>
                  <button className="gm-form-btn danger"
                          onClick={() => editing && removePin(editing)}>Delete</button>
                </div>
              </div>
            </div>
          )}
        </div>
        {/* Zone name pinned to the viewport's corner, like the in-game map
            window — it stays put while the map pans and zooms under it. */}
        <div className="gm-zonename">{map.zone}</div>
      </div>

    </div>
  );
}
