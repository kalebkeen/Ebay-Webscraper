"""
snapshot.py — precompute every price the phone will ever need.

Run this on a laptop, on whatever schedule suits you. It writes a single
static JSON file. Host that file anywhere that serves bytes (a GitHub raw
URL, a Gist, S3, a Pages site) and the Android app fetches it on a timer.

Why this shape instead of the phone calling a price API directly:

  NO KEY ON THE DEVICE. An API key inside an APK is extractable, and the
  quota it burns is yours. Here the key never leaves the machine running
  this script.

  NO CORS PROBLEM. PriceCharting does not send cross-origin headers, so a
  WebView cannot call it at all. A static JSON file has no such issue.

  NO SERVER. There is nothing to deploy, monitor, or pay for. A file on a
  CDN is the whole backend.

  RETRO PRICES MOVE WEEKLY. Hourly freshness buys nothing on a market where
  a title trades a few times a month. A snapshot is not a compromise here;
  it matches the data's actual tempo.

The split that makes it work: everything expensive and statistical — outlier
rejection, recency weighting, variant de-mixing, velocity — happens HERE,
once per title. What ships is a small table of finished numbers. The device
only runs the economics, which it must, because those depend on the asking
price in front of you and whether you are collecting it in person.

    python snapshot.py --out static/prices.json
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import date
from pathlib import Path

import catalog
import comps
import economics as ec
import mock_sources as ms
from listing_parser import Completeness as C, Region as R, Variant as V

# Every combination worth carrying. Sealed is deliberately absent: it is a
# different market with its own grading and reseal fraud, and a number for it
# derived from used-disc comps would be confident nonsense.
COMPLETENESS = [C.LOOSE, C.DISC_CASE, C.CIB]
SCHEMA_VERSION = 2


def build(source, today: date, region: R = R.NTSC_U,
          verbose: bool = True) -> dict:
    titles: dict[str, dict] = {}
    skipped = 0

    for entry in sorted(catalog.CATALOG, key=lambda t: t.canonical):
        variants = ([V.BLACK_LABEL, V.GREATEST_HITS] if entry.has_greatest_hits
                    else [V.BLACK_LABEL])
        skus: dict[str, dict] = {}

        for variant in variants:
            for completeness in COMPLETENESS:
                val = comps.value_sku(
                    title=entry.canonical, region=region, variant=variant,
                    completeness=completeness, source=source,
                    has_budget_reprint=entry.has_greatest_hits, today=today)
                if not val.quotable:
                    skipped += 1
                    continue
                # Short keys: this file is downloaded over mobile data, and
                # at full catalogue size the field names would outweigh the
                # numbers several times over.
                skus[f"{variant.value}|{completeness.value}"] = {
                    "r": round(val.conservative_resale, 2),   # what we price against
                    "e": round(val.expected_resale, 2),       # centre
                    "lo": round(val.p25, 2),
                    "hi": round(val.p75, 2),
                    "cf": val.confidence.value,
                    "n": round(val.n_effective, 1),
                    "d": round(val.est_days_to_sell, 1) if val.est_days_to_sell else None,
                }

        if skus:
            titles[entry.canonical] = {
                "liq": entry.liquidity,
                "rr": entry.repro_risk,
                "gh": bool(entry.has_greatest_hits),
                "sk": skus,
            }
        if verbose:
            print(f"  {entry.canonical:<38} {len(skus)} skus")

    fees, ops, hurdle, risk = ec.FeeModel(), ec.OpsModel(), ec.Hurdle(), ec.RiskModel()
    return {
        "schema": SCHEMA_VERSION,
        "generated": today.isoformat(),
        "region": region.value,
        # The client must not carry its own copy of these. Shipping the
        # constants with the prices means a fee change is a snapshot
        # regeneration, not an app release.
        "models": {
            "fee": {k: v for k, v in vars(fees).items()},
            "ops": {k: v for k, v in vars(ops).items()},
            "hurdle": {k: v for k, v in vars(hurdle).items()},
            "risk": {k: v for k, v in vars(risk).items()},
        },
        "titles": titles,
        "stats": {"titles": len(titles),
                  "skus": sum(len(t["sk"]) for t in titles.values()),
                  "skipped": skipped},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="static/prices.json")
    ap.add_argument("--gzip", action="store_true",
                    help="also write a .gz; most static hosts serve it directly")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    today = date(2026, 8, 22)
    # Swap for a PriceCharting / eBay adapter. It needs three methods:
    # sold_records, active_listing_count, quote.
    source = ms.CombinedSource(ms.MockMarketplace(seed=7, today=today),
                               ms.MockReference(today))
    source_name = "mock"

    if not args.quiet:
        print(f"building snapshot from '{source_name}' as of {today}\n")
    snap = build(source, today, verbose=not args.quiet)
    snap["source"] = source_name
    # The client shows a standing banner while this is true. A field tool
    # quietly serving synthetic prices is worse than one that refuses to open.
    snap["is_mock"] = source_name == "mock"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snap, separators=(",", ":"), sort_keys=True)
    out.write_text(payload)

    size = len(payload)
    line = (f"\n{out}  {size/1024:.1f} KB  "
            f"{snap['stats']['titles']} titles, {snap['stats']['skus']} skus")
    if args.gzip:
        gz = out.with_suffix(out.suffix + ".gz")
        gz.write_bytes(gzip.compress(payload.encode(), 9))
        line += f"  (gz {gz.stat().st_size/1024:.1f} KB)"
    print(line)
    if snap["stats"]["skipped"]:
        print(f"{snap['stats']['skipped']} skus had too few comps to price")


if __name__ == "__main__":
    main()
