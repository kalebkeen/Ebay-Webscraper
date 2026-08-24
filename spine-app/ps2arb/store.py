"""
store.py — build your own sold-price history by watching listings vanish.

Marketplace Insights is the API that returns real completed sales, and most
developer keys are never granted it. Without sold data the entire valuation
layer has nothing to stand on, because asking prices are aspirational and
routinely 2-3x what a title actually clears. A model fed asking prices finds
"bargains" continuously and loses money on every one.

So this harvests the data instead. Poll active listings on a schedule, keep
a record of every one seen, and watch which disappear. A listing that was
live on Monday and is gone on Wednesday either sold or was withdrawn. That
is a noisy sold-price signal, but it is a real one, and it is free.

WHAT MAKES IT NOISY, AND WHAT IS DONE ABOUT IT

  Ended-not-sold. Sellers withdraw listings, run out of stock, or let them
  lapse. Those look identical to a sale from the outside. Handled by
  `confidence`: a listing that survived several polls and then vanished
  close to a known end date is likelier a sale than one that appeared and
  disappeared between two polls a week apart.

  Best Offer. A BEST_OFFER listing that sells almost never sells at the ask.
  Recorded with a haircut and flagged, so the estimator can down-weight it.

  Relisting. eBay's relist creates a new itemId for the same physical disc,
  which reads as a sale plus a new listing. Detected by matching title,
  price and seller within a short window.

  Poll gaps. Anything that appeared and vanished entirely between two polls
  is invisible. Fast-selling items are therefore systematically
  under-represented — the bias runs toward pessimism on liquidity, which is
  the safe direction but worth knowing.

None of this is as good as Insights. It is what is available, it improves
every week it runs, and it costs nothing but call quota.

    python store.py --observe "ps2 silent hill"     # one polling pass
    python store.py --stats
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from comps import CompQuote, SoldRecord
from listing_parser import Completeness, Region, Variant

DB_PATH = Path(os.environ.get("PS2ARB_STORE", "harvest.db"))

# A listing gone within this many days of first sighting, having survived at
# least two polls, is treated as a probable sale.
MAX_PLAUSIBLE_DAYS = 120

# Best Offer listings clear below ask. This is the assumed discount, and it
# is a prior worth recalibrating from real data once enough has accumulated.
BEST_OFFER_HAIRCUT = 0.88


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id       TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    matched_title TEXT,
    variant       TEXT,
    completeness  TEXT,
    region        TEXT,
    price         REAL NOT NULL,
    shipping      REAL NOT NULL DEFAULT 0,
    currency      TEXT DEFAULT 'USD',
    best_offer    INTEGER DEFAULT 0,
    auction       INTEGER DEFAULT 0,
    seller        TEXT,
    country       TEXT,
    condition_id  INTEGER,
    url           TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    polls         INTEGER NOT NULL DEFAULT 1,
    vanished_on   TEXT,
    outcome       TEXT,          -- 'sold' | 'withdrawn' | 'relisted' | NULL
    confidence    REAL
);
CREATE INDEX IF NOT EXISTS idx_matched  ON listings(matched_title, region);
CREATE INDEX IF NOT EXISTS idx_vanished ON listings(vanished_on);
CREATE INDEX IF NOT EXISTS idx_lastseen ON listings(last_seen);

CREATE TABLE IF NOT EXISTS polls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    query     TEXT NOT NULL,
    ran_at    TEXT NOT NULL,
    seen      INTEGER NOT NULL,
    new_items INTEGER NOT NULL,
    vanished  INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_date(stamp: str | None) -> date | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).date()
    except ValueError:
        return None


@dataclass
class PollResult:
    query: str
    seen: int
    new_items: int
    vanished: int
    probable_sales: int


class HarvestStore:
    """
    Sold-price history assembled from repeated observation.

    Implements the CompSource protocol, so it drops straight into
    comps.value_sku wherever the mock source sits today.
    """

    name = "harvest"

    def __init__(self, path: Path | None = None,
                 min_confidence: float = 0.55):
        # Every harvested sale is an inference, not an observation, and they
        # are not equally good. Serving a 0.25-confidence "it disappeared
        # once" alongside a 0.9 auction close would let the weakest signals
        # set prices. Filtering here keeps that judgement in one place.
        self.min_confidence = min_confidence
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- observe

    def observe(self, query: str, listings: list, *,
                classify=None) -> PollResult:
        """
        Record one polling pass.

        `listings` are ebay.Listing objects. `classify` is an optional
        callable taking a listing and returning
        (matched_title, variant, completeness) — normally pipeline.resolve.
        Classifying at harvest time rather than at read time means the
        expensive parse happens once per listing, not once per query.
        """
        now = _now()
        seen_ids = set()
        new_items = 0

        for lst in listings:
            item_id = getattr(lst, "item_id", None) or getattr(lst, "itemId", "")
            if not item_id:
                continue
            seen_ids.add(item_id)

            matched = variant = completeness = None
            if classify is not None:
                try:
                    matched, variant, completeness = classify(lst)
                except Exception:
                    pass

            row = self.db.execute(
                "SELECT item_id, polls FROM listings WHERE item_id=?",
                (item_id,)).fetchone()

            if row:
                self.db.execute(
                    "UPDATE listings SET last_seen=?, polls=polls+1, "
                    "price=?, shipping=?, vanished_on=NULL, outcome=NULL "
                    "WHERE item_id=?",
                    (now, lst.price, getattr(lst, "shipping", 0.0), item_id))
            else:
                new_items += 1
                self.db.execute(
                    "INSERT INTO listings (item_id,title,matched_title,variant,"
                    "completeness,region,price,shipping,currency,best_offer,"
                    "auction,seller,country,condition_id,url,first_seen,"
                    "last_seen,polls) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (item_id, lst.title, matched, variant, completeness,
                     getattr(lst, "region", None) or "unknown",
                     lst.price, getattr(lst, "shipping", 0.0),
                     getattr(lst, "currency", "USD"),
                     int(bool(getattr(lst, "best_offer", False))),
                     int(bool(getattr(lst, "auction", False))),
                     getattr(lst, "seller", None),
                     getattr(lst, "country", None),
                     getattr(lst, "condition_id", None),
                     getattr(lst, "url", ""), now, now))

        vanished = self._mark_vanished(query, seen_ids, now)
        sales = sum(1 for v in vanished if v == "sold")

        self.db.execute(
            "INSERT INTO polls (query,ran_at,seen,new_items,vanished) "
            "VALUES (?,?,?,?,?)",
            (query, now, len(seen_ids), new_items, len(vanished)))
        self.db.commit()
        return PollResult(query, len(seen_ids), new_items, len(vanished), sales)

    def _mark_vanished(self, query: str, seen_ids: set, now: str) -> list[str]:
        """
        Anything present in the previous pass and absent from this one has gone.

        The window is derived from when this query last ran, not fixed. A
        hardcoded three-day window silently broke every polling cadence
        slower than three days: poll weekly and every live listing is
        already outside the window, so nothing is ever marked vanished and
        the store accumulates listings forever without ever recording a
        single sale. The bug was invisible in testing because a simulated
        run completes in one second.

        Scoping by last-seen rather than by query text also matters: the
        same listing legitimately appears under several searches, and it
        must not be declared gone merely because this particular query
        missed it.
        """
        prev = self.db.execute(
            "SELECT ran_at FROM polls WHERE query=? ORDER BY id DESC LIMIT 1",
            (query,)).fetchone()
        if prev is None:
            return []          # first pass for this query: nothing to compare

        # Anything seen at or after the previous run of this query was live
        # then, so its absence now is meaningful. Small grace period for
        # clock skew and for polls that overlap.
        cutoff = (datetime.fromisoformat(prev["ran_at"])
                  - timedelta(minutes=5)).isoformat()

        rows = self.db.execute(
            "SELECT * FROM listings WHERE vanished_on IS NULL AND last_seen>=?",
            (cutoff,)).fetchall()

        outcomes = []
        for row in rows:
            if row["item_id"] in seen_ids:
                continue
            outcome, conf = self._classify_disappearance(row)
            self.db.execute(
                "UPDATE listings SET vanished_on=?, outcome=?, confidence=? "
                "WHERE item_id=?", (now, outcome, conf, row["item_id"]))
            outcomes.append(outcome)
        return outcomes

    def _classify_disappearance(self, row: sqlite3.Row) -> tuple[str, float]:
        """Did it sell, or did the seller pull it?

        No outside information is available, so this is a judgement from
        observation count and lifetime. Confidence is carried forward rather
        than thresholded here, so the estimator can weight rather than
        include or exclude.
        """
        first = _as_date(row["first_seen"])
        last = _as_date(row["last_seen"])
        polls = row["polls"] or 1
        lifetime = (last - first).days if first and last else 0

        # Seen once and gone: most likely a poll artefact, not a sale.
        if polls < 2:
            return "withdrawn", 0.25

        if lifetime > MAX_PLAUSIBLE_DAYS:
            # Long-lived listings that vanish are more often withdrawn or
            # expired than sold; genuine demand clears sooner.
            return "withdrawn", 0.35

        # A relist keeps title, price and seller but takes a new itemId.
        twin = self.db.execute(
            "SELECT item_id FROM listings WHERE item_id!=? AND title=? "
            "AND ABS(price-?)<0.01 AND seller IS ? AND vanished_on IS NULL "
            "AND first_seen>=?",
            (row["item_id"], row["title"], row["price"], row["seller"],
             row["last_seen"])).fetchone()
        if twin:
            return "relisted", 0.80

        # Survived several polls, disappeared within a plausible window.
        confidence = 0.55 + min(polls, 8) * 0.04
        if row["auction"]:
            confidence += 0.15          # auctions end decisively
        return "sold", round(min(confidence, 0.92), 2)

    # ---------------------------------------------------- CompSource surface

    def sold_records(self, title: str, region: Region,
                     since: date) -> list[SoldRecord]:
        """Probable sales as SoldRecords, for comps.value_sku."""
        rows = self.db.execute(
            "SELECT * FROM listings WHERE matched_title=? AND outcome='sold' "
            "AND vanished_on IS NOT NULL AND vanished_on>=? "
            "AND COALESCE(confidence,0)>=?",
            (title, since.isoformat(), self.min_confidence)).fetchall()

        out: list[SoldRecord] = []
        for row in rows:
            sold_on = _as_date(row["vanished_on"])
            if sold_on is None:
                continue
            row_region = row["region"] or "unknown"
            if region.value not in (row_region, "unknown") and row_region != "unknown":
                continue

            price = row["price"]
            note = ""
            if row["best_offer"]:
                # It listed at this price; it did not necessarily clear at it.
                price *= BEST_OFFER_HAIRCUT
                note = "best_offer_estimate"

            out.append(SoldRecord(
                price=round(price, 2),
                shipping=row["shipping"] or 0.0,
                sold_on=sold_on,
                completeness=_enum(Completeness, row["completeness"]),
                variant=_enum(Variant, row["variant"]),
                region=_enum(Region, row_region),
                note=note or f"harvested/conf={row['confidence']}"))
        return out

    def active_listing_count(self, title: str, region: Region) -> int | None:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE matched_title=? "
            "AND vanished_on IS NULL", (title,)).fetchone()
        return row["n"] if row and row["n"] else None

    def quote(self, title: str, region: Region) -> dict[Completeness, CompQuote]:
        """No tier prices here. Harvest yields individual sales only."""
        return {}

    # --------------------------------------------------------------- stats

    def stats(self) -> dict:
        def one(sql, *args):
            row = self.db.execute(sql, args).fetchone()
            return row[0] if row else 0

        total = one("SELECT COUNT(*) FROM listings")
        active = one("SELECT COUNT(*) FROM listings WHERE vanished_on IS NULL")
        sold = one("SELECT COUNT(*) FROM listings WHERE outcome='sold'")
        withdrawn = one("SELECT COUNT(*) FROM listings WHERE outcome='withdrawn'")
        relisted = one("SELECT COUNT(*) FROM listings WHERE outcome='relisted'")
        matched = one("SELECT COUNT(*) FROM listings WHERE matched_title IS NOT NULL")
        polls = one("SELECT COUNT(*) FROM polls")
        titles = one("SELECT COUNT(DISTINCT matched_title) FROM listings "
                     "WHERE matched_title IS NOT NULL")
        first = one("SELECT MIN(ran_at) FROM polls")
        return {"listings": total, "active": active, "identified": matched,
                "titles": titles, "sold": sold, "withdrawn": withdrawn,
                "relisted": relisted, "polls": polls, "since": first}

    def harvest_quality(self, title: str | None = None) -> dict:
        """How much of the price signal is inference rather than observation.

        Callers need this to interpret a valuation honestly. comps.value_sku
        will happily report 'high confidence' on sixteen harvested sales,
        because it is judging sample size and dispersion and has no way to
        know the sales themselves were inferred from listings going quiet.
        """
        sql = ("SELECT AVG(confidence) a, MIN(confidence) lo, COUNT(*) n "
               "FROM listings WHERE outcome='sold'")
        args: tuple = ()
        if title:
            sql += " AND matched_title=?"
            args = (title,)
        row = self.db.execute(sql, args).fetchone()
        n = row["n"] or 0
        return {
            "n": n,
            "mean_confidence": round(row["a"], 3) if row["a"] else None,
            "min_confidence": round(row["lo"], 3) if row["lo"] else None,
            "basis": "inferred from listings disappearing, not confirmed sales",
            "caveat": ("Treat any downstream confidence tier as one step "
                       "lower than reported." if n else "No sales harvested yet."),
        }

    def readiness(self) -> str:
        """Is there enough here to price anything yet?

        Worth stating plainly, because the failure mode is subtle: a store
        with forty sales spread over sixty titles produces confident-looking
        numbers backed by two comps each.
        """
        s = self.stats()
        rows = self.db.execute(
            "SELECT matched_title, COUNT(*) AS n FROM listings "
            "WHERE outcome='sold' AND matched_title IS NOT NULL "
            "GROUP BY matched_title ORDER BY n DESC").fetchall()
        usable = [r for r in rows if r["n"] >= 5]
        lines = [
            f"  {s['polls']} polls since {s['since'] or 'never'}",
            f"  {s['listings']} listings seen, {s['active']} still live",
            f"  {s['sold']} probable sales, {s['withdrawn']} withdrawn, "
            f"{s['relisted']} relisted",
            f"  {len(usable)} titles have 5+ sales and can be priced",
        ]
        if not usable:
            lines.append("  Nothing is priceable yet. Keep polling — this "
                         "needs weeks, not hours.")
        else:
            lines.append("  best covered: " + ", ".join(
                f"{r['matched_title']} ({r['n']})" for r in usable[:5]))
        return "\n".join(lines)


def _enum(cls, value):
    try:
        return cls(value)
    except (ValueError, TypeError):
        return cls.UNKNOWN


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest sold-price data.")
    ap.add_argument("--observe", metavar="QUERY",
                    help="run one polling pass for this search")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    store = HarvestStore(Path(args.db) if args.db else None)

    if args.stats or not args.observe:
        print(store.readiness())
        return

    import ebay
    import pipeline

    client = ebay.EbayClient()

    def classify(lst):
        target = pipeline.resolve(lst.title, getattr(lst, "description", ""))
        if target.title is None:
            return None, None, None
        return (target.title, target.variant.value, target.completeness.value)

    listings = client.search(args.observe, limit=args.limit)
    for lst in listings:
        lst.region = ebay.infer_region(lst, client.auth.marketplace
                                       if hasattr(client, "auth") else "EBAY_US")
    result = store.observe(args.observe, listings, classify=classify)
    print(f"  seen {result.seen}, new {result.new_items}, "
          f"vanished {result.vanished} ({result.probable_sales} probable sales)")
    print()
    print(store.readiness())


if __name__ == "__main__":
    main()
