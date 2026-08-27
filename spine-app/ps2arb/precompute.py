"""
precompute.py — desktop harvester that fills the price cache.

Runs on the DESKTOP (where the real tokens live), walks a chosen set of
catalog titles, prices the common SKUs against the real layered source, and
writes the finished estimates into pricecache.py. The keystore then serves
that cache and the phone reads it -- instant, offline, and without spending a
comp-API call per scan.

QUOTA IS THE WHOLE POINT, SO IT IS RESPECTED HERE.
  * SoldComps' free tier is 100 requests/MONTH. Pricing loose + CIB +
    disc-case x a few variants would re-fetch the same title several times,
    so a per-run MemoSource caches each (title, region) fetch and every SKU
    of that title reuses it: ~1 SoldComps call per title, not six.
  * There is NO "price the whole catalog" default. You must pass an explicit
    scope (--title / --curated / --limit / --titles-file). Running bare just
    prints cache stats, so a stray invocation can't torch the month's quota.
  * --max-age skips SKUs priced recently, so a scheduled run refreshes only
    what has gone stale.

    python precompute.py --curated --limit 50      # price 50 curated titles
    python precompute.py --title "Ico" --title "Okami"
    python precompute.py --stats
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import catalog
import comps
import pricecache
from listing_parser import Completeness as C, Region as R, Variant as V

TODAY = date(2026, 8, 22)
DB_PATH = os.environ.get("PS2ARB_PRICECACHE", "pricecache.db")

# The completeness tiers worth precomputing. SEALED/NO_DISC are deliberately
# excluded: comps refuses to extrapolate them, so caching would just store a
# refusal.
_TIERS = (C.LOOSE, C.CIB, C.DISC_CASE)


class MemoSource:
    """Wraps a CompSource and memoises each (title, region) fetch for the
    duration of a run, so pricing several SKUs of one title costs one fetch.

    Scoped to a single harvest run and thrown away, so it never serves stale
    data to the live app -- that is the price cache's job, with timestamps."""

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "layered")
        self._sold: dict = {}
        self._quote: dict = {}
        self._active: dict = {}

    def sold_records(self, title, region, since):
        k = (title, region.value)
        if k not in self._sold:
            self._sold[k] = self.inner.sold_records(title, region, since)
        return self._sold[k]

    def quote(self, title, region):
        k = (title, region.value)
        if k not in self._quote:
            self._quote[k] = self.inner.quote(title, region)
        return self._quote[k]

    def active_listing_count(self, title, region):
        k = (title, region.value)
        if k not in self._active:
            self._active[k] = self.inner.active_listing_count(title, region)
        return self._active[k]


def skus_for(entry):
    """The (variant, completeness) pairs worth caching for a title.

    Every title gets UNKNOWN (what an unlabelled scan is priced as); titles
    with a budget reprint additionally get the two named variants, since the
    spread between them is large and worth precomputing both sides of.
    """
    variants = [V.UNKNOWN]
    if entry.has_greatest_hits:
        variants += [V.BLACK_LABEL, V.GREATEST_HITS]
    for tier in _TIERS:
        for var in variants:
            yield var, tier


def run(cache, source, titles, *, today=TODAY, regions=(R.NTSC_U,),
        max_age_days: float | None = None, log=print) -> dict:
    """Price every SKU of every title and store it. Returns a small summary."""
    priced = skipped = refused = 0
    for title in titles:
        entry = _entry(title)
        if entry is None:
            log(f"  skip (not in catalog): {title}")
            continue
        for region in regions:
            memo = MemoSource(source)
            for variant, tier in skus_for(entry):
                sku = f"{title}|{region.value}|{variant.value}|{tier.value}"
                if max_age_days is not None and cache.get(sku, max_age_days) is not None:
                    skipped += 1
                    continue
                val = comps.value_sku(
                    title=title, region=region, variant=variant,
                    completeness=tier, source=memo,
                    has_budget_reprint=entry.has_greatest_hits, today=today)
                cache.put_valuation(val, source_name=memo.name)
                if val.quotable:
                    priced += 1
                else:
                    refused += 1
        log(f"  priced {title}")
    return {"priced": priced, "refused": refused, "skipped_fresh": skipped}


def _entry(title: str):
    return next((t for t in catalog.CATALOG if t.canonical == title), None)


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
    ap = argparse.ArgumentParser(description="Precompute resale estimates into "
                                             "the price cache.")
    ap.add_argument("--db", default=None, help=f"cache path (default {DB_PATH})")
    ap.add_argument("--title", action="append", help="a title to price (repeatable)")
    ap.add_argument("--titles-file", help="file of titles, one per line")
    ap.add_argument("--curated", action="store_true",
                    help="restrict the catalog sweep to curated titles")
    ap.add_argument("--limit", type=int, help="cap how many titles to price")
    ap.add_argument("--max-age", type=float, default=None,
                    help="skip SKUs already priced within this many days")
    ap.add_argument("--stats", action="store_true", help="print cache stats and exit")
    args = ap.parse_args()

    cache = pricecache.PriceCache(args.db or DB_PATH)

    if args.stats:
        print(cache.stats())
        return 0

    # No explicit scope = do nothing but report. This is the quota guard: a
    # bare run must never sweep the whole catalog against a metered API.
    if not (args.title or args.titles_file or args.curated or args.limit):
        print("No scope given. Pass --title / --titles-file / --curated / "
              "--limit to price something (kept explicit so a stray run can't "
              "burn your comp-API quota).")
        print(cache.stats())
        return 0

    try:
        import sources
        source, is_real = sources.build_source(today=TODAY)
    except Exception as exc:                            # noqa: BLE001
        print(f"could not build source: {exc}")
        return 1
    if not is_real:
        print("WARNING: no real source configured (no tokens) — this would "
              "cache MOCK prices. Set pricecharting_token / soldcomps_token "
              "first. Aborting.")
        return 1

    titles = _select(args)
    print(f"pricing {len(titles)} title(s) via {getattr(source, 'name', '?')} ...")
    summary = run(cache, source, titles, max_age_days=args.max_age)
    print(summary)
    print(cache.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
