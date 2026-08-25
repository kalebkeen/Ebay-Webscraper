"""
scandex.py — bulk barcode coverage, as a fallback behind the learned index.

ScanDex is a barcode database purpose-built for video games. It holds roughly
3,457 PlayStation 2 barcodes and returns an IGDB game match rather than the
retail-listing string a generic barcode API gives back. Free during its
launch period; sign up at https://scandex.gamery.app.

WHERE THIS SITS

    1. upc.UpcIndex        learned locally — you scanned it, variant confirmed
    2. scandex (here)      broad coverage, TITLE ONLY
    3. manual pick         the existing "teach this barcode" flow

Layer 1 always wins. That ordering is the whole design, because of the
limitation below.

WHAT IT CANNOT TELL YOU

ScanDex resolves a barcode to an IGDB *game*, not to a *pressing*. Black
label and Greatest Hits are separate products with separate barcodes and a
3-5x price gap, and IGDB models them as one entry. So a ScanDex hit gives a
title and nothing more; the variant stays unknown and `pricing_variant()`
falls back to its pessimistic default.

That is safe — you underprice and miss a deal rather than overpay — but it
is not free. The fix is to confirm the spine once and let `upc.teach()`
record it, after which layer 1 answers precisely. Coverage starts broad and
sharpens where you actually trade.

DEPENDENCY RISK

Free "during the launch period" is a term someone else controls. The eBay
harvest in store.py builds an asset you own outright and yields
variant-accurate barcodes, so it is worth running regardless of how good
coverage here turns out to be. This module is a shortcut, not a foundation.

    export SCANDEX_TOKEN=...
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import catalog
import upc

BASE_URL = "https://scandex.gamery.app/api/v2"
CACHE_PATH = Path(os.environ.get("SCANDEX_CACHE",
                                 Path(__file__).parent / "scandex_cache.json"))

# IGDB platform id for PlayStation 2. Checked against the name as well: an
# id that silently drifts would resolve PS3 and PS4 barcodes as PS2 games,
# which is the one failure mode worth being paranoid about here.
IGDB_PS2_ID = 8
PS2_NAMES = {"playstation 2", "ps2", "playstation2"}

# Negative results are cached too, and for a shorter time. Without this,
# every scan of an unknown disc costs a network round trip; with too long a
# TTL, barcodes added upstream stay invisible for weeks.
TTL_HIT_DAYS = 90
TTL_MISS_DAYS = 7


@dataclass
class ScanDexResult:
    barcode: str
    igdb_name: str | None = None
    igdb_id: int | None = None
    platform: str | None = None
    platform_id: int | None = None
    catalog_title: str | None = None     # matched into our own catalog
    match_score: float = 0.0
    status: str = "unknown"              # matched | unmatched | absent | error
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "matched" and self.catalog_title is not None


class ScanDexClient:
    """Thin lookup client. Never raises into the caller."""

    name = "scandex"

    def __init__(self, token: str | None = None,
                 cache_path: Path | None = None,
                 timeout: float = 8.0):
        self.token = token or os.environ.get("SCANDEX_TOKEN", "")
        self.timeout = timeout
        self.cache_path = Path(cache_path or CACHE_PATH)
        self._cache: dict = {}
        self.calls_made = 0
        self._load_cache()

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # ----------------------------------------------------------- cache

    def _load_cache(self) -> None:
        try:
            self._cache = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1))
        except OSError:
            pass

    def _cached(self, code: str) -> dict | None:
        row = self._cache.get(code)
        if not row:
            return None
        ttl = TTL_HIT_DAYS if row.get("status") == "matched" else TTL_MISS_DAYS
        if time.time() - row.get("fetched", 0) > ttl * 86400:
            return None
        return row

    # ---------------------------------------------------------- lookup

    def lookup(self, barcode: str) -> ScanDexResult:
        """Resolve a barcode. Degrades to a status, never an exception."""
        code = upc.normalise(barcode)
        if not code:
            return ScanDexResult(barcode, status="error",
                                 note="not a product barcode")

        cached = self._cached(code)
        if cached is not None:
            return self._build(code, cached)

        if not self.configured:
            return ScanDexResult(code, status="error",
                                 note="SCANDEX_TOKEN is not set")

        url = f"{BASE_URL}/lookup?{urllib.parse.urlencode({'value': code})}"
        req = urllib.request.Request(url, headers={
            "Authorization": self.token, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.calls_made += 1
                payload = json.loads(resp.read().decode())
            payload["status"] = ("matched" if payload.get("igdb_metadata")
                                 else "unmatched")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                payload = {"status": "absent"}
            elif exc.code in (401, 403):
                return ScanDexResult(code, status="error",
                                     note="ScanDex rejected the token")
            else:
                return ScanDexResult(code, status="error",
                                     note=f"ScanDex returned {exc.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # Offline is the normal case in a shop basement. Not an error
            # worth surfacing -- the local index and manual entry still work.
            return ScanDexResult(code, status="error", note="ScanDex unreachable")

        payload["fetched"] = time.time()
        self._cache[code] = payload
        self._save_cache()
        return self._build(code, payload)

    def _build(self, code: str, payload: dict) -> ScanDexResult:
        status = payload.get("status", "unknown")
        if status != "matched":
            return ScanDexResult(code, status=status,
                                 note={"absent": "not in ScanDex",
                                       "unmatched": "barcode known, no game "
                                                    "metadata yet"}.get(status, ""))

        meta = payload.get("igdb_metadata") or {}
        platform = (meta.get("platform") or {})
        pid, pname = platform.get("id"), (platform.get("name") or "")

        result = ScanDexResult(
            barcode=code, igdb_name=meta.get("name"), igdb_id=meta.get("id"),
            platform=pname, platform_id=pid, status="matched")

        # Platform guard. Multi-platform releases share a title, and a PS3 or
        # PS4 barcode resolving to a PS2 valuation would be confidently wrong
        # in a way nothing downstream could detect.
        if pid != IGDB_PS2_ID and pname.strip().lower() not in PS2_NAMES:
            result.status = "wrong_platform"
            result.note = f"barcode is for {pname or 'another platform'}, not PS2"
            return result

        # IGDB names differ from our Wikipedia-derived canonical titles
        # (punctuation, subtitles, regional naming), so go through the same
        # matcher the listing parser uses rather than comparing strings.
        if result.igdb_name:
            m = catalog.match(result.igdb_name)
            if m.title is not None and m.confident:
                result.catalog_title = m.title.canonical
                result.match_score = m.score
            else:
                result.note = (f"'{result.igdb_name}' did not match the "
                               f"catalog confidently (best {m.score:.0f})")
        return result

    def stats(self) -> dict:
        matched = sum(1 for v in self._cache.values()
                      if v.get("status") == "matched")
        return {"cached": len(self._cache), "matched": matched,
                "calls_this_session": self.calls_made,
                "configured": self.configured}


# ---------------------------------------------------------------------------
# The layered resolver
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    barcode: str
    title: str | None = None
    variant: str = "unknown"
    source: str = "none"          # local | scandex | none
    confident: bool = False
    warnings: list = field(default_factory=list)


def resolve(barcode: str, index: upc.UpcIndex,
            client: ScanDexClient | None = None) -> Resolution:
    """
    Local index first, ScanDex second, nothing third.

    The ordering is not arbitrary. A locally learned entry carries a
    confirmed VARIANT; a ScanDex hit carries only a title. Preferring the
    remote source because it is "more authoritative" would throw away the
    more valuable answer.
    """
    code = upc.normalise(barcode)
    out = Resolution(barcode=code or barcode)
    if not code:
        out.warnings.append("That is not a product barcode.")
        return out

    entry = index.lookup(code)
    if entry is not None:
        out.title = entry.title
        out.variant = getattr(entry, "variant", "unknown") or "unknown"
        out.source = "local"
        out.confident = getattr(entry, "times_scanned", 1) >= 2
        if not out.confident:
            out.warnings.append("Seen once before — confirm the title is right.")
        return out

    if client is None or not client.configured:
        return out

    hit = client.lookup(code)
    if hit.usable:
        out.title = hit.catalog_title
        out.source = "scandex"
        out.confident = False
        # Stated plainly because it is the difference between a $20 quote and
        # a $90 one, and the app will price pessimistically until told.
        out.warnings.append(
            "Variant unknown — ScanDex identifies the game, not the pressing. "
            "Check the spine: black label is worth far more than Greatest Hits."
        )
    elif hit.status == "wrong_platform":
        out.warnings.append(hit.note)
    elif hit.note:
        out.warnings.append(hit.note)
    return out


def promote(resolution: Resolution, variant: str, index: upc.UpcIndex) -> bool:
    """
    Write a confirmed variant into the local index.

    This is the step that turns broad coverage into precise coverage: once
    the spine has been checked once, layer 1 answers every future scan of
    that barcode without a network call.
    """
    if not resolution.title:
        return False
    index.teach(resolution.barcode, resolution.title, variant=variant)
    return True
