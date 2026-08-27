"""
seed_covers.py — pre-seed the photo index with reference cover art.

Why: photo identify is slow because an unrecognised cover falls through to the
vision model (a network round-trip + inference + free-tier queueing). The
desktop vault already matches a query photo against stored covers FIRST
(angle-robust CLIP, dHash fallback) and only calls the model on a miss. But
that index starts empty -- it only fills as you confirm scans -- so early on
almost everything is a slow model call.

This seeds it up front: for each catalog title it fetches the box art and
stores it under that title, so a straight-on cover photo resolves instantly
against the seed instead of waiting on the model. It runs on the DESKTOP,
because add_photo needs Pillow (and CLIP), which never enter the APK.

Sources (pick with --source):
  * libretro (default) -- the libretro-thumbnails project: free, no API key,
    box art named by title. Files are No-Intro style: "Title (Region)
    (extras).png". We match a catalog title to the best regional boxart,
    preferring USA/World, and skip disc-2 / demo scans.
  * thegamesdb -- TheGamesDB's documented v1 API. Carries front AND back
    boxart, needs a free key (requested on their forum), and reports
    remaining_monthly_allowance on every response, which is honoured as a hard
    stop. Prefer this over coverproject: same coverage, no terms ambiguity.
  * coverproject -- thecoverproject.net, a volunteer scan community. Covers
    the ~30% libretro misses and also has BACK covers. Their published
    automated-access terms are encoded as hard limits below (see
    _CP_MIN_INTERVAL and _CP_WINDOW_UTC); they are deliberately not
    CLI-configurable downward, so a careless later run cannot exceed them.

IMPORTANT LIMITS, stated honestly:
  * This speeds up AT-DESK identify (desktop reachable, CLIP matches the
    reference art). It does NOT speed up field identify with the desktop
    unreachable: the phone has no image decoder (stdlib only, no Pillow), so
    it cannot match on-device -- that would need the browser to compute a
    hash, a separate future item.
  * Reference art is one clean image; a glare-y, angled phone photo may still
    miss and fall through to the model. The seed raises the hit rate; it does
    not guarantee one.
  * Cover art is copyrighted. This stores it locally as a private recognition
    index for your own tool, not for redistribution. Be a good citizen: the
    default is an explicit, bounded scope, with a polite delay between fetches.

    python seed_covers.py --curated --limit 100     # seed 100 curated titles
    python seed_covers.py --title "Ico" --title "Okami"
    python seed_covers.py --source coverproject --limit 50 --dry-run
    python seed_covers.py --source coverproject --probe   # show parsed links
    python seed_covers.py --stats
"""

from __future__ import annotations

import argparse
import base64
import datetime
import difflib
import html.parser
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import catalog
import httpjson

REPO = "libretro-thumbnails/Sony_-_PlayStation_2"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/master?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/master/Named_Boxarts/"

# difflib ratio below which a fuzzy title match is rejected as too weak.
_FUZZY_MIN = 0.92

# Region preference: lower is better.
_REGION_ORDER = (("usa", 0), ("world", 0), ("europe", 1), ("japan", 2))


