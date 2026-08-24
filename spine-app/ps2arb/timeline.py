"""
timeline.py — a market with a fixed past, and a clock you cannot cheat.

Two pieces of infrastructure that Stage 4 cannot exist without.

TIMELINE MARKETPLACE
    `MockMarketplace` generates sales lazily from the query window, so the
    number of sales "before May" depends on whether you asked in May or in
    August. That is fine for testing an estimator on one date and fatal for
    a backtest, which is precisely a claim about what was knowable when.
    `Timeline` draws the entire history once at construction and then only
    ever serves slices of it.

POINT-IN-TIME SOURCE
    Look-ahead bias is the standard way a backtest reports a strategy that
    does not exist. It is rarely deliberate -- it arrives as a comp query
    that happens to include next month's sales. Rather than rely on every
    call site remembering to filter, `PointInTime` wraps a source and drops
    anything after its as-of date, so the leak cannot be written in the
    first place.

The simulator also carries a `truth` dict of the parameters it generated
from. Stage 4's own test suite injects deliberate errors there -- a real
Greatest Hits ratio different from the one the model assumes -- and checks
that the calibration report notices. A backtest that cannot detect a known
planted bias has no power to detect an unknown one.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from comps import CompQuote, SoldRecord
from listing_parser import Completeness, Region, Variant
from mock_sources import ANCHORS, REGION_MULTIPLIER, TRENDING


@dataclass(frozen=True)
class TruthParams:
    """
    The data-generating process. The model under test does NOT get to see
    this; the backtest uses it only to plant known errors and confirm they
    are detected.
    """
    gh_price_ratio: float = 0.55        # comps.GH_PRICE_RATIO assumes 0.55
    gh_population_share: float = 0.60   # comps.GH_POPULATION_SHARE assumes 0.60
    manual_premium_share: float = 0.65  # comps.MANUAL_PREMIUM_SHARE assumes 0.65
    noise_sigma: float = 0.22
    contamination: float = 0.10
    variant_label_rate: float = 0.30    # share of listings stating a variant


@dataclass
class ListingEvent:
    """A listing that appeared, and what became of it.

    The outcome of the SOURCE listing is the cleanest evidence a backtest
    has. If the pipeline flags something as badly underpriced and it then
    sits unsold for three months at that price, the valuation was wrong --
    and no assumption about what would have happened in your hands is
    needed to know it.
    """
    title: str
    region: Region
    variant: Variant
    completeness: Completeness
    listed_on: date
    ask: float
    ship_in: float
    sold_on: date | None = None
    sold_price: float | None = None
    raw_title: str = ""
    description: str = ""

    @property
    def sold(self) -> bool:
        return self.sold_on is not None


class Timeline:
    """A fixed synthetic market history, drawn once and then immutable."""

    def __init__(self, start: date, end: date, seed: int = 23,
                 truth: TruthParams | None = None,
                 titles: list[str] | None = None):
        self.start, self.end = start, end
        self.truth = truth or TruthParams()
        self.seed = seed
        self.titles = titles or list(ANCHORS)
        self._sales: dict[tuple[str, Region], list[SoldRecord]] = {}
        # Parallel to _sales: what the item ACTUALLY was, regardless of how
        # the seller labelled it. Scoring needs this to compare a
        # variant-specific prediction against a variant-matched realisation.
        self._true_variant: dict[tuple[str, Region], list[Variant]] = {}
        self._listings: list[ListingEvent] = []
        self._build()

    # ---------------------------------------------------------------- build

    def _true_price(self, title: str, comp: Completeness, variant: Variant,
                    when: date, rng: random.Random, region: Region) -> float:
        a = ANCHORS[title]
        t = self.truth
        if comp is Completeness.LOOSE:
            base = a.loose
        elif comp is Completeness.CIB:
            base = a.cib
        else:
            base = a.loose + (1.0 - t.manual_premium_share) * (a.cib - a.loose)

        base *= REGION_MULTIPLIER[region]

        # Variant. The population is mostly budget reprints where they exist,
        # and the price gap is `gh_price_ratio` -- both of which the model
        # only knows as priors.
        if variant is Variant.GREATEST_HITS:
            base *= t.gh_price_ratio
        elif variant is Variant.BLACK_LABEL:
            # Black label is the reference; the aggregate sits below it.
            pass

        drift = TRENDING.get(title, 0.0)
        age_days = (self.end - when).days
        base *= (1.0 + drift) ** (-age_days / 30.0)
        return base * math.exp(rng.gauss(0.0, t.noise_sigma))

    def _draw_variant(self, title: str, rng: random.Random) -> Variant:
        from catalog import CATALOG
        entry = next((e for e in CATALOG if e.canonical == title), None)
        if entry is None or not entry.has_greatest_hits:
            return Variant.BLACK_LABEL
        return (Variant.GREATEST_HITS
                if rng.random() < self.truth.gh_population_share
                else Variant.BLACK_LABEL)

    def _build(self) -> None:
        span = max((self.end - self.start).days, 1)
        for title in self.titles:
            if title not in ANCHORS:
                continue
            a = ANCHORS[title]
            for region in (Region.NTSC_U,):
                rng = random.Random(f"{self.seed}|{title}|{region.value}")
                n = max(int(a.sales_per_month * span / 30.0), 0)
                records: list[SoldRecord] = []
                truths: list[Variant] = []
                for _ in range(n):
                    when = self.start + timedelta(days=rng.randint(0, span))
                    comp = rng.choices(
                        [Completeness.LOOSE, Completeness.DISC_CASE, Completeness.CIB],
                        weights=[0.45, 0.25, 0.30])[0]
                    variant = self._draw_variant(title, rng)
                    price = self._true_price(title, comp, variant, when, rng, region)
                    shipping = round(rng.uniform(4.50, 6.50), 2)

                    # Most sellers never state the variant, so most comps are
                    # unlabelled even though the underlying item has one.
                    stated = (variant if rng.random() < self.truth.variant_label_rate
                              else Variant.UNKNOWN)

                    note = ""
                    if rng.random() < self.truth.contamination:
                        kind = rng.choice(["lot", "graded", "intl", "bo_at_list"])
                        if kind == "lot":
                            price *= rng.uniform(3.0, 9.0); note = "lot"
                        elif kind == "graded":
                            price *= rng.uniform(4.0, 12.0); note = "graded"
                        elif kind == "intl":
                            shipping = round(rng.uniform(22.0, 48.0), 2); note = "intl"
                        else:
                            price *= rng.uniform(1.25, 1.6); note = "bo_at_list"

                    records.append(SoldRecord(
                        price=round(max(price, 0.99), 2), shipping=shipping,
                        sold_on=when, completeness=comp, variant=stated,
                        region=region, note=note))
                    truths.append(variant)
                order = sorted(range(len(records)), key=lambda i: records[i].sold_on)
                self._sales[(title, region)] = [records[i] for i in order]
                self._true_variant[(title, region)] = [truths[i] for i in order]

        self._build_listings()

    def _build_listings(self) -> None:
        """
        Candidate listings to run the pipeline against.

        Most are priced at or above fair value -- that is what a real feed
        looks like. A minority are genuinely underpriced, and a minority are
        underpriced for a REASON the text does not state, which is the
        adverse-selection case the whole pipeline exists to survive.
        """
        span = max((self.end - self.start).days, 1)
        rng = random.Random(f"{self.seed}|listings")
        for title in self.titles:
            if title not in ANCHORS:
                continue
            a = ANCHORS[title]
            n = max(int(a.sales_per_month * span / 30.0 * 0.6), 2)
            for _ in range(n):
                listed = self.start + timedelta(days=rng.randint(0, span - 30))
                comp = rng.choices(
                    [Completeness.LOOSE, Completeness.DISC_CASE, Completeness.CIB],
                    weights=[0.45, 0.25, 0.30])[0]
                variant = self._draw_variant(title, rng)
                fair = self._true_price(title, comp, variant, listed,
                                        random.Random(rng.random()), Region.NTSC_U)

                roll = rng.random()
                hidden_defect = False
                if roll < 0.70:                      # priced at or above fair
                    ask_ratio = rng.uniform(0.95, 1.6)
                elif roll < 0.88:                    # genuine bargain
                    ask_ratio = rng.uniform(0.35, 0.75)
                else:                                # cheap for an unstated reason
                    ask_ratio = rng.uniform(0.25, 0.60)
                    hidden_defect = True

                ask = round(max(fair * ask_ratio, 1.00), 2)
                ship_in = round(rng.uniform(0.0, 6.50), 2)

                # Does the source listing sell, and how fast? Underpriced
                # listings clear quickly; overpriced ones sit. This is the
                # ground truth the calibration report scores against.
                value_ratio = ask / max(fair, 0.01)
                p_sell = max(0.05, min(0.97, 1.35 - 0.75 * value_ratio))
                sold_on = None
                sold_price = None
                if rng.random() < p_sell:
                    lag = max(1, int(rng.expovariate(1.0 / (12.0 + 55.0 * value_ratio))))
                    when = listed + timedelta(days=lag)
                    if when <= self.end:
                        sold_on, sold_price = when, ask

                self._listings.append(ListingEvent(
                    title=title, region=Region.NTSC_U, variant=variant,
                    completeness=comp, listed_on=listed, ask=ask,
                    ship_in=ship_in, sold_on=sold_on, sold_price=sold_price,
                    raw_title=self._render_title(title, comp, variant,
                                                 hidden_defect, rng),
                    description=self._render_desc(hidden_defect, rng),
                ))
        self._listings.sort(key=lambda e: e.listed_on)

    def _render_title(self, title, comp, variant, hidden_defect, rng) -> str:
        bits = [title, "PS2"]
        if variant is Variant.GREATEST_HITS and rng.random() < 0.45:
            bits.append("Greatest Hits")
        elif variant is Variant.BLACK_LABEL and rng.random() < 0.30:
            bits.append("Black Label")
        bits.append({Completeness.LOOSE: "disc only",
                     Completeness.DISC_CASE: "disc and case",
                     Completeness.CIB: "complete"}[comp])
        if not hidden_defect and rng.random() < 0.5:
            bits.append("tested working")
        return " ".join(bits)

    def _render_desc(self, hidden_defect, rng) -> str:
        if not hidden_defect:
            return rng.choice(["Ships fast from a smoke-free home.",
                               "Plays great, no issues.", ""])
        # The defect IS disclosed, just buried -- which is exactly the case
        # Stage 1's description scan has to catch.
        return rng.choice([
            "Ships next day. Please note there is a hairline crack near the hub.",
            "Fast shipping. Untested, no way to check.",
            "Great condition overall. Disc is heavily scratched.",
            "Custom case with printed cover art, burned backup disc.",
        ])

    # ---------------------------------------------------------------- serve

    def sales(self, title: str, region: Region, since: date,
              until: date) -> list[SoldRecord]:
        return [r for r in self._sales.get((title, region), [])
                if since <= r.sold_on <= until]

    def sales_detailed(self, title: str, region: Region, since: date,
                       until: date) -> list[tuple[SoldRecord, Variant]]:
        """Sales paired with their TRUE variant. Scoring only -- not a source."""
        recs = self._sales.get((title, region), [])
        truths = self._true_variant.get((title, region), [])
        return [(r, v) for r, v in zip(recs, truths) if since <= r.sold_on <= until]

    def listings_between(self, start: date, end: date) -> list[ListingEvent]:
        return [e for e in self._listings if start <= e.listed_on <= end]

    def active_count(self, title: str, region: Region, as_of: date) -> int:
        """Listings live on `as_of`: posted, not yet sold."""
        n = sum(1 for e in self._listings
                if e.title == title and e.region is region
                and e.listed_on <= as_of
                and (e.sold_on is None or e.sold_on > as_of))
        return max(n, 1)


class PointInTime:
    """
    A CompSource frozen at `as_of`. Nothing after that date is visible.

    The guard is structural rather than advisory: every query is clipped
    here, so a caller cannot accidentally reach into the future even by
    passing a window that extends past the as-of date.
    """

    name = "point-in-time"

    def __init__(self, timeline: Timeline, as_of: date,
                 reference_lag_days: int = 30):
        self.timeline = timeline
        self.as_of = as_of
        # Reference sources publish stale figures. PriceCharting's number
        # today reflects sales from weeks ago, and pretending otherwise
        # imports a mild look-ahead of its own.
        self.reference_lag_days = reference_lag_days

    def sold_records(self, title: str, region: Region,
                     since: date) -> list[SoldRecord]:
        return self.timeline.sales(title, region, since, self.as_of)

    def active_listing_count(self, title: str, region: Region) -> int | None:
        return self.timeline.active_count(title, region, self.as_of)

    def quote(self, title: str, region: Region) -> dict[Completeness, CompQuote]:
        """Reference-style tier prices, computed from lagged history only."""
        cutoff = self.as_of - timedelta(days=self.reference_lag_days)
        window_start = cutoff - timedelta(days=365)
        recs = self.timeline.sales(title, region, window_start, cutoff)
        if len(recs) < 4:
            return {}
        out: dict[Completeness, CompQuote] = {}
        for tier in (Completeness.LOOSE, Completeness.CIB):
            tier_recs = [r for r in recs if r.completeness is tier]
            if len(tier_recs) < 3:
                continue
            prices = sorted(r.price for r in tier_recs)   # item price, not delivered
            med = prices[len(prices) // 2]
            out[tier] = CompQuote(tier, round(med, 2), n=len(tier_recs),
                                  as_of=cutoff, variant_split=False,
                                  source="point-in-time-ref",
                                  includes_shipping=False)
        return out
