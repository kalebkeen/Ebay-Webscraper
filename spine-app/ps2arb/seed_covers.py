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

Source: the libretro-thumbnails project -- free, no API key, box art named by
title. Files are No-Intro style: "Title (Region) (extras).png". We match a
catalog title to the best regional boxart, preferring USA/World, and skip
disc-2 / demo scans.

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
    python seed_covers.py --stats
"""

from __future__ import annotations

import argparse
import base64
import difflib
import re
import time
import unicodedata
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
    args = ap.parse_args()

    if args.stats:
        try:
            import vault
            print(vault.stats())
        except Exception as exc:                        # noqa: BLE001
            print(f"vault unavailable: {exc}")
        return 0

    if not (args.title or args.titles_file or args.curated or args.limit):
        print("No scope given. Pass --title / --titles-file / --curated / "
              "--limit. (Kept explicit so a stray run doesn't hammer the CDN.)")
        return 0

    titles = _select(args)
    covers = LibretroCovers()
    print(f"loading boxart index for {REPO} ...")
    n = covers.load()
    if not n:
        print("could not load the boxart index (network?). Aborting.")
        return 1
    print(f"  {n} boxarts indexed; seeding {len(titles)} title(s) "
          f"{'(dry run)' if args.dry_run else ''} ...")
    summary = run(covers, titles, variant=args.variant, delay=args.delay,
                  dry_run=args.dry_run)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