def _norm(name: str) -> str:
    """Normalise a title for matching: drop parenthetical tags, punctuation,
    and a leading article, and fold '&' to 'and'. 'Devil May Cry 3: Dante's
    Awakening' and 'Devil May Cry 3 - Dante's Awakening (USA)' collapse to the
    same key. Accents are folded (libretro writes 'Ōkami', our catalog 'Okami'),
    so diacritics don't split a match."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"\([^)]*\)", " ", name)
    name = name.lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9]+", " ", name)
    name = re.sub(r"^(the|a|an)\s+", "", name)
    return " ".join(name.split())


def _region_rank(filename: str) -> int:
    tags = " ".join(re.findall(r"\(([^)]*)\)", filename)).lower()
    rank = 3
    for needle, r in _REGION_ORDER:
        if needle in tags:
            rank = min(rank, r)
    # Deprioritise non-primary scans (extra discs, demos, betas).
    if re.search(r"disc [2-9]|taikenban|demo|beta|sample|proto", tags):
        rank += 10
    return rank


class LibretroCovers:
    """Index of libretro boxart filenames, matched to catalog titles."""

    def __init__(self, opener=None):
        self._opener = opener            # test seam; None -> real urlopen
        self.by_key: dict[str, list[str]] = {}
        self.loaded = False

    def load(self) -> int:
        """Fetch the full boxart filename list and index it by normalised key."""
        kwargs = {}
        if self._opener is not None:
            kwargs["opener"] = self._opener
        status, data = httpjson.get_json(TREE_URL, **kwargs)
        if status != 200 or not isinstance(data, dict):
            return 0
        for entry in data.get("tree", []) or []:
            path = entry.get("path", "")
            if not (path.startswith("Named_Boxarts/") and path.endswith(".png")):
                continue
            filename = path[len("Named_Boxarts/"):]
            key = _norm(filename[:-4])          # strip ".png" before keying
            self.by_key.setdefault(key, []).append(filename)
        self.loaded = True
        return sum(len(v) for v in self.by_key.values())

    def best_filename(self, title: str) -> str | None:
        """The best regional boxart filename for a catalog title, or None."""
        key = _norm(title)
        candidates = self.by_key.get(key)
        if not candidates:
            # Fuzzy fallback: closest normalised key above threshold.
            close = difflib.get_close_matches(key, self.by_key.keys(),
                                              n=1, cutoff=_FUZZY_MIN)
            if not close:
                return None
            candidates = self.by_key[close[0]]
        # Prefer USA/World, then Europe, then Japan; then the shortest name
        # (avoids "... (Demo)" variants that slipped through the rank).
        return sorted(candidates, key=lambda f: (_region_rank(f), len(f)))[0]

    def image_url(self, filename: str) -> str:
        return RAW_BASE + urllib.parse.quote(filename)

    def fetch_png(self, filename: str, timeout: float = 30.0) -> bytes | None:
        url = self.image_url(filename)
        opener = self._opener or urllib.request.urlopen
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": httpjson._UA})
            with opener(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) not in (200, None):
                    return None
                return resp.read()
        except Exception:                               # noqa: BLE001
            return None


# --------------------------------------------------------------------------
# thecoverproject.net
#
# Their automated-access terms, encoded rather than documented, because a
# comment is not a constraint. Do not expose these as CLI flags that can lower
# them: the whole point is that the tool cannot be turned into a hammer later.
#   * 1 request / 2 seconds, single connection, no concurrency.
#   * Batch runs during off-peak hours, 02:00-06:00 UTC.
#   * Downloaded art is stored locally (add_photo does this); never hotlinked.
#   * Personal, non-commercial use only.
# --------------------------------------------------------------------------

CP_BASE = "https://www.thecoverproject.net/"
CP_PS2_CAT = 6                       # PS2 category id; this tool is PS2-only.
_CP_MIN_INTERVAL = 2.0               # seconds between requests -- a floor.
_CP_WINDOW_UTC = (2, 6)              # [start, end) hours for batch runs.

# Identifies the tool honestly rather than impersonating a browser. If their
# edge rejects this, the answer is to ask them to allowlist it -- not to put a
# Chrome string here. See _cp_explain_403.
_CP_UA = ("spine-ps2arb-coverseeder/1.0 (personal, non-commercial; "
          "1 req/2s single-threaded; +https://github.com/kalebkeen/Ebay-Webscraper)")


class _RateLimiter:
    """A floor on the gap between requests, shared by every instance.

    Class-level state on purpose: two CoverProjectCovers objects in one
    process still queue behind one another, so 'single connection thread'
    holds even if a caller constructs more than one.
    """

    # None, not 0.0: a monotonic clock legitimately reads 0.0 near process
    # start, and testing `if cls._last` would then skip the very first gap.
    _last: float | None = None

    @classmethod
    def wait(cls, interval: float = _CP_MIN_INTERVAL, *, sleep=time.sleep,
             clock=time.monotonic) -> None:
        if cls._last is not None:
            gap = clock() - cls._last
            if gap < interval:
                sleep(interval - gap)
        cls._last = clock()


def in_offpeak_window(now=None, window=_CP_WINDOW_UTC) -> bool:
    """True if UTC now falls in their requested batch window."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return window[0] <= now.hour < window[1]


