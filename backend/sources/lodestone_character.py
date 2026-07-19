"""SKETCH — Lodestone character import for the player profile.

Mirrors lodestone.py's design: curl_cffi (Chrome impersonation) against the
bot-sensitive Lodestone, live-first with a thin per-character cache fallback.
No official API exists, so this scrapes these public pages:

    search:    /lodestone/character/?q=<name>&worldname=<world>   -> resolve name -> id
    main:      /lodestone/character/<id>/                         -> identity, world/DC, active job
    class_job: /lodestone/character/<id>/class_job/               -> every job + level
    minion:    /lodestone/character/<id>/minion/                  -> minion total
    mount:     /lodestone/character/<id>/mount/                   -> mount total + names

All CSS selectors below were verified against the live site (Lodestone is
unversioned and drifts — the try/except + cache fallback is the safety net, and
any selector that returns None just leaves that profile field blank).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from curl_cffi import requests as cffi

from config import USER_AGENT
from paths import KNOWLEDGE_DIR
# Re-exported: callers (app.py) import LodestoneBlocked from this module.
from sources.lodestone_http import LodestoneBlocked, waf_get

BASE = "https://na.finalfantasyxiv.com"
CHAR_URL = BASE + "/lodestone/character/{id}/"
JOBS_URL = BASE + "/lodestone/character/{id}/class_job/"
MINION_URL = BASE + "/lodestone/character/{id}/minion/"
MOUNT_URL = BASE + "/lodestone/character/{id}/mount/"
SEARCH_URL = BASE + "/lodestone/character/"
CACHE_DIR = KNOWLEDGE_DIR / "characters"

# Safety valve on mount-name fetching: each name is its own tooltip request (the
# list markup carries only an icon), and the Lodestone's bot blocker triggers on
# BURSTS. ~200 covers every mount in the game today; the cap just stops a future
# expansion from silently turning a refresh into a 500-request storm.
MAX_MOUNT_NAMES = 250


# Lodestone groups jobs under role headings on the class_job page. We map each
# job name to a role so the profile can say "you main a healer" without the LLM
# having to know the job list. (Classes fold into their job, e.g. Marauder->WAR.)
ROLE_BY_JOB = {
    # Tank
    "Paladin": "Tank", "Gladiator": "Tank", "Warrior": "Tank", "Marauder": "Tank",
    "Dark Knight": "Tank", "Gunbreaker": "Tank",
    # Healer
    "White Mage": "Healer", "Conjurer": "Healer", "Scholar": "Healer",
    "Astrologian": "Healer", "Sage": "Healer",
    # Melee DPS
    "Monk": "Melee DPS", "Pugilist": "Melee DPS", "Dragoon": "Melee DPS",
    "Lancer": "Melee DPS", "Ninja": "Melee DPS", "Rogue": "Melee DPS",
    "Samurai": "Melee DPS", "Reaper": "Melee DPS", "Viper": "Melee DPS",
    # Physical Ranged DPS
    "Bard": "Ranged DPS", "Archer": "Ranged DPS", "Machinist": "Ranged DPS",
    "Dancer": "Ranged DPS",
    # Magical Ranged DPS
    "Black Mage": "Caster DPS", "Thaumaturge": "Caster DPS", "Summoner": "Caster DPS",
    "Arcanist": "Caster DPS", "Red Mage": "Caster DPS", "Pictomancer": "Caster DPS",
    "Blue Mage": "Caster DPS",
}


@dataclass
class Job:
    name: str
    level: int          # 0 == unleveled ("-" on the page)
    role: str = ""


@dataclass
class GearPiece:
    """One equipped item, from its Lodestone equipment tooltip."""
    slot: str           # the item's category, e.g. "Rogue's Arm", "Head", "Ring"
    name: str           # "Augmented Shinobi Knives"
    item_level: int = 0
    bonuses: str = ""   # "Dexterity +409, Vitality +456…"
    materia: str = ""


@dataclass
class Character:
    id: str
    name: str = ""
    world: str = ""            # "Cactuar"
    data_center: str = ""      # "Aether"
    title: str = ""
    race_clan: str = ""        # "Au Ra / Raen / ♀"
    nameday: str = ""          # "10th Sun of the 5th Umbral Moon"
    guardian: str = ""         # "Menphina, the Lover"
    city_state: str = ""       # "Ul'dah"
    grand_company: str = ""    # "Immortal Flames / Second Flame Lieutenant" ("" if none)
    free_company: str = ""     # "" if none
    active_job: str = ""       # job shown on the profile face
    active_level: int = 0
    portrait: str = ""
    jobs: list[Job] = field(default_factory=list)
    gear: list[GearPiece] = field(default_factory=list)   # currently equipped
    # Collections. Counts are cheap (one page each); mount NAMES cost a request
    # apiece, so we pay it for mounts only — see _fetch_collections.
    collections_public: bool = False   # False when the player hides them on the Lodestone
    minion_count: int = 0
    mount_count: int = 0
    mounts: list[str] = field(default_factory=list)       # names; may be capped
    synced_at: str = ""        # ISO-8601 UTC of the last successful live scrape

    @property
    def equipment(self) -> list[GearPiece]:
        """Equipped GEAR — i.e. everything except the soul crystal.

        The job stone reports a real item level (i30) but isn't gear and can't be
        upgraded, so counting it would make it the permanent "lowest piece" and drag
        the average well below what the player means by "my ilvl" (e.g. 559 vs 607).
        """
        return [g for g in self.gear
                if g.item_level > 0 and g.slot.strip().lower() != "soul crystal"]

    @property
    def average_item_level(self) -> int:
        """Mean item level of equipped gear — the number players mean by "my ilvl"."""
        lv = [g.item_level for g in self.equipment]
        return round(sum(lv) / len(lv)) if lv else 0

    @property
    def lowest_gear(self) -> GearPiece | None:
        """The weakest equipped piece — the usual "what should I upgrade next"."""
        eq = self.equipment
        return min(eq, key=lambda g: g.item_level) if eq else None

    @property
    def max_level_jobs(self) -> list[Job]:
        top = max((j.level for j in self.jobs), default=0)
        return [j for j in self.jobs if j.level == top and top > 0]

    @property
    def role_focus(self) -> str:
        """Dominant role among leveled jobs — a strong profile signal."""
        from collections import Counter
        c = Counter(j.role for j in self.jobs if j.level >= 50 and j.role)
        return c.most_common(1)[0][0] if c else ""


@dataclass
class SearchHit:
    id: str
    name: str
    world: str


def _parse_gear_tooltip(html: str) -> GearPiece | None:
    """One equipment tooltip -> a GearPiece.

    Verified live: the tooltip carries name, category ("Rogue's Arm"), "Item Level
    660", the stat bonuses, and any melded materia.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    def txt(sel: str) -> str:
        el = soup.select_one(sel)
        return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""

    name = txt(".db-tooltip__item__name")
    if not name:
        return None
    ilvl = 0
    m = re.search(r"Item Level\s*(\d+)", txt(".db-tooltip__item__level"))
    if m:
        ilvl = int(m.group(1))
    materia = " / ".join(
        re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        for li in soup.select(".db-tooltip__materia__list li, .db-tooltip__materia li")
    )
    return GearPiece(
        slot=txt(".db-tooltip__item__category") or "Unknown",
        name=name,
        item_level=ilvl,
        bonuses=txt(".db-tooltip__basic_bonus")[:220],
        materia=materia[:160],
    )


