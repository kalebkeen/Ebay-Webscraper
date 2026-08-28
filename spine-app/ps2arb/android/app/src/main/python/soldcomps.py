"""
soldcomps.py — real individual sold listings from the SoldComps API.

This is the marketplace-shaped comp source: it answers `sold_records()` with
individual completed eBay sales (price, shipping, date), which is what the
robust estimator in comps.py actually wants -- a distribution to take a
conservative quantile of and dates to measure velocity from. It fills the gap
left by eBay's own Marketplace Insights API being closed to new developers.

API (https://sold-comps.com, confirmed against openapi.json):

    GET https://api.sold-comps.com/v1/scrape
        ?keyword=<text>&ebaySite=ebay.com&sold=true&count=40
        Authorization: Bearer sc_...
    -> {"items":[{title, soldPrice, shippingPrice, endedAt, buyingFormat,
                  bestOfferAccepted, boaAcceptedPrice, conditionId, ...}],
        "hasNextPage": bool, ...}

    sold=false returns ACTIVE listings (currentPrice) -- the supply count for
    sell-through / days-to-sell.

Two honesty details this adapter gets right, both straight from the API:

  * BEST OFFER. When a sale was a best offer, `soldPrice` is the LIST price,
    not what it cleared for -- the classic "best offer recorded at list"
    contamination. SoldComps exposes `boaAcceptedPrice`, the ACTUAL accepted
    amount, so we use that when present instead of guessing a haircut.

  * IDENTITY. A keyword search returns near-neighbours (a "silent hill 2"
    query returns SH3, SH4, guides, posters). Each result's title is run back
    through the same `pipeline.resolve` the rest of the app uses, and anything
    that does not resolve to the exact title we asked about is dropped. That
    also fills in variant / completeness / region from the listing text.

Stdlib only (urllib via httpjson). Token held on the desktop keystore as
`soldcomps_token`, synced to the phone; never entered by Claude. Free tier is
100 requests/month, so callers should lean on core.py's cache.
"""

from __future__ import annotations

import urllib.parse
from datetime import date, datetime

import httpjson
from comps import SoldRecord
from listing_parser import Completeness, Region, Variant

BASE = "https://api.sold-comps.com"

# Region -> the eBay marketplace to search. SoldComps covers 8 sites; there is
# no Japanese site, so NTSC-J falls back to ebay.com and relies on the title
# parse + the region filter in comps to keep only matching sales.
_REGION_SITE = {
    Region.NTSC_U: "ebay.com",
    Region.UNKNOWN: "ebay.com",
    Region.PAL: "ebay.co.uk",
    Region.NTSC_J: "ebay.com",
}


def _default_resolve(text: str):
    """Classify a listing title with the app's own resolver (lazy import).

    Imported lazily so tests can inject a fake resolver and so importing this
    module never drags in the whole identify pipeline.
    """
    import pipeline
    return pipeline.resolve(text)


class SoldCompsSource:
    """SoldComps /v1/scrape as a marketplace CompSource."""

    name = "soldcomps"

    def __init__(self, token: str, *, resolve=None, opener=None,
                 count: int = 40, exclude_lots: bool = True,
                 timeout: float = 30.0):
        self.token = (token or "").strip()
        self.resolve = resolve or _default_resolve
        self._opener = opener            # test seam; None -> real urlopen
        self.count = count
        self.exclude_lots = exclude_lots
        self.timeout = timeout

    # -- CompSource surface ------------------------------------------------

    def sold_records(self, title: str, region: Region,
                     since: date) -> list[SoldRecord]:
        if not self.token:
            return []
        ok, items = self._scrape(title, region, sold=True)
        if not ok:
            return []

        out: list[SoldRecord] = []
        for it in items:
            rec = self._to_record(it, title, region, since)
            if rec is not None:
                out.append(rec)
        return sorted(out, key=lambda r: r.sold_on)

    def active_listing_count(self, title: str, region: Region) -> int | None:
        if not self.token:
            return None
        ok, items = self._scrape(title, region, sold=False)
        if not ok:
            return None
        n = 0
        for it in items:
            if self._matches_title(it.get("title", ""), title):
                n += 1
        # Zero active is more likely a thin/incomplete active scrape than a
        # genuinely empty shelf; report None (unknown) so velocity is not
        # computed off it rather than an over-optimistic "sells instantly".
        return n or None

    def quote(self, title, region):
        return {}                              # marketplace: no tier prices

    # -- internals ---------------------------------------------------------

    def _scrape(self, title: str, region: Region, *, sold: bool):
        """(ok, items). ok is False on any non-200 so the caller can tell an
        empty result from a failed request and not price off a network blip."""
        keyword = f"{title} playstation 2"
        if self.exclude_lots:
            keyword += " -lot -bundle"
        params = {
            "keyword": keyword,
            "ebaySite": _REGION_SITE.get(region, "ebay.com"),
            "sold": "true" if sold else "false",
            "count": str(self.count),
        }
        url = f"{BASE}/v1/scrape?" + urllib.parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self.token}"}
        kwargs = {"headers": headers, "timeout": self.timeout}
        if self._opener is not None:
            kwargs["opener"] = self._opener
        status, data = httpjson.get_json(url, **kwargs)
        if status != 200 or not isinstance(data, dict):
            return False, []
        items = data.get("items")
        return True, (items if isinstance(items, list) else [])

    def _to_record(self, item: dict, title: str, region: Region,
                   since: date) -> SoldRecord | None:
        sold_on = _parse_date(item.get("endedAt"))
        if sold_on is None or sold_on < since:
            return None

        # Best-offer sales list at one price and clear at another; prefer the
        # actual accepted amount when the API provides it.
        best_offer = bool(item.get("bestOfferAccepted"))
        price = None
        note = ""
        if best_offer:
            price = _money(item.get("boaAcceptedPrice"))
            if price is not None:
                note = "best_offer_accepted"
        if price is None:
            price = _money(item.get("soldPrice"))
            if best_offer:
                note = "best_offer_at_list"   # cleared below this; flagged
        if price is None or price <= 0:
            return None
        shipping = _money(item.get("shippingPrice")) or 0.0

        target = self._classify(item.get("title", ""))
        if target is None or not self._same_title(target, title):
            return None

        fmt = str(item.get("buyingFormat", "")).strip()
        if fmt:
            note = f"{note}/{fmt}" if note else fmt

        rec_region = getattr(target, "region", None) or Region.UNKNOWN
        return SoldRecord(
            price=round(price, 2),
            shipping=round(shipping, 2),
            sold_on=sold_on,
            completeness=getattr(target, "completeness", Completeness.LOOSE),
            variant=getattr(target, "variant", Variant.UNKNOWN),
            region=rec_region,
            note=note,
        )

    def _classify(self, text: str):
        try:
            return self.resolve(text or "")
        except Exception:
            return None

    def _matches_title(self, text: str, title: str) -> bool:
        target = self._classify(text)
        return target is not None and self._same_title(target, title)

    @staticmethod
    def _same_title(target, title: str) -> bool:
        got = getattr(target, "title", None)
        return bool(got) and got.strip().lower() == title.strip().lower()


def _money(value) -> float | None:
    """SoldComps prices are decimal strings like "12.99". Strip stray currency
    symbols/commas defensively; unparseable -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    for sym in ("$", "£", "€", "USD", "GBP", "EUR"):
        s = s.replace(sym, "")
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(stamp) -> date | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
