#!/usr/bin/env python3
"""Refine bulk-title liquidity from real eBay data, replacing the offline prior.

The catalog ships ~4,173 uncurated titles whose `liquidity` is a conservative
offline PRIOR baked into catalog_data.py (derived from region breadth + known
mega-franchises). This tool upgrades that prior to a measured signal: for each
title it asks the eBay Browse API how many ACTIVE listings match, and maps that
count to a liquidity tier.

Honesty about what this measures:
  * Browse `total` is SUPPLY (how many are listed right now), not sell-through
    (how many sell per month, which is the real definition of liquidity). A
    title can sit on 200 stale listings that never move. This is a better-
    informed proxy than the offline prior, not ground truth. True velocity
    needs the restricted Marketplace Insights API or weeks of store.py
    harvesting.
  * The count->tier thresholds below are judgement calls. Run once, then use
    `--map-only` to re-map from the cached counts with different thresholds
    without spending any more API calls.

It's free within the Browse API's default daily call budget (~5,000/day), which
covers the whole catalog in one run. Credentials come from the environment:

    export EBAY_CLIENT_ID=...
    export EBAY_CLIENT_SECRET=...
    export EBAY_ENV=production        # or: sandbox

Usage:
    python tools/refine_liquidity.py                 # harvest + rewrite catalog_data.py
    python tools/refine_liquidity.py --limit 50      # try 50 titles first
    python tools/refine_liquidity.py --dry-run       # harvest, print, don't write
    python tools/refine_liquidity.py --map-only      # re-map tiers from cache only

The run is resumable: counts are cached to tools/liquidity_counts.json and a
re-run skips titles already fetched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))

import catalog_data  # noqa: E402

CACHE = HERE / "liquidity_counts.json"
DATA_FILE = PKG / "catalog_data.py"

# Active-listing count -> liquidity tier. Supply, not velocity: see module docs.
# Deliberately demanding at the top end because a wrong 'high' (0.5x dead-stock
# risk) makes the pipeline overpay. Tune with --map-only.
THRESHOLDS = [
    (150, "high"),     # >=150 active US listings
    (40,  "medium"),   # 40-149
    (8,   "low"),      # 8-39
    (0,   "thin"),     # 0-7
]


def tier_for(count: int) -> str:
    for floor, tier in THRESHOLDS:
        if count >= floor:
            return tier
    return "thin"


def load_cache() -> dict[str, int]:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict[str, int]) -> None:
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0),
                     encoding="utf-8")


def harvest(limit: int | None, cache: dict[str, int]) -> dict[str, int]:
    import ebay
    auth = ebay.EbayAuth()
    if not auth.configured:
        sys.exit("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set — see this "
                 "file's docstring. Nothing fetched.")
    client = ebay.EbayClient(auth=auth, marketplace="EBAY_US")

    todo = [r[0] for r in catalog_data.BULK if r[0] not in cache]
    if limit is not None:
        todo = todo[:limit]
    print(f"{len(cache)} cached, fetching {len(todo)} more "
          f"(of {len(catalog_data.BULK)} total)...")

    for i, canonical in enumerate(todo, 1):
        # Scope the query to the platform so a bare title like "Black" does not
        # match the entire category.
        query = f"{canonical} PlayStation 2"
        try:
            cache[canonical] = client.active_count(query)
        except ebay.NotEntitled as e:
            sys.exit(f"\n{e}")
        except ebay.EbayError as e:
            print(f"  ! {canonical}: {e} (leaving uncached, will retry next run)")
            continue
        if i % 25 == 0:
            save_cache(cache)
            print(f"  {i}/{len(todo)}  (last: {canonical} -> {cache[canonical]})")
    save_cache(cache)
    return cache


def rewrite(cache: dict[str, int]) -> dict[str, int]:
    """Rewrite catalog_data.py, replacing each liquidity with the measured tier
    where a count exists. Titles without a count keep their existing value."""
    import collections
    dist = collections.Counter()
    rows = []
    for row in catalog_data.BULK:
        canonical, regions, aliases = row[0], row[1], row[2]
        existing = row[3] if len(row) > 3 else "low"
        if canonical in cache:
            liq = tier_for(cache[canonical])
        else:
            liq = existing
        dist[liq] += 1
        rows.append((canonical, tuple(regions), tuple(aliases), liq))

    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        fh.write('"""\n')
        fh.write("Auto-generated bulk PS2 title data. DO NOT EDIT BY HAND.\n\n")
        fh.write("Source: English Wikipedia 'List of PlayStation 2 games (A-K)/(L-Z)'.\n")
        fh.write("Each entry: (canonical_title, (regions...), (aliases...), liquidity).\n")
        fh.write("regions is a subset of NA/EU/JP taken from the list's region columns.\n")
        fh.write("aliases are alternate regional/Japanese release names from the same rows.\n")
        fh.write("liquidity is measured from eBay active-listing counts where available\n")
        fh.write("(tools/refine_liquidity.py), else a conservative offline prior. It is a\n")
        fh.write("supply proxy, not sell-through. These entries stay curated=False.\n")
        fh.write('"""\n\n')
        fh.write("BULK = [\n")
        for c, r, a, liq in rows:
            fh.write(f"    ({c!r}, {r!r}, {a!r}, {liq!r}),\n")
        fh.write("]\n")
    return dict(dist)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only fetch this many uncached titles (testing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch/map and print the distribution but do not write")
    ap.add_argument("--map-only", action="store_true",
                    help="skip the API; re-map tiers from the cached counts")
    args = ap.parse_args()

    cache = load_cache()
    if not args.map_only:
        cache = harvest(args.limit, cache)
    elif not cache:
        sys.exit("--map-only but no cache found; run a harvest first.")

    # Preview distribution from the cache.
    import collections
    preview = collections.Counter(tier_for(c) for c in cache.values())
    print("\nmeasured tiers (from cache):",
          {t: preview[t] for t in ("high", "medium", "low", "thin")},
          f"| {len(cache)} titles measured")

    if args.dry_run:
        print("--dry-run: catalog_data.py left unchanged.")
        return
    dist = rewrite(cache)
    print("wrote catalog_data.py; full liquidity distribution:",
          {t: dist[t] for t in ("high", "medium", "low", "thin")})
    print("Remember to re-run ./sync_android.sh before building the APK.")


if __name__ == "__main__":
    main()
