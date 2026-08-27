"""
test_pricecache.py — the precomputed-estimate cache and its wiring.

Covers the store (roundtrip, staleness, sync-merge), the desktop harvester
populating it from a source, and the core.value fast path: a cached SKU is
served without touching the live source, reads as REAL even when this process
only has the mock live source, and still gets fresh economics.
"""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import core
import mock_sources as ms
import precompute
import pricecache
from listing_parser import Region as R

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds")


def test_store():
    with tempfile.TemporaryDirectory() as d:
        cache = pricecache.PriceCache(Path(d) / "pc.db")
        check("empty stats", cache.stats()["skus"] == 0)

        est = {"quotable": True, "sku": "Ico|ntsc_u|unknown|loose",
               "expected_resale": 20.0, "conservative_resale": 15.0,
               "p25": 12.0, "p75": 28.0, "confidence": "medium",
               "n_effective": 9.0, "days_to_sell": 30.0,
               "adjustments": [], "warnings": []}
        cache.put("Ico|ntsc_u|unknown|loose", "Ico", est, source_name="layered")
        got = cache.get("Ico|ntsc_u|unknown|loose")
        check("roundtrip preserves estimate",
              got and got["expected_resale"] == 20.0 and got["confidence"] == "medium")
        check("get stamps cached_at", got and "cached_at" in got)
        check("miss returns None", cache.get("nope|x|y|z") is None)
        check("stats counts it", cache.stats()["skus"] == 1
              and cache.stats()["quotable"] == 1)

        # Staleness: a row written 40 days ago is refused at max_age 30.
        cache.put("Old|ntsc_u|unknown|loose", "Old", est,
                  computed_at=_iso(40))
        check("fresh enough passes", cache.get("Old|ntsc_u|unknown|loose",
                                               max_age_days=90) is not None)
        check("too old is refused", cache.get("Old|ntsc_u|unknown|loose",
                                              max_age_days=30) is None)
        cache.close()


def test_sync_merge():
    with tempfile.TemporaryDirectory() as d:
        a = pricecache.PriceCache(Path(d) / "a.db")
        est = {"quotable": True, "expected_resale": 10.0}
        a.put("S|ntsc_u|unknown|loose", "S", est, computed_at=_iso(10))

        # Incoming NEWER row wins; incoming OLDER row is ignored.
        newer = {"sku": "S|ntsc_u|unknown|loose", "title": "S",
                 "computed_at": _iso(1),
                 "payload": {"quotable": True, "expected_resale": 12.0}}
        older = {"sku": "S|ntsc_u|unknown|loose", "title": "S",
                 "computed_at": _iso(30),
                 "payload": {"quotable": True, "expected_resale": 8.0}}
        n1 = a.import_rows([newer])
        check("newer row merges in", n1 == 1
              and a.get("S|ntsc_u|unknown|loose")["expected_resale"] == 12.0)
        n2 = a.import_rows([older])
        check("older row is skipped", n2 == 0
              and a.get("S|ntsc_u|unknown|loose")["expected_resale"] == 12.0)

        # export/import moves rows between stores intact.
        b = pricecache.PriceCache(Path(d) / "b.db")
        moved = b.import_rows(a.export_rows())
        check("export/import carries rows",
              moved == 1 and b.get("S|ntsc_u|unknown|loose") is not None)
        a.close(); b.close()


def test_harvester_and_core_fastpath():
    core._CACHE.clear()
    with tempfile.TemporaryDirectory() as d:
        cache = pricecache.PriceCache(Path(d) / "pc.db")
        src = ms.CombinedSource(ms.MockMarketplace(seed=7, today=precompute.TODAY),
                                ms.MockReference(precompute.TODAY))

        summary = precompute.run(cache, src, ["Ico", "Okami"], log=lambda *_: None)
        check("harvester priced SKUs", summary["priced"] > 0)
        check("cache now holds Ico loose",
              cache.get("Ico|ntsc_u|unknown|loose") is not None)

        # A source that raises if touched: proves the cache hit is served
        # WITHOUT any live comp call.
        class Boom:
            name = "boom"
            def sold_records(self, *a):
                raise AssertionError("live source must not be called on a hit")
            def quote(self, *a):
                raise AssertionError("live source must not be called on a hit")
            def active_listing_count(self, *a):
                raise AssertionError("live source must not be called on a hit")

        out = core.value(Boom(), False, title="Ico", variant="unknown",
                         completeness="loose", price_cache=cache)
        check("core serves cached estimate without the live source",
              out.get("quotable") is True)
        check("cache hit is flagged cached", out.get("cached") is True)
        check("cache hit reads as REAL despite mock live source",
              out.get("source_is_mock") is False)
        check("cache hit still computes economics live", "max_bid" in out)
        check("cache hit surfaces cached_at", "cached_at" in out)
        cache.close()

    # With no cache and a raising source, core would have to call it (control):
    core._CACHE.clear()
    class Boom2:
        name = "boom"
        def sold_records(self, *a):
            raise RuntimeError("called")
        def quote(self, *a):
            raise RuntimeError("called")
        def active_listing_count(self, *a):
            raise RuntimeError("called")
    raised = False
    try:
        core.value(Boom2(), False, title="Ico", variant="unknown",
                   completeness="loose")            # no price_cache
    except Exception:
        raised = True
    check("without a cache, the live source IS consulted", raised)


def test_core_miss_falls_through_to_live():
    core._CACHE.clear()
    with tempfile.TemporaryDirectory() as d:
        cache = pricecache.PriceCache(Path(d) / "pc.db")     # empty cache
        src = ms.CombinedSource(ms.MockMarketplace(seed=7, today=core.TODAY),
                                ms.MockReference(core.TODAY))
        out = core.value(src, False, title="Ico", variant="unknown",
                         completeness="loose", price_cache=cache)
        check("empty cache falls through to live compute",
              out.get("quotable") is True and out.get("cached") is False)
        check("live compute reflects the source's realness flag",
              out.get("source_is_mock") is True)
        cache.close()


def main() -> int:
    test_store()
    test_sync_merge()
    test_harvester_and_core_fastpath()
    test_core_miss_falls_through_to_live()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
