"""
pricecharting.py — real tier prices from PriceCharting's Prices API.

This is the reference-shaped comp source: it answers `quote()` with loose /
CIB / new tier prices, exactly the shape `MockReference` faked. PriceCharting
derives those numbers from eBay-and-other-marketplace sold listings, so it is
a real aggregate sold price, not an asking price.

API (https://www.pricecharting.com/api-documentation), confirmed live:

    GET /api/product?t=<token>&q=<title> playstation 2
        -> {"status":"success","id","product-name","console-name",
            "loose-price","cib-price","new-price", ...}   prices in PENNIES
    GET /api/products?t=<token>&q=<title>
        -> {"status":"success","products":[{id,product-name,console-name}...]}

`q=` returns the single best match, which may land on the PSP or PS1 edition
of a title. So we bias the query with the console name and then VERIFY the
returned console-name before trusting the prices; if the best match is the
wrong console we fall back to the multi-result endpoint and pick the PS2 row.
A wrong-console price silently fed into a buy decision is precisely the bug
this project refuses to ship, so a console mismatch returns no quote rather
than a plausible-looking wrong one.

Stdlib only (urllib via httpjson). One token, held on the desktop keystore as
`pricecharting_token` and synced to the phone; never entered by Claude.

Requires a paid PriceCharting subscription. With no token the source is not
constructed at all (see sources.build_source), so this never degrades a scan.
"""

from __future__ import annotations

import urllib.parse
from datetime import date

import httpjson
from comps import CompQuote
from listing_parser import Completeness, Region

BASE = "https://www.pricecharting.com"

# PriceCharting's console label for the platform this app prices.
PS2_CONSOLE = "playstation 2"

# Region -> extra query term. PriceCharting files PAL and Japanese copies as
# separate products; NTSC-U is the unmarked default. We only bias the search;
# the console-name check still gates the result.
_REGION_TERM = {
    Region.NTSC_U: "",
    Region.PAL: "pal",
    Region.NTSC_J: "jp",
    Region.UNKNOWN: "",
}

# JSON price field -> the completeness tier it represents.
#   loose-price : disc only
#   cib-price   : complete in box (disc + case + manual)
#   new-price   : factory sealed -- its own market, but a REAL sealed comp,
#                 so we surface it rather than letting comps refuse SEALED.
_TIER_FIELD = {
    Completeness.LOOSE: "loose-price",
    Completeness.CIB: "cib-price",
    Completeness.SEALED: "new-price",
}


class PriceChartingSource:
    """PriceCharting Prices API as a reference CompSource."""

    name = "pricecharting"

    def __init__(self, token: str, *, console: str = PS2_CONSOLE,
                 opener=None, timeout: float = 30.0):
        self.token = (token or "").strip()
        self.console = console.strip().lower()
        self._opener = opener            # test seam; None -> real urlopen
        self.timeout = timeout

    # -- CompSource surface ------------------------------------------------

    def quote(self, title: str, region: Region) -> dict[Completeness, CompQuote]:
        if not self.token:
            return {}
        prod = self._lookup(title, region)
        if not prod:
            return {}

        # Guard: only trust prices whose console is actually PS2.
        console_name = str(prod.get("console-name", "")).strip().lower()
        if self.console not in console_name:
            return {}

        as_of = date.today()
        out: dict[Completeness, CompQuote] = {}
        for tier, field in _TIER_FIELD.items():
            price = _dollars(prod.get(field))
            if price is None:
                continue
            out[tier] = CompQuote(
                tier=tier, price=price, n=0, as_of=as_of,
                variant_split=False,          # aggregate over variants
                source=self.name,
                includes_shipping=False,       # item price, no postage
            )
        return out

    def sold_records(self, title, region, since):
        return []                              # reference source: no raw sales

    def active_listing_count(self, title, region):
        return None                            # supply is not a PriceCharting field

    # -- internals ---------------------------------------------------------

    def _lookup(self, title: str, region: Region) -> dict | None:
        """The best PS2 product dict for this title, or None."""
        term = _REGION_TERM.get(region, "")
        query = f"{title} {self.console}" + (f" {term}" if term else "")

        prod = self._get("/api/product", {"q": query})
        if prod and prod.get("status") == "success":
            console_name = str(prod.get("console-name", "")).strip().lower()
            if self.console in console_name:
                return prod

        # Best match was the wrong console (or nothing). Enumerate and pick
        # the PS2 row explicitly, then fetch its prices by id.
        listing = self._get("/api/products", {"q": f"{title} {term}".strip()})
        if not listing or listing.get("status") != "success":
            return None
        for row in listing.get("products", []) or []:
            console_name = str(row.get("console-name", "")).strip().lower()
            if self.console in console_name and row.get("id"):
                by_id = self._get("/api/product", {"id": str(row["id"])})
                if by_id and by_id.get("status") == "success":
                    return by_id
        return None

    def _get(self, path: str, params: dict) -> dict | None:
        qs = urllib.parse.urlencode({"t": self.token, **params})
        url = f"{BASE}{path}?{qs}"
        kwargs = {"timeout": self.timeout}
        if self._opener is not None:
            kwargs["opener"] = self._opener
        status, data = httpjson.get_json(url, **kwargs)
        if status == 200 and isinstance(data, dict):
            return data
        return None


def _dollars(pennies) -> float | None:
    """PriceCharting prices are integer pennies. Absent/zero -> no price.

    A zero or missing tier means PriceCharting has no data for it, not that
    the game is free; treated as absent so the tier is simply omitted.
    """
    try:
        cents = int(pennies)
    except (TypeError, ValueError):
        return None
    if cents <= 0:
        return None
    return round(cents / 100.0, 2)
