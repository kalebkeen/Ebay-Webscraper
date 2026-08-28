"""
upc.py — barcode to catalog entry.

Scanning a UPC removes almost everything `listing_parser` exists to handle:
no fuzzy title match, no sequel-number guard, no ambiguity rejection. The
code either resolves to one product or it doesn't.

WHY THIS SHIPS EMPTY
--------------------
There is no free, reliable, complete UPC database for PS2 games, and I am
not going to invent one. Fabricated barcodes would resolve confidently to
the wrong game, which is worse than not resolving at all -- a silent
mismatch here feeds a wrong price straight into a buy decision.

So the index starts empty and learns. The first time you scan an unknown
code the app asks which game it is; after that it knows. Bootstrapping from
your own scans is slower on day one and produces data you can actually
trust, which matters more.

The same file accepts a bulk import if you later buy or scrape a real
dataset -- see `bulk_load`.

UPC NOTES
---------
Retail PS2 games in North America carry a 12-digit UPC-A. The same title in
a Greatest Hits reprint usually carries a DIFFERENT code from the black
label original, which is exactly the distinction the pricing model cares
most about -- so `UpcEntry` records variant, not just title.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from listing_parser import Completeness, Region, Variant

STORE = Path(__file__).parent / "upc_index.json"


@dataclass
class UpcEntry:
    upc: str
    title: str                       # canonical catalog title
    variant: str = Variant.UNKNOWN.value
    region: str = Region.NTSC_U.value
    confirmed_by: str = "user"       # user | import
    first_seen: str = ""
    times_scanned: int = 0


def normalise(code: str) -> str:
    """
    Strip to digits and reconcile the UPC-A / EAN-13 split.

    Phone scanners frequently report a 12-digit UPC-A as a 13-digit EAN-13
    with a leading zero. Treating those as different codes would fragment
    the index and make it look like nothing was ever learned.
    """
    digits = re.sub(r"\D", "", code or "")
    if len(digits) == 13 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _variant_score(entry: dict) -> int:
    """How much a record's variant is worth in a merge: a user-confirmed known
    variant beats an imported one beats an unknown. This is what stops a sync
    from ever downgrading a spine you checked by hand back to 'unknown'."""
    variant = (entry.get("variant") or "").strip()
    if not variant or variant == Variant.UNKNOWN.value:
        return 0
    return 2 if entry.get("confirmed_by") == "user" else 1


def merge_two(a: dict, b: dict) -> dict:
    """Merge two records for the SAME barcode without losing information.

    The higher-confidence variant wins; times_scanned takes the max (not the
    sum — the same entry seen on both sides across syncs must not inflate);
    first_seen keeps the earliest; 'user' confirmation is sticky. Order-
    independent, so pushing and pulling in either direction converges.
    """
    win, other = (a, b) if _variant_score(a) >= _variant_score(b) else (b, a)
    seens = [x for x in (a.get("first_seen"), b.get("first_seen")) if x]
    confirmed = "user" if "user" in (a.get("confirmed_by"),
                                     b.get("confirmed_by")) else \
                (win.get("confirmed_by") or other.get("confirmed_by") or "user")
    return {
        "upc": a.get("upc") or b.get("upc"),
        "title": win.get("title") or other.get("title") or "",
        "variant": win.get("variant") or other.get("variant")
        or Variant.UNKNOWN.value,
        "region": win.get("region") or other.get("region")
        or Region.NTSC_U.value,
        "confirmed_by": confirmed,
        "first_seen": min(seens) if seens else "",
        "times_scanned": max(int(a.get("times_scanned") or 0),
                             int(b.get("times_scanned") or 0)),
    }


def check_digit_ok(code: str) -> bool:
    """UPC-A checksum. Catches a mis-read before it becomes a bad lookup."""
    d = normalise(code)
    if len(d) != 12 or not d.isdigit():
        return False
    odd = sum(int(d[i]) for i in range(0, 11, 2))
    even = sum(int(d[i]) for i in range(1, 11, 2))
    return (10 - (odd * 3 + even) % 10) % 10 == int(d[11])


class UpcIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or STORE
        self._by_upc: dict[str, UpcEntry] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for code, payload in raw.items():
            self._by_upc[code] = UpcEntry(**payload)

    def save(self) -> None:
        self.path.write_text(json.dumps(
            {k: asdict(v) for k, v in self._by_upc.items()},
            indent=2, sort_keys=True))

    # ------------------------------------------------------------------

    def lookup(self, code: str) -> UpcEntry | None:
        entry = self._by_upc.get(normalise(code))
        if entry:
            entry.times_scanned += 1
            self.save()
        return entry

    def teach(self, code: str, title: str, variant: str = Variant.UNKNOWN.value,
              region: str = Region.NTSC_U.value,
              source: str = "user") -> UpcEntry:
        """Record what a code actually is. Overwrites a previous mapping."""
        code = normalise(code)
        existing = self._by_upc.get(code)
        entry = UpcEntry(
            upc=code, title=title, variant=variant, region=region,
            confirmed_by=source,
            first_seen=(existing.first_seen if existing
                        else date.today().isoformat()),
            times_scanned=(existing.times_scanned if existing else 0),
        )
        self._by_upc[code] = entry
        self.save()
        return entry

    def all_entries(self) -> list[dict]:
        """Every record as a plain dict — for backing the index up to the vault."""
        return [asdict(e) for e in self._by_upc.values()]

    def merge_entries(self, rows: list[dict]) -> int:
        """Merge records in from the vault (or another device) without clobbering.

        Uses merge_two per barcode, so a pulled record never downgrades a local
        confirmed variant and never double-counts scans. Returns how many local
        records changed."""
        changed = 0
        for row in rows:
            code = normalise(str(row.get("upc", "")))
            if not code or not row.get("title"):
                continue
            incoming = {
                "upc": code,
                "title": row.get("title") or "",
                "variant": row.get("variant") or Variant.UNKNOWN.value,
                "region": row.get("region") or Region.NTSC_U.value,
                "confirmed_by": row.get("confirmed_by") or "user",
                "first_seen": row.get("first_seen") or "",
                "times_scanned": int(row.get("times_scanned") or 0),
            }
            existing = self._by_upc.get(code)
            m = merge_two(asdict(existing), incoming) if existing else incoming
            new_entry = UpcEntry(
                upc=code, title=m["title"],
                variant=m.get("variant", Variant.UNKNOWN.value),
                region=m.get("region", Region.NTSC_U.value),
                confirmed_by=m.get("confirmed_by", "user"),
                first_seen=m.get("first_seen", ""),
                times_scanned=int(m.get("times_scanned", 0)),
            )
            if existing is None or asdict(existing) != asdict(new_entry):
                self._by_upc[code] = new_entry
                changed += 1
        if changed:
            self.save()
        return changed

    def bulk_load(self, rows: list[dict]) -> int:
        """Import a real dataset. Rows need at least `upc` and `title`."""
        n = 0
        for row in rows:
            code = normalise(str(row.get("upc", "")))
            title = row.get("title")
            if not code or not title:
                continue
            self._by_upc[code] = UpcEntry(
                upc=code, title=title,
                variant=row.get("variant", Variant.UNKNOWN.value),
                region=row.get("region", Region.NTSC_U.value),
                confirmed_by="import",
                first_seen=date.today().isoformat())
            n += 1
        self.save()
        return n

    def stats(self) -> dict:
        return {
            "known_codes": len(self._by_upc),
            "learned_from_scans": sum(1 for e in self._by_upc.values()
                                      if e.confirmed_by == "user"),
            "total_scans": sum(e.times_scanned for e in self._by_upc.values()),
        }
