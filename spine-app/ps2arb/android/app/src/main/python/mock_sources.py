"""
mock_sources.py — synthetic comp data for offline development.

Two implementations of the CompSource protocol:

  MockReference   — behaves like PriceCharting: tier prices, no variant
                    split, no dispersion, no velocity.
  MockMarketplace — behaves like eBay sold listings: individual sales,
                    mostly unlabelled variants, and contaminated.

The contamination is the point. A mock that returns clean log-normal prices
would validate the estimator against a world that does not exist. Real sold
data contains miscategorised lots, graded copies sold as raw, international
sales with punitive shipping, and best offers recorded at list price. If the
MAD filter cannot survive those, it will not survive eBay.

Anchor prices are plausible 2026 figures, not measured ones. They exist to
exercise the pipeline, not to price your inventory.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from comps import CompQuote, SoldRecord
from listing_parser import Completeness, Region, Variant


@dataclass(frozen=True)
class Anchor:
    loose: float
    cib: float
    sales_per_month: float
    active_listings: int


# Rough NTSC-U figures. Note the shape: the liquid titles are all under $20,
# which is exactly the band where eBay's fee floor eats the entire spread.
ANCHORS: dict[str, Anchor] = {
    # thin supply, high value
    "Rule of Rose":                     Anchor(250, 500, 0.8, 4),
    "Haunting Ground":                  Anchor(150, 300, 1.2, 5),
    "Kuon":                             Anchor(200, 400, 0.6, 3),
    "Michigan: Report from Hell":       Anchor(100, 180, 1.0, 4),
    "Fatal Frame III: The Tormented":   Anchor(90, 170, 2.0, 7),
    ".hack//Quarantine":                Anchor(60, 110, 1.5, 6),
    "God Hand":                         Anchor(70, 130, 3.0, 9),
    "Silent Hill 3":                    Anchor(70, 140, 4.0, 14),
    "Persona 3 FES":                    Anchor(45, 80, 5.0, 16),
    "Suikoden V":                       Anchor(40, 70, 3.5, 12),
    "Silent Hill 2":                    Anchor(45, 90, 8.0, 26),
    # mid
    "Okami":                            Anchor(25, 45, 9.0, 30),
    "We Love Katamari":                 Anchor(22, 40, 7.0, 24),
    "Ico":                              Anchor(18, 35, 10.0, 33),
    "Katamari Damacy":                  Anchor(18, 30, 12.0, 40),
    "Metal Gear Solid 3: Snake Eater":  Anchor(15, 28, 14.0, 45),
    "Devil May Cry 3: Dante's Awakening": Anchor(15, 28, 11.0, 38),
    "Shadow of the Colossus":           Anchor(12, 22, 18.0, 60),
    # high liquidity, low value — the dead zone
    "Grand Theft Auto: San Andreas":    Anchor(10, 18, 40.0, 130),
    "Kingdom Hearts II":                Anchor(10, 18, 22.0, 70),
    "Guitar Hero II":                   Anchor(10, 18, 15.0, 55),
    "God of War II":                    Anchor(9, 16, 20.0, 65),
    "Grand Theft Auto: Vice City":      Anchor(8, 15, 30.0, 100),
    "Kingdom Hearts":                   Anchor(8, 15, 25.0, 80),
    "Final Fantasy X":                  Anchor(8, 14, 28.0, 90),
    "Final Fantasy XII":                Anchor(8, 14, 18.0, 62),
    "God of War":                       Anchor(8, 14, 24.0, 78),
    "Grand Theft Auto III":             Anchor(7, 13, 26.0, 85),
    "Tony Hawk's Pro Skater 4":         Anchor(7, 13, 12.0, 44),
}

# Titles whose prices are climbing, as fractional drift per month.
TRENDING: dict[str, float] = {
    "Rule of Rose": 0.020,
    "Kuon": 0.018,
    "Persona 3 FES": 0.012,
    "God Hand": 0.010,
    "Guitar Hero II": -0.008,     # plastic-instrument titles are soft
    "Tony Hawk's Pro Skater 4": -0.006,
}

REGION_MULTIPLIER = {
    Region.NTSC_U: 1.00,
    Region.PAL: 0.70,
    Region.NTSC_J: 0.55,
    Region.UNKNOWN: 1.00,
}


class MockReference:
    """PriceCharting-shaped: tier prices only, variants not separated."""

    name = "mock-reference"

    def __init__(self, as_of: date | None = None):
        self.as_of = as_of or date.today()

    def quote(self, title: str, region: Region) -> dict[Completeness, CompQuote]:
        a = ANCHORS.get(title)
        if a is None:
            return {}
        m = REGION_MULTIPLIER[region]
        return {
            Completeness.LOOSE: CompQuote(
                Completeness.LOOSE, round(a.loose * m, 2), n=0,
                as_of=self.as_of, variant_split=False, source=self.name),
            Completeness.CIB: CompQuote(
                Completeness.CIB, round(a.cib * m, 2), n=0,
                as_of=self.as_of, variant_split=False, source=self.name),
        }

    def sold_records(self, title, region, since):
        return []

    def active_listing_count(self, title, region):
        return None


class MockMarketplace:
    """eBay-sold-shaped: individual contaminated sales, mostly unlabelled.

    Deterministic given a seed, so tests are reproducible and a regression
    in the estimator cannot be dismissed as sampling noise.
    """

    name = "mock-marketplace"

    #: fraction of sales that are junk of one kind or another
    CONTAMINATION_RATE = 0.10

    def __init__(self, seed: int = 7, today: date | None = None,
                 contamination: float | None = None):
        self.seed = seed
        self.today = today or date.today()
        self.contamination = (self.CONTAMINATION_RATE if contamination is None
                              else contamination)

    def _rng(self, title: str, region: Region) -> random.Random:
        return random.Random(f"{self.seed}|{title}|{region.value}")

    def sold_records(self, title: str, region: Region,
                     since: date) -> list[SoldRecord]:
        a = ANCHORS.get(title)
        if a is None:
            return []
        rng = self._rng(title, region)
        m = REGION_MULTIPLIER[region]
        drift = TRENDING.get(title, 0.0)
        span_days = max((self.today - since).days, 1)
        n = max(int(a.sales_per_month * span_days / 30.0), 0)

        out: list[SoldRecord] = []
        for _ in range(n):
            age = rng.randint(0, span_days)
            sold_on = self.today - timedelta(days=age)

            comp = rng.choices(
                [Completeness.LOOSE, Completeness.DISC_CASE, Completeness.CIB],
                weights=[0.45, 0.25, 0.30],
            )[0]
            base = a.loose if comp is Completeness.LOOSE else a.cib
            if comp is Completeness.DISC_CASE:
                base = a.loose + 0.35 * (a.cib - a.loose)

            # Prices are multiplicative, so noise is log-normal.
            price = base * m * math_exp(rng.gauss(0.0, 0.22))
            # Apply the trend backwards from today.
            price *= (1.0 + drift) ** (-age / 30.0)

            # Most sellers never state the variant.
            variant = rng.choices(
                [Variant.UNKNOWN, Variant.GREATEST_HITS, Variant.BLACK_LABEL],
                weights=[0.70, 0.18, 0.12],
            )[0]
            if variant is Variant.GREATEST_HITS:
                price *= 0.60
            elif variant is Variant.BLACK_LABEL:
                price *= 1.25

            shipping = round(rng.uniform(4.50, 6.50), 2)
            note = ""

            # --- contamination -------------------------------------------
            if rng.random() < self.contamination:
                kind = rng.choice(["lot", "graded", "intl", "bo_at_list"])
                if kind == "lot":
                    price *= rng.uniform(3.0, 9.0)     # miscategorised bundle
                    note = "miscategorised lot"
                elif kind == "graded":
                    price *= rng.uniform(4.0, 12.0)    # WATA/VGA sold as raw
                    note = "graded copy"
                elif kind == "intl":
                    shipping = round(rng.uniform(22.0, 48.0), 2)
                    note = "international shipping"
                else:
                    price *= rng.uniform(1.25, 1.6)    # offer recorded at list
                    note = "best offer recorded at list"

            out.append(SoldRecord(
                price=round(max(price, 0.99), 2),
                shipping=shipping,
                sold_on=sold_on,
                completeness=comp,
                variant=variant,
                region=region,
                note=note,
            ))
        return sorted(out, key=lambda r: r.sold_on)

    def quote(self, title, region):
        return {}

    def active_listing_count(self, title: str, region: Region) -> int | None:
        a = ANCHORS.get(title)
        if a is None:
            return None
        m = {Region.NTSC_U: 1.0, Region.PAL: 0.35,
             Region.NTSC_J: 0.25, Region.UNKNOWN: 1.0}[region]
        return max(int(a.active_listings * m), 1)


class CombinedSource:
    """Marketplace sales first, reference price as fallback and cross-check.

    This mirrors the real setup: eBay sold data where you can get it,
    PriceCharting for the long tail where you cannot.
    """

    name = "combined"

    def __init__(self, marketplace: MockMarketplace, reference: MockReference):
        self.marketplace = marketplace
        self.reference = reference

    def quote(self, title, region):
        return self.reference.quote(title, region)

    def sold_records(self, title, region, since):
        return self.marketplace.sold_records(title, region, since)

    def active_listing_count(self, title, region):
        return self.marketplace.active_listing_count(title, region)


def math_exp(x: float) -> float:
    import math
    return math.exp(x)
