"""
test_sources.py — the real comp adapters, driven without a network.

Both adapters take an injectable `opener` (urlopen's signature) and SoldComps
takes an injectable `resolve`, so the parsing and the honesty rules can be
exercised against canned JSON: pennies -> dollars, the PriceCharting console
guard, best-offer accepted-price preference, near-neighbour title rejection,
the since-window, and the LayeredSource merge semantics. No token, no HTTP.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pricecharting
import soldcomps
import sources
from comps import CompQuote, SoldRecord
from listing_parser import Completeness as C, Region as R, Variant as V

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


# --------------------------------------------------------------------------
# A urlopen stand-in: routes on the request URL to canned (status, json).
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, obj):
        self.status = status
        self._body = json.dumps(obj).encode("utf-8")
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class Router:
    def __init__(self, fn):
        self.fn = fn
        self.urls = []
    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        status, obj = self.fn(url)
        return _Resp(status, obj)


def fake_resolve(text: str):
    """Stand in for pipeline.resolve: crude keyword classifier."""
    t = (text or "").lower()
    if "ico" in t:
        title = "Ico"
    elif "okami" in t:
        title = "Okami"
    else:
        return SimpleNamespace(title=None, variant=V.UNKNOWN,
                               completeness=C.LOOSE, region=R.UNKNOWN)
    comp = C.CIB if ("complete" in t or "cib" in t) else C.LOOSE
    return SimpleNamespace(title=title, variant=V.UNKNOWN,
                           completeness=comp, region=R.NTSC_U)


# --------------------------------------------------------------------------
# PriceCharting
# --------------------------------------------------------------------------

def test_pricecharting():
    def route(url):
        if "/api/product?" in url and "q=" in url:
            return 200, {"status": "success", "console-name": "Playstation 2",
                         "id": "123", "product-name": "Ico",
                         "loose-price": 1800, "cib-price": 3500,
                         "new-price": 9000}
        return 200, {"status": "error", "error-message": "no"}

    src = pricecharting.PriceChartingSource("tok", opener=Router(route))
    q = src.quote("Ico", R.NTSC_U)
    check("pc returns three tiers", set(q) == {C.LOOSE, C.CIB, C.SEALED})
    check("pc pennies -> dollars", q[C.LOOSE].price == 18.00
          and q[C.CIB].price == 35.00 and q[C.SEALED].price == 90.00)
    check("pc quote is aggregate item price",
          q[C.LOOSE].variant_split is False
          and q[C.LOOSE].includes_shipping is False)
    check("pc marketplace surface is empty",
          src.sold_records("Ico", R.NTSC_U, date(2026, 1, 1)) == []
          and src.active_listing_count("Ico", R.NTSC_U) is None)

    # Console guard: the single best match is the wrong console -> fall back to
    # /api/products, pick the PS2 row, fetch its prices by id.
    def route2(url):
        if "/api/products?" in url:
            return 200, {"status": "success", "products": [
                {"console-name": "PSP", "id": "1", "product-name": "Ico"},
                {"console-name": "Playstation 2", "id": "77",
                 "product-name": "Ico"}]}
        if "id=77" in url:
            return 200, {"status": "success", "console-name": "Playstation 2",
                         "id": "77", "product-name": "Ico",
                         "loose-price": 2000, "cib-price": 4000}
        # best match is wrong console
        return 200, {"status": "success", "console-name": "PSP", "id": "1",
                     "product-name": "Ico", "loose-price": 500}

    src2 = pricecharting.PriceChartingSource("tok", opener=Router(route2))
    q2 = src2.quote("Ico", R.NTSC_U)
    check("pc recovers PS2 row via /products fallback",
          q2.get(C.LOOSE) and q2[C.LOOSE].price == 20.00)
    check("pc drops absent/zero tiers", C.SEALED not in q2)

    # A wrong-console-only result yields NO quote, never a wrong price.
    def route3(url):
        if "/api/products?" in url:
            return 200, {"status": "success", "products": [
                {"console-name": "PSP", "id": "1", "product-name": "Ico"}]}
        return 200, {"status": "success", "console-name": "PSP", "id": "1",
                     "product-name": "Ico", "loose-price": 500}
    src3 = pricecharting.PriceChartingSource("tok", opener=Router(route3))
    check("pc refuses wrong-console price", src3.quote("Ico", R.NTSC_U) == {})

    check("pc with no token makes no request",
          pricecharting.PriceChartingSource("").quote("Ico", R.NTSC_U) == {})


# --------------------------------------------------------------------------
# SoldComps
# --------------------------------------------------------------------------

def test_soldcomps():
    since = date(2026, 1, 1)
    sold_items = [
        {"title": "Ico PS2 complete in box", "endedAt": "2026-08-01",
         "soldPrice": "30.00", "shippingPrice": "4.00",
         "buyingFormat": "buyItNow"},
        {"title": "Ico PlayStation 2", "endedAt": "2026-08-10",
         "soldPrice": "35.00", "shippingPrice": "0.00",
         "buyingFormat": "acceptsOffers", "bestOfferAccepted": True,
         "boaAcceptedPrice": "20.00"},
        {"title": "Okami PS2", "endedAt": "2026-08-05",
         "soldPrice": "25.00", "shippingPrice": "3.00"},        # wrong title
        {"title": "Ico PS2", "endedAt": "2020-01-01",
         "soldPrice": "12.00", "shippingPrice": "3.00"},        # too old
    ]

    def route(url):
        if "sold=true" in url:
            return 200, {"items": sold_items, "hasNextPage": False}
        if "sold=false" in url:
            return 200, {"items": [
                {"title": "Ico PS2", "currentPrice": "29.99"},
                {"title": "Ico PlayStation 2 CIB", "currentPrice": "45.00"},
                {"title": "Okami PS2", "currentPrice": "40.00"}]}   # not Ico
        return 200, {"items": []}

    src = soldcomps.SoldCompsSource("sc_tok", opener=Router(route),
                                    resolve=fake_resolve)
    recs = src.sold_records("Ico", R.NTSC_U, since)
    check("sc keeps only the two matching in-window Ico sales", len(recs) == 2)
    check("sc parses price + shipping",
          any(abs(r.price - 30.0) < 1e-9 and abs(r.shipping - 4.0) < 1e-9
              for r in recs))
    check("sc prefers accepted best-offer price over list",
          any(abs(r.price - 20.0) < 1e-9 for r in recs)
          and not any(abs(r.price - 35.0) < 1e-9 for r in recs))
    check("sc flags the accepted best offer",
          any("best_offer_accepted" in r.note for r in recs))
    check("sc classifies completeness from the title",
          any(r.completeness is C.CIB for r in recs))
    check("sc drops the wrong-title sale",
          all("okami" not in (r.note or "").lower() for r in recs)
          and len(recs) == 2)
    check("sc drops the out-of-window sale",
          all(r.sold_on >= since for r in recs))
    check("sc records are SoldRecords", all(isinstance(r, SoldRecord)
                                            for r in recs))

    n = src.active_listing_count("Ico", R.NTSC_U)
    check("sc counts only matching active listings", n == 2)

    check("sc quote surface is empty", src.quote("Ico", R.NTSC_U) == {})
    check("sc with no token makes no request",
          soldcomps.SoldCompsSource("").sold_records("Ico", R.NTSC_U, since)
          == [])

    # A failed request (non-200) must be distinguishable from "no sales":
    # sold_records is empty AND active count is None, never a wrong number.
    def route_fail(url):
        return 503, {"error": "busy"}
    bad = soldcomps.SoldCompsSource("sc_tok", opener=Router(route_fail),
                                    resolve=fake_resolve)
    check("sc empty on server error", bad.sold_records("Ico", R.NTSC_U, since)
          == [] and bad.active_listing_count("Ico", R.NTSC_U) is None)


# --------------------------------------------------------------------------
# LayeredSource + build_source
# --------------------------------------------------------------------------

class _Ref:
    name = "ref"
    def quote(self, t, r):
        return {C.LOOSE: CompQuote(C.LOOSE, 18.0, source="ref")}
    def sold_records(self, t, r, s):
        return []
    def active_listing_count(self, t, r):
        return None


class _Mkt:
    name = "mkt"
    def __init__(self, active):
        self._active = active
    def quote(self, t, r):
        return {}
    def sold_records(self, t, r, s):
        return [SoldRecord(20.0, 4.0, date(2026, 8, 2), C.LOOSE),
                SoldRecord(22.0, 4.0, date(2026, 8, 1), C.LOOSE)]
    def active_listing_count(self, t, r):
        return self._active


def test_layered():
    layered = sources.LayeredSource([_Mkt(active=7), _Ref()])
    q = layered.quote("Ico", R.NTSC_U)
    check("layered quote takes first non-empty (reference)",
          q.get(C.LOOSE) and q[C.LOOSE].price == 18.0)
    recs = layered.sold_records("Ico", R.NTSC_U, date(2026, 1, 1))
    check("layered merges sold records across sources", len(recs) == 2)
    check("layered sold records are date-sorted",
          recs[0].sold_on <= recs[1].sold_on)
    check("layered active takes first non-None",
          layered.active_listing_count("Ico", R.NTSC_U) == 7)

    # active falls through a None-reporting source to the next.
    layered2 = sources.LayeredSource([_Mkt(active=None), _Ref()])
    check("layered active falls through None",
          layered2.active_listing_count("Ico", R.NTSC_U) is None)

    # A source that raises must not sink the whole query.
    class _Boom:
        name = "boom"
        def quote(self, t, r): raise RuntimeError("x")
        def sold_records(self, t, r, s): raise RuntimeError("x")
        def active_listing_count(self, t, r): raise RuntimeError("x")
    layered3 = sources.LayeredSource([_Boom(), _Ref(), _Mkt(active=3)])
    check("layered survives a throwing source",
          layered3.quote("Ico", R.NTSC_U).get(C.LOOSE) is not None
          and layered3.active_listing_count("Ico", R.NTSC_U) == 3)


def test_build_source_fallback():
    empty = SimpleNamespace(get=lambda field: "")
    src, is_real = sources.build_source(today=date(2026, 8, 22), settings=empty)
    check("build_source falls back to mock when unconfigured", is_real is False)
    # The mock still answers the protocol.
    check("mock source is usable",
          hasattr(src, "quote") and hasattr(src, "sold_records"))

    configured = SimpleNamespace(
        get=lambda field: "sc_demo" if field == "soldcomps_token" else "")
    src2, is_real2 = sources.build_source(today=date(2026, 8, 22),
                                          settings=configured)
    check("build_source goes real with a soldcomps token", is_real2 is True)
    check("real build is a LayeredSource",
          isinstance(src2, sources.LayeredSource))


def main() -> int:
    test_pricecharting()
    test_soldcomps()
    test_layered()
    test_build_source_fallback()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
