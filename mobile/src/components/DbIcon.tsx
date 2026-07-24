import { useEffect, useState } from "react";
import { currentBase, iconObjectUrl, isDesktopIcon, normalizeIconUrl } from "../lib/icons";

// One database icon. Two kinds arrive and they need different handling:
//
//  * Garland's own art (garlandtools.org/files/icons/…) is public — <img src>
//    loads it directly.
//  * The desktop's game-client art (/map/icon, /icons/by-name) is served by the
//    backend as an ABSOLUTE LOOPBACK url and sits behind the companion token
//    gate. On the phone, 127.0.0.1 is the phone, and an <img> tag cannot send an
//    Authorization header — so those are re-pointed at the paired desktop and
//    fetched with the token, then shown as an object URL.
//
// A failed icon renders as nothing rather than a broken-image glyph: these lists
// are long, and one missing sprite shouldn't punch a hole in the row.
export default function DbIcon({ url, className }: { url?: string; className?: string }) {
  const direct = !!url && !isDesktopIcon(url);
  const [src, setSrc] = useState<string>(direct && url ? url : "");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    if (!url) { setSrc(""); return; }
    if (!isDesktopIcon(url)) { setSrc(url); setFailed(false); return; }
    // Desktop-served: needs the bearer token, so fetch it rather than <img src>.
    setSrc("");
    iconObjectUrl(normalizeIconUrl(url, currentBase()))
      .then((u) => { if (live) { setSrc(u); setFailed(!u); } })
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [url]);

  // No icon for this record at all — render nothing, so the row sits flush.
  if (!url || failed) return null;
  // Expected but still loading: hold the slot at the same size, so the row's
  // text doesn't jump sideways when the sprite lands.
  if (!src) return <span className={`${className ?? ""} icon-ph`} aria-hidden="true" />;
  return <img className={className} src={src} alt="" loading="lazy" onError={() => setFailed(true)} />;
}
