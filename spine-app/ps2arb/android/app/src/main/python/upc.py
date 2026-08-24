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