class CharacterClient:
    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._s = cffi.Session(impersonate="chrome", headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._s.close()

    # --- public API ---
    def find(self, name: str, world: str = "") -> list[SearchHit]:
        """Resolve a character name (+ optional world) to candidate IDs.
        Raises LodestoneBlocked if the WAF challenge is in the way."""
        html = self._get(SEARCH_URL, params={"q": name, "worldname": world})
        return self._parse_search(html)

    def character(self, char_id: str) -> Character | None:
        """Full profile for a Lodestone id. Live, with per-character cache fallback.
        Raises LodestoneBlocked when the WAF blocks and there's no cached copy."""
        char_id = str(char_id).strip()
        try:
            main = self._get(CHAR_URL.format(id=char_id))
            jobs = self._get(JOBS_URL.format(id=char_id))
            char = self._parse_character(char_id, main, jobs)
            if char and char.name:
                char.gear = self._fetch_gear(main)
                self._fetch_collections(char, main)
                char.synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._write_cache(char)
                return char
        except LodestoneBlocked:
            cached = self._read_cache(char_id)
            if cached:
                return cached
            raise
        except Exception:
            pass
        return self._read_cache(char_id)

    # --- parsing (selectors verified against the live Lodestone) ---
    def _parse_search(self, html: str) -> list[SearchHit]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        hits: list[SearchHit] = []
        for e in soup.select(".entry"):
            a = e.select_one("a.entry__link")
            name = e.select_one(".entry__name")
            world = e.select_one(".entry__world")
            if not (a and name):                       # trailing non-character rows -> skip
                continue
            m = re.search(r"/character/(\d+)", a.get("href", ""))
            if not m:
                continue
            hits.append(SearchHit(
                id=m.group(1),
                name=name.get_text(strip=True),
                world=world.get_text(strip=True) if world else "",
            ))
        return hits

    def _parse_character(self, char_id: str, main_html: str, jobs_html: str) -> Character:
        from bs4 import BeautifulSoup
        m = BeautifulSoup(main_html, "html.parser")

        def txt(sel, root=m):
            el = root.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""

        char = Character(id=char_id)
        char.name = txt(".frame__chara__name")
        char.title = txt(".frame__chara__title")

        world_dc = txt(".frame__chara__world")                     # "Cactuar [Aether]"
        wm = re.match(r"(.+?)\s*\[(.+?)\]", world_dc)
        if wm:
            char.world, char.data_center = wm.group(1).strip(), wm.group(2).strip()
        else:
            char.world = world_dc

        face = m.select_one(".frame__chara__face img")
        char.portrait = face.get("src", "") if face else ""

        # Identity blocks share a class; disambiguate by each block's title heading.
        # Iterate the granular `__box` (one title/value pair each) so paired blocks
        # like Nameday + Guardian are read separately; Nameday's value lives in
        # `__birth`, the rest in `__name`.
        for box in (m.select(".character-block__box") or m.select(".character-block")):
            title = txt(".character-block__title", box).lower()
            value = txt(".character-block__name", box) or txt(".character-block__birth", box)
            if not value:
                continue
            if "race" in title:
                char.race_clan = value.replace("/ ", " / ")
            elif "nameday" in title:
                char.nameday = value
            elif "guardian" in title:
                char.guardian = value
            elif "city-state" in title or "city state" in title:
                char.city_state = value
            elif "grand company" in title:
                char.grand_company = value
        fc = m.select_one(".character__freecompany__name h4")
        if fc:
            char.free_company = fc.get_text(strip=True)

        active = txt(".character__class__data p")                  # "LEVEL 22"
        al = re.search(r"(\d+)", active)
        char.active_level = int(al.group(1)) if al else 0

        # Jobs page — one li per class/job.
        j = BeautifulSoup(jobs_html, "html.parser")
        for li in j.select(".character__job li"):
            name = txt(".character__job__name", li)
            lvl_raw = txt(".character__job__level", li)            # "22" or "-"
            if not name:
                continue
            lvl = int(lvl_raw) if lvl_raw.isdigit() else 0
            char.jobs.append(Job(name=name, level=lvl, role=ROLE_BY_JOB.get(name, "")))

        # Active job = the highest-level job (the face icon has no text label).
        if char.jobs:
            top = max(char.jobs, key=lambda x: x.level)
            char.active_job = top.name
            char.active_level = char.active_level or top.level
        return char

    # --- equipped gear ---
    def _fetch_gear(self, main_html: str) -> list[GearPiece]:
        """Read every equipped item off the character page.

        The page itself only renders gear ICONS — each carries a
        `data-lazy_load_url` (…/equipment/tooltip/<n>) that the site fetches on hover,
        and THAT is where the name/item level/bonuses live. So there's one request per
        equipped slot (~13). Deliberately uncached: gear is the thing that changes, and
        this only runs on an explicit character import/refresh.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(main_html, "html.parser")
        urls: list[str] = []
        for el in soup.select("[data-lazy_load_url]"):
            u = el.get("data-lazy_load_url") or ""
            if "/equipment/tooltip/" in u and u not in urls:
                urls.append(u)

        out: list[GearPiece] = []
        for u in urls:
            try:
                piece = _parse_gear_tooltip(self._get(BASE + u))
            except LodestoneBlocked:
                raise                      # caller decides (cache fallback / tell the player)
            except Exception:
                continue                   # one bad slot must not lose the rest
            if piece:
                out.append(piece)
        return out

    # --- collections (minions / mounts) ---
    def _collection_page(self, url: str) -> tuple[int, list[str]]:
        """One collection page -> (total, list of tooltip hrefs).

        Verified live: the page shows a plain "Total: N", and each entry is a
        `li.<kind>__list_icon` carrying ONLY an icon plus a `data-tooltip_href`.
        There is no name in the list markup (alt is empty), which is why names cost
        one request each.
        """
        from bs4 import BeautifulSoup

        html = self._get(url)
        soup = BeautifulSoup(html, "html.parser")
        m = re.search(r"Total:\s*([\d,]+)", soup.get_text(" ", strip=True))
        total = int(m.group(1).replace(",", "")) if m else 0
        hrefs = [li.get("data-tooltip_href") for li in soup.select("li[data-tooltip_href]")
                 if li.get("data-tooltip_href")]
        return total, hrefs

    def _tooltip_name(self, href: str) -> str:
        """Name out of a minion/mount tooltip (h4.<kind>__header__label)."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self._get(BASE + href), "html.parser")
        el = soup.select_one("h4[class$='__header__label']") or soup.select_one("h4")
        return el.get_text(" ", strip=True) if el else ""

    @staticmethod
    def _collections_enabled(main_html: str) -> bool:
        """Does this character expose Minions/Mounts publicly?

        The Lodestone tells us directly in the character menu: a public collection is
        a real link, a private one is rendered as a dead span —

            <li class="character_menu__link"><a href="…/class_job/">Class/Job</a></li>
            <li class="disable"><span class="disable">Minions</span></li>

        We read that instead of probing the pages, because the private pages return a
        200-shaped 404 body: waf_get doesn't inspect status codes, so a "Page not
        found" parses cleanly and yields Total=0 — indistinguishable from a real zero
        and far more dangerous, since "0 mounts" reads as "owns none".
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(main_html, "html.parser")
        for li in soup.select("li"):
            if li.get_text(strip=True) in ("Minions", "Mounts"):
                return bool(li.select_one("a[href]"))
        return False

    def _fetch_collections(self, char: "Character", main_html: str) -> None:
        """Fill in minion/mount counts, and mount names, on `char` in place.

        PRIVACY: these are gated by the player's own Lodestone setting, so "can't
        read them" means "they keep them private", NOT "the scraper is broken". We
        record collections_public=False and the profile then says so plainly rather
        than implying the player owns nothing.

        NAMES are deliberately asymmetric. The list markup carries no names, so each
        one is a separate request. Mounts are worth it (tens of them, and "what can I
        fly" is a real question). Minions are not — there are hundreds, they'd cost
        hundreds of requests against a burst-sensitive WAF, and they'd add thousands
        of tokens to a system prompt that is re-sent on every chat turn. Count only.
        """
        if not self._collections_enabled(main_html):
            char.collections_public = False
            return
        try:
            char.minion_count, _ = self._collection_page(MINION_URL.format(id=char.id))
            char.mount_count, mount_hrefs = self._collection_page(MOUNT_URL.format(id=char.id))
        except LodestoneBlocked:
            raise                      # caller decides (cache fallback / tell the player)
        except Exception:
            char.collections_public = False
            return

        char.collections_public = True
        names: list[str] = []
        for href in mount_hrefs[:MAX_MOUNT_NAMES]:
            try:
                n = self._tooltip_name(href)
            except LodestoneBlocked:
                raise
            except Exception:
                continue               # one bad tooltip must not lose the rest
            if n:
                names.append(n)
        char.mounts = sorted(names)

    # --- profile rendering: Character -> the markdown player.md wants ---
    def to_profile_markdown(self, c: Character, archive: dict | None = None) -> str:
        """Render the machine-owned identity block for profile/player.md.

        `archive` is the per-job gear store (workspaces.gear_archive). It exists
        because the Lodestone only ever publishes the set you logged out wearing, so
        this block has to be explicit about WHICH job each set belongs to, when it was
        seen, and where it came from — otherwise the assistant answers a Ninja
        question with Botanist numbers and sounds certain doing it.
        """
        leveled = sorted((j for j in c.jobs if j.level > 0),
                         key=lambda x: -x.level)
        job_line = ", ".join(f"{j.name} {j.level}" for j in leveled[:8]) or "(none leveled)"
        tops = ", ".join(j.name for j in c.max_level_jobs) or "(none)"
        title_line = f"- **Title:** {c.title}\n" if c.title else ""
        # Equipped gear, weakest first: this is what lets the assistant answer
        # "which piece should I upgrade?" instead of guessing from the job level.
        gear_block = ""
        if c.equipment:
            rows = "\n".join(
                f"  - {g.slot}: {g.name} (i{g.item_level})"
                for g in sorted(c.equipment, key=lambda g: g.item_level)
            )
            low = c.lowest_gear
            gear_block = (
                f"- **Average item level:** {c.average_item_level}\n"
                + (f"- **Weakest piece (upgrade first):** {low.name} — {low.slot} "
                   f"(i{low.item_level})\n" if low else "")
                + f"- **Equipped gear** (as worn on {c.active_job or 'the active job'}, "
                  f"weakest first; the soul crystal is the job stone, not gear):\n{rows}\n"
            )
        # Collections. When the player hides these on the Lodestone we must say so
        # explicitly — otherwise "Minions: 0" reads as "owns none" and the assistant
        # would happily recommend a minion they already have.
        if not c.collections_public:
            coll_block = (
                "- **Minions / Mounts:** hidden on the Lodestone (the player has them "
                "set to private, so they could not be read — do NOT treat this as "
                "owning none; ask, or tell them to make Minions/Mounts public on the "
                "Lodestone and hit Refresh)\n"
            )
        else:
            mount_names = ", ".join(c.mounts)
            capped = len(c.mounts) < c.mount_count
            coll_block = (
                f"- **Minions owned:** {c.minion_count} (names not imported — ask the "
                f"player if it matters)\n"
                f"- **Mounts owned:** {c.mount_count}\n"
                + (f"- **Mounts:** {mount_names}"
                   + (f" … (only the first {len(c.mounts)} of {c.mount_count} imported)"
                      if capped else "")
                   + "\n" if c.mounts else "")
            )
        synced_line = f"- **Profile last synced:** {c.synced_at}\n" if c.synced_at else ""
        archive_block = self._gear_archive_md(c, archive or {})
        return (
            "## Identity (imported from the Lodestone)\n"
            f"- **Character:** {c.name}\n"
            f"{title_line}"
            f"- **World / Data center:** {c.world} / {c.data_center}\n"
            f"- **Race/Clan:** {c.race_clan}\n"
            f"- **Nameday:** {c.nameday or '(unknown)'}\n"
            f"- **Guardian:** {c.guardian or '(unknown)'}\n"
            f"- **City-state:** {c.city_state or '(unknown)'}\n"
            f"- **Grand Company:** {c.grand_company or '(none)'}\n"
            f"- **Free Company:** {c.free_company or '(none)'}\n"
            f"- **Active job:** {c.active_job} (Lv {c.active_level})\n"
            f"- **Jobs (leveled):** {job_line}\n"
            f"- **Highest-level jobs:** {tops}\n"
            f"- **Likely role focus:** {c.role_focus or '(unclear)'}\n"
            f"{gear_block}"
            f"{coll_block}"
            f"{synced_line}"
            f"{archive_block}"
        )

    @staticmethod
    def _gear_archive_md(c: Character, archive: dict) -> str:
        """Every job we've ever seen gear for, with its age and provenance.

        Uses '### ' rather than '## ' on purpose: workspaces._merge_identity replaces
        everything from the Identity heading up to the next '## ', so a '## ' here
        would fall outside the machine-owned block and never refresh again.
        """
        if not archive:
            return ""
        rows = []
        for job, e in sorted(archive.items(), key=lambda kv: -(kv[1].get("average_item_level") or 0)):
            pieces = e.get("pieces") or []
            avg = e.get("average_item_level") or 0
            seen = (e.get("observed") or "")[:10]
            src = {"gearset_file": "read from the game's own saved gearset — exact",
                   "lodestone": "Lodestone logout snapshot",
                   "screenshot": "read from a screenshot you sent — may be misread",
                   "player": "you told us"}.get(e.get("source", ""), e.get("source", ""))
            low = min((p for p in pieces if p.get("item_level")),
                      key=lambda p: p["item_level"], default=None)
            rows.append(
                f"- **{job}** — avg i{avg}, {len(pieces)} pieces "
                f"(seen {seen}; {src})"
                + (f"\n  - weakest: {low['name']} — {low['slot']} (i{low['item_level']})"
                   if low else "")
            )
        known = ", ".join(sorted(archive))
        return (
            "\n### Gear known per job\n"
            "The Lodestone only publishes the set the player logged out wearing, so "
            "this is an archive built up over time — one entry per job we have ever "
            "seen. It is the ONLY gear data available.\n"
            f"- **Jobs with known gear:** {known}\n"
            f"- **Any job not listed above has NO gear data.** Do not answer gear "
            f"questions about it from the numbers here — those belong to a different "
            f"job. Ask the player to log out on that job and hit Refresh, or to send "
            f"a screenshot of their in-game character window.\n"
            + "\n".join(rows) + "\n"
        )

    # --- internals (mirrors lodestone.py) ---
    def _get(self, url: str, params: dict | None = None, retries: int = 3) -> str:
        """GET a Lodestone page, retrying through the intermittent AWS WAF challenge."""
        return waf_get(self._s, url, params=params, timeout=self._timeout, retries=retries)

    def _cache_path(self, char_id: str):
        return CACHE_DIR / f"{char_id}.json"

    def _write_cache(self, char: Character) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache_path(char.id).write_text(
            json.dumps(asdict(char), ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_cache(self, char_id: str) -> Character | None:
        try:
            data = json.loads(self._cache_path(char_id).read_text(encoding="utf-8"))
            data["jobs"] = [Job(**jd) for jd in data.get("jobs", [])]
            data["gear"] = [GearPiece(**gd) for gd in data.get("gear", [])]
            return Character(**data)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None