def _cp_explain_403() -> str:
    return (
        "thecoverproject returned 403 for a self-identifying bot.\n"
        "  Their published terms permit rate-limited automated access, so this\n"
        "  is most likely an edge rule (e.g. Cloudflare bot-fight) blocking all\n"
        "  non-browser clients regardless of site policy.\n"
        "  The fix is to ask them to allowlist this user-agent, NOT to send a\n"
        "  browser user-agent -- that would be evading the control rather than\n"
        "  operating within their terms.\n"
        f"  User-agent used: {_CP_UA}"
    )


class _LinkParser(html.parser.HTMLParser):
    """Collects (tag, attr, url, text) for anchors and images.

    Kept deliberately dumb. The site's markup was not inspectable while this
    was written (their edge 403s non-browser clients), so nothing here depends
    on a specific class name or nesting -- only on hrefs and srcs, which are
    the stable part of any page. --probe prints what this found so the
    selectors below can be corrected against reality in one pass.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []      # (href, link text)
        self.images: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self._href = d["href"]
            self._text = []
        elif tag == "img" and d.get("src"):
            self.images.append(d["src"])

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href, self._text = None, []


class CoverProjectCovers:
    """thecoverproject.net PS2 covers, matched to catalog titles.

    Same interface as LibretroCovers (load / best_filename / fetch_png), so
    run() drives either without knowing which it has. 'filename' here is an
    absolute cover URL rather than a bare name; run() only ever passes it back
    to fetch_png, so the distinction stays internal.
    """

    def __init__(self, opener=None, *, face: str = "front", limiter=_RateLimiter):
        self._opener = opener                       # test seam
        self._limiter = limiter
        self.face = face                            # "front" | "back"
        self.by_key: dict[str, str] = {}            # norm title -> game page
        self.loaded = False
        self.last_status = 0

    # -- transport ---------------------------------------------------------

    def _get(self, url: str, timeout: float = 30.0) -> bytes | None:
        """One rate-limited GET. Returns the body, or None on any failure."""
        self._limiter.wait()
        opener = self._opener or urllib.request.urlopen
        req = urllib.request.Request(url, headers={"User-Agent": _CP_UA})
        try:
            with opener(req, timeout=timeout) as resp:
                self.last_status = getattr(resp, "status", 200) or 200
                if self.last_status != 200:
                    return None
                return resp.read()
        except urllib.error.HTTPError as exc:
            self.last_status = exc.code
            return None
        except Exception:                               # noqa: BLE001
            self.last_status = 0
            return None

    def _get_text(self, url: str) -> str | None:
        raw = self._get(url)
        return raw.decode("utf-8", "replace") if raw else None

    # -- index -------------------------------------------------------------

    def load(self) -> int:
        """Index the PS2 category page: normalised title -> game page URL."""
        url = urllib.parse.urljoin(CP_BASE, f"view.php?cat_id={CP_PS2_CAT}")
        page = self._get_text(url)
        if not page:
            return 0
        parser = _LinkParser()
        parser.feed(page)
        for href, text in parser.links:
            if "game_id=" not in href or not text:
                continue
            key = _norm(text)
            if key:
                self.by_key.setdefault(key, urllib.parse.urljoin(CP_BASE, href))
        self.loaded = True
        return len(self.by_key)

    def best_filename(self, title: str) -> str | None:
        """Resolve a catalog title to a cover-image URL, or None.

        Two requests at most: the category index is already in memory, so this
        costs one game-page fetch per title (rate-limited like everything else).
        """
        key = _norm(title)
        page_url = self.by_key.get(key)
        if not page_url:
            close = difflib.get_close_matches(key, self.by_key.keys(),
                                              n=1, cutoff=_FUZZY_MIN)
            if not close:
                return None
            page_url = self.by_key[close[0]]
        return self._cover_url(page_url)

    def _cover_url(self, page_url: str) -> str | None:
        page = self._get_text(page_url)
        if not page:
            return None
        parser = _LinkParser()
        parser.feed(page)
        candidates = [h for h, _ in parser.links] + parser.images
        scored = []
        for href in candidates:
            if not re.search(r"\.(jpg|jpeg|png)(\?|$)", href, re.I):
                continue
            low = href.lower()
            if "thumb" in low or "icon" in low:      # never seed a thumbnail
                continue
            wanted, other = (self.face, "back" if self.face == "front" else "front")
            if other in low and wanted not in low:
                continue
            scored.append((0 if wanted in low else 1,
                           urllib.parse.urljoin(page_url, href)))
        if not scored:
            return None
        return sorted(scored)[0][1]

    def fetch_png(self, filename: str, timeout: float = 30.0) -> bytes | None:
        """`filename` is the absolute cover URL from best_filename."""
        return self._get(filename, timeout=timeout)

    # -- diagnostics -------------------------------------------------------

    def probe(self, url: str | None = None, log=print) -> dict:
        """Dump what the parser sees on one page, to correct selectors.

        Exists because the markup could not be inspected while this was
        written; one probe run replaces a round of guessing.
        """
        url = url or urllib.parse.urljoin(CP_BASE, f"view.php?cat_id={CP_PS2_CAT}")
        page = self._get_text(url)
        if not page:
            log(f"probe failed: HTTP {self.last_status} for {url}")
            if self.last_status == 403:
                log(_cp_explain_403())
            return {"status": self.last_status, "links": 0, "images": 0}
        parser = _LinkParser()
        parser.feed(page)
        game_links = [(h, t) for h, t in parser.links if "game_id=" in h]
        log(f"probe {url}: HTTP {self.last_status}, {len(parser.links)} links, "
            f"{len(parser.images)} images, {len(game_links)} game links")
        for href, text in parser.links[:15]:
            log(f"    a  {text[:40]!r:44} {href[:80]}")
        for src in parser.images[:10]:
            log(f"    img{'':43} {src[:80]}")
        return {"status": self.last_status, "links": len(parser.links),
                "images": len(parser.images), "games": len(game_links)}


# --------------------------------------------------------------------------
# TheGamesDB
#
# A documented API rather than a scrape, so the constraints here are the ones
# the API itself publishes: an allowance counter returned on every response,
# and a key you request on their forum. Carries BACK covers, which libretro
# does not have at all.
#
# Key handling follows the project rule: never entered here, never printed.
# Read from the desktop keystore (settings.Settings), else the environment.
# --------------------------------------------------------------------------

TGDB_API = "https://api.thegamesdb.net/v1"
TGDB_PS2_PLATFORM = 11               # Sony PlayStation 2.
_TGDB_PAGE_SIZE = 20                 # games per ByPlatformID page.


def _credential(field: str) -> str:
    """Read a credential from the settings store, the desktop keystore store,
    or the environment -- the same resolution sources.build_source uses, so
    there is one story about where credentials come from. Returns "" rather
    than raising, so a missing key is a clean 'not configured' message.
    """
    try:
        import settings as _settings
        value = _settings.resolve(field)
        if value and str(value).strip():
            return str(value).strip()
    except Exception:                                   # noqa: BLE001
        pass
    return (os.environ.get(field.upper(), "") or "").strip()


class TheGamesDBCovers:
    """TheGamesDB PS2 boxart, matched to catalog titles.

    Same load / best_filename / fetch_png interface as the other two sources.
    'filename' is an absolute CDN URL; run() only hands it back to fetch_png.

    Allowance is respected as a hard stop: every response carries
    remaining_monthly_allowance, and once it reaches zero this stops resolving
    rather than continuing to spend against an exhausted key.
    """

    def __init__(self, api_key: str | None = None, opener=None, *,
                 face: str = "front"):
        self._key = api_key if api_key is not None else _credential("thegamesdb_token")
        self._opener = opener
        self.face = face
        self.by_key: dict[str, int] = {}         # norm title -> game id
        self._images: dict[int, dict] = {}       # game id -> {side: url}
        self.base_url = ""
        self.remaining = None                    # None until the API tells us
        self.loaded = False
        self.last_status = 0

    # -- transport ---------------------------------------------------------

    def _api(self, path: str, params: dict):
        """GET a v1 endpoint. Returns the `data` block, or None."""
        if not self._key:
            return None
        query = dict(params, apikey=self._key)
        url = f"{TGDB_API}/{path}?" + urllib.parse.urlencode(query)
        kwargs = {"opener": self._opener} if self._opener is not None else {}
        status, payload = httpjson.get_json(url, **kwargs)
        self.last_status = status
        if status != 200 or not isinstance(payload, dict):
            return None
        # Every response reports what is left; track it so a batch can stop
        # itself instead of hammering an exhausted key.
        if "remaining_monthly_allowance" in payload:
            try:
                self.remaining = int(payload["remaining_monthly_allowance"])
            except (TypeError, ValueError):
                pass
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0

    # -- index -------------------------------------------------------------

    def load(self, max_pages: int = 200) -> int:
        """Page through the PS2 platform listing, indexing title -> game id."""
        if not self._key:
            return 0
        page = 1
        while page <= max_pages:
            data = self._api("Games/ByPlatformID", {
                "id": TGDB_PS2_PLATFORM,
                "fields": "players,publishers,genres",
                "page": page,
            })
            if not data:
                break
            games = data.get("games") or []
            if not games:
                break
            for game in games:
                title, gid = game.get("game_title"), game.get("id")
                if not title or gid is None:
                    continue
                key = _norm(str(title))
                if key:
                    self.by_key.setdefault(key, int(gid))
            if len(games) < _TGDB_PAGE_SIZE or self.exhausted():
                break
            page += 1
        self.loaded = bool(self.by_key)
        return len(self.by_key)

    def _game_id(self, title: str) -> int | None:
        key = _norm(title)
        gid = self.by_key.get(key)
        if gid is not None:
            return gid
        close = difflib.get_close_matches(key, self.by_key.keys(),
                                          n=1, cutoff=_FUZZY_MIN)
        return self.by_key[close[0]] if close else None

    def _load_images(self, gid: int) -> dict:
        """Fetch and cache the boxart sides for one game id."""
        if gid in self._images:
            return self._images[gid]
        data = self._api("Games/Images", {"games_id": gid,
                                          "filter[type]": "boxart"})
        sides: dict[str, str] = {}
        if data:
            base = data.get("base_url") or {}
            # Prefer the full-resolution original; fall back through the
            # smaller renditions rather than returning nothing.
            self.base_url = (base.get("original") or base.get("large")
                             or base.get("medium") or self.base_url)
            entries = (data.get("images") or {}).get(str(gid)) or []
            for img in entries:
                if img.get("type") != "boxart":
                    continue
                side = (img.get("side") or "").lower()
                filename = img.get("filename")
                if side and filename and side not in sides:
                    sides[side] = urllib.parse.urljoin(self.base_url, filename)
        self._images[gid] = sides
        return sides

    def best_filename(self, title: str) -> str | None:
        if self.exhausted():
            return None
        gid = self._game_id(title)
        if gid is None:
            return None
        sides = self._load_images(gid)
        # Honour the requested face exactly. Falling back to the other side
        # would silently seed a back cover as a front one, which would poison
        # the recognition index rather than merely miss.
        return sides.get(self.face)

    def fetch_png(self, filename: str, timeout: float = 30.0) -> bytes | None:
        """Images come off the CDN, unauthenticated and outside the allowance."""
        opener = self._opener or urllib.request.urlopen
        req = urllib.request.Request(filename,
                                     headers={"User-Agent": httpjson._UA})
        try:
            with opener(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) not in (200, None):
                    return None
                return resp.read()
        except Exception:                                   # noqa: BLE001
            return None

    # -- diagnostics -------------------------------------------------------

    def probe(self, url: str | None = None, log=print) -> dict:
        """One page of the platform listing, to confirm the key and the shape."""
        if not self._key:
            log("no thegamesdb_token configured. On the desktop:\n"
                "    python keystore.py set thegamesdb_token <key>\n"
                "  (or set THEGAMESDB_TOKEN in the environment)")
            return {"status": 0, "games": 0}
        data = self._api("Games/ByPlatformID",
                         {"id": TGDB_PS2_PLATFORM, "page": 1})
        if not data:
            log(f"probe failed: HTTP {self.last_status}")
            return {"status": self.last_status, "games": 0}
        games = data.get("games") or []
        log(f"probe ok: HTTP {self.last_status}, {len(games)} games on page 1, "
            f"allowance left: {self.remaining}")
        for game in games[:8]:
            log(f"    {game.get('id')!s:>8}  {game.get('game_title')}")
        return {"status": self.last_status, "games": len(games),
                "remaining": self.remaining}


def build_source(name: str, *, face: str = "front", opener=None):
    """Construct a cover source by name. Unknown names raise, so a typo on the
    command line fails loudly rather than silently seeding from the default."""
    if name == "libretro":
        return LibretroCovers(opener=opener)
    if name == "coverproject":
        return CoverProjectCovers(opener=opener, face=face)
    if name == "thegamesdb":
        return TheGamesDBCovers(opener=opener, face=face)
    raise ValueError(f"unknown source: {name}")


def _default_add_photo(image_b64: str, title: str, variant: str):
    import vault
    return vault.add_photo(image_b64, title, variant)


def run(covers, titles, *, add_photo=None, variant: str = "unknown",
        delay: float = 0.5, dry_run: bool = False, log=print) -> dict:
    """Seed each title's boxart into the photo index. Returns a summary."""
    add_photo = add_photo or _default_add_photo
    if not covers.loaded and covers.load() == 0:
        return {"error": "could not load the boxart index"}

    seeded = missing = failed = 0
    for title in titles:
        filename = covers.best_filename(title)
        if not filename:
            missing += 1
            log(f"  no art: {title}")
            continue
        if dry_run:
            seeded += 1
            log(f"  would seed {title}  <-  {filename}")
            continue
        png = covers.fetch_png(filename)
        if not png:
            failed += 1
            log(f"  fetch failed: {title} ({filename})")
            continue
        res = add_photo(base64.b64encode(png).decode("ascii"), title, variant)
        if res.get("ok"):
            seeded += 1
            state = "stored" if res.get("stored") else "dup"
            log(f"  {state}: {title}")
        else:
            failed += 1
            log(f"  add_photo refused {title}: {res.get('detail')}")
        if delay:
            time.sleep(delay)
    return {"seeded": seeded, "missing": missing, "failed": failed}


def _select(args) -> list:
    titles = list(args.title or [])
    if args.titles_file:
        with open(args.titles_file, encoding="utf-8") as fh:
            titles += [ln.strip() for ln in fh if ln.strip()]
    pool = [t for t in catalog.CATALOG if (t.curated if args.curated else True)]
    pool.sort(key=lambda t: t.canonical)
    if not titles:
        titles = [t.canonical for t in pool]
    if args.limit:
        titles = titles[:args.limit]
    return titles


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the photo index with cover art.")
    ap.add_argument("--title", action="append", help="a title to seed (repeatable)")
    ap.add_argument("--titles-file", help="file of titles, one per line")
    ap.add_argument("--curated", action="store_true",
                    help="restrict a catalog sweep to curated titles")
    ap.add_argument("--limit", type=int, help="cap how many titles to seed")
    ap.add_argument("--variant", default="unknown",
                    help="variant label to store (default: unknown)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between fetches (be polite)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be seeded without fetching/storing")
    ap.add_argument("--stats", action="store_true",
                    help="print photo-index stats and exit")
    ap.add_argument("--source", default="libretro",
                    choices=("libretro", "coverproject", "thegamesdb"),
                    help="where to fetch cover art from (default: libretro)")
    ap.add_argument("--face", default="front", choices=("front", "back"),
                    help="coverproject/thegamesdb only: which face to seed "
                         "(run twice to get both)")
    ap.add_argument("--probe", action="store_true",
                    help="check one page from the source and exit, to verify "
                         "the key and the response shape")
    ap.add_argument("--anytime", action="store_true",
                    help="coverproject only: run a batch outside their "
                         "requested 02:00-06:00 UTC window")
    args = ap.parse_args()

    if args.stats:
        try:
            import vault
            print(vault.stats())
        except Exception as exc:                        # noqa: BLE001
            print(f"vault unavailable: {exc}")
        return 0

    if args.probe:
        source = build_source(args.source, face=args.face)
        if not hasattr(source, "probe"):
            print(f"--source {args.source} has nothing to probe "
                  "(its index is a plain file listing).")
            return 2
        source.probe()
        return 0

    if not (args.title or args.titles_file or args.curated or args.limit):
        print("No scope given. Pass --title / --titles-file / --curated / "
              "--limit. (Kept explicit so a stray run doesn't hammer the CDN.)")
        return 0

    titles = _select(args)

    if args.source == "coverproject" and not args.dry_run:
        # Their terms ask that batch runs sit in an off-peak window. A single
        # title is a lookup, not a batch, so it is not held to the window.
        if len(titles) > 1 and not in_offpeak_window() and not args.anytime:
            now = datetime.datetime.now(datetime.timezone.utc)
            print(f"thecoverproject asks that batch runs happen between "
                  f"{_CP_WINDOW_UTC[0]:02d}:00 and {_CP_WINDOW_UTC[1]:02d}:00 "
                  f"UTC; it is now {now:%H:%M} UTC.\n"
                  f"  Re-run in the window, or pass --anytime to proceed.")
            return 1
        est = len(titles) * 2 * _CP_MIN_INTERVAL      # index + image per title
        print(f"  rate limit: 1 req / {_CP_MIN_INTERVAL:g}s, single-threaded "
              f"-> ~{est / 60:.0f} min for {len(titles)} title(s).")

    covers = build_source(args.source, face=args.face)
    label = {"libretro": REPO,
             "coverproject": "thecoverproject.net (PS2)",
             "thegamesdb": "TheGamesDB (PS2)"}[args.source]
    print(f"loading cover index for {label} ...")
    n = covers.load()
    if not n:
        print("could not load the cover index (network?). Aborting.")
        if args.source == "coverproject" and getattr(covers, "last_status", 0) == 403:
            print(_cp_explain_403())
        if args.source == "thegamesdb" and not covers._key:
            print("  No thegamesdb_token is set. On the desktop:\n"
                  "    python keystore.py set thegamesdb_token <key>")
        return 1
    if args.source == "thegamesdb" and covers.remaining is not None:
        print(f"  allowance remaining: {covers.remaining}")
    print(f"  {n} covers indexed; seeding {len(titles)} title(s) "
          f"{'(dry run)' if args.dry_run else ''} ...")
    # coverproject enforces its own floor inside _get; --delay only pads it.
    summary = run(covers, titles, variant=args.variant, delay=args.delay,
                  dry_run=args.dry_run)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
