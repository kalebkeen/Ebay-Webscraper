"""
comps.py — Stage 2. Turn a resolved SKU into a defensible resale estimate.

The hard part is not fetching prices. It is that Stage 1 produces a SKU at
a granularity no comp source actually sells:

    title | region | variant | completeness

PriceCharting keys on (console, title) and reports three tiers — loose, CIB,
new. It has no disc+case tier, and for most PS2 titles it does not separate
black label from Greatest Hits. eBay sold listings have the granularity but
arrive as unlabelled free text, which is what Stage 1 exists to fix.

So this module does three things:

  1. ADAPT. Map a fine SKU onto whatever tiers the source exposes, and be
     explicit about every assumption injected on the way. Each adjustment
     appends to an audit trail, because a price you cannot explain is a
     price you cannot defend when the flip loses money.

  2. ESTIMATE ROBUSTLY. Sold-comp data is contaminated — miscategorised
     lots, graded copies, international sales with inflated shipping,
     accepted best offers recorded at list. A mean is worthless here. We
     use recency-weighted robust statistics on log prices and report a
     distribution, never a single number.

  3. REFUSE. Below a minimum effective sample size there is no estimate,
     only a guess with a decimal point. `quotable` gates this, and the
     backtest in Stage 4 will show that refusing is usually correct.

Nothing here should be trusted until calibrated against your own realised
sales. The two structural constants — MANUAL_PREMIUM_SHARE and
GH_PRICE_RATIO — are priors, flagged as such, and are the first things a
backtest should overwrite.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Protocol, Sequence


from listing_parser import Completeness, Region, Variant


# ---------------------------------------------------------------------------
# Calibration constants — PRIORS, not measurements
# ---------------------------------------------------------------------------

# Of the gap between a loose disc and a full CIB copy, how much is the
# manual rather than the case? Cases are near-worthless and replaceable;
# manuals are the scarce component. A disc+case copy therefore sits nearer
# the loose end than the midpoint.
#   disc_case = loose + (1 - MANUAL_PREMIUM_SHARE) * (cib - loose)
MANUAL_PREMIUM_SHARE = 0.65

# Greatest Hits copies sell for roughly this fraction of a black label copy
# of the same game in the same condition. Varies enormously by title — some
# collectors pay a large premium for first prints, some do not care at all.
GH_PRICE_RATIO = 0.55

# What share of surviving copies of a Greatest-Hits-era title are the budget
# reprint? Reprints ran longer and sold more, so they dominate the pool.
# Used to de-mix an aggregate quote into per-variant prices.
GH_POPULATION_SHARE = 0.60

# Half-life for recency weighting of sold comps, in days. Retro prices drift
# but do not whipsaw; six months is a reasonable default.
RECENCY_HALF_LIFE_DAYS = 180.0

# Typical delivered-cost uplift for a single PS2 game, USD.
#
# Sold-comp totals are what the buyer paid (item + shipping); reference
# sources like PriceCharting publish item price only. Comparing them
# directly makes every cheap title look 50%+ mispriced -- on a $10 game the
# postage IS the discrepancy. Everything in this module is normalised to
# DELIVERED cost, because that is also the base eBay charges fees on.
#
# Media Mail does not cover video games, so this is USPS Ground Advantage.
TYPICAL_SHIPPING = 5.50

# Outlier rejection width, in scaled-MAD units, applied to log prices.
OUTLIER_K = 3.0

# A p75/p25 above this means the sample is a mixture (variants, completeness,
# contamination) the filters could not split -- the median is not one market
# price. Such a SKU is priced a tier more cautiously, and above the value floor
# is flagged for manual verification rather than quoted like a known price.
SPREAD_WIDE = 1.8
VERIFY_VALUE_FLOOR = 60.0      # dollars; below this a wide spread is low-stakes


class Confidence(str, Enum):
    NONE = "none"       # do not quote
    LOW = "low"         # quote, but only a human should act on it
    MEDIUM = "medium"
    HIGH = "high"


# Conservative quantile to price against, by confidence. Thin data means
# bidding against a pessimistic quantile, not a wider warning label.
CONSERVATIVE_QUANTILE = {
    Confidence.HIGH: 0.40,
    Confidence.MEDIUM: 0.33,
    Confidence.LOW: 0.25,
    Confidence.NONE: 0.20,
}

# One-tier confidence drop for a wide-spread SKU. Floored at LOW: still
# quotable (a human can act on it), just not asserted as a confident price.
_SPREAD_DOWNGRADE = {
    Confidence.HIGH: Confidence.MEDIUM,
    Confidence.MEDIUM: Confidence.LOW,
    Confidence.LOW: Confidence.LOW,
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SoldRecord:
    """One observed completed sale."""
    price: float             # item price, excluding shipping
    shipping: float          # what the buyer paid to receive it
    sold_on: date
    completeness: Completeness
    variant: Variant = Variant.UNKNOWN
    region: Region = Region.NTSC_U
    note: str = ""

    @property
    def total(self) -> float:
        """What the buyer actually paid. eBay charges fees on this."""
        return self.price + self.shipping


@dataclass
class CompQuote:
    """What a source knows about one tier of one product."""
    tier: Completeness
    price: float
    n: int = 0
    as_of: date | None = None
    variant_split: bool = False   # True if this price is variant-specific
    source: str = "unknown"
    includes_shipping: bool = False   # False for PriceCharting-style item prices


@dataclass
class Valuation:
    sku: str
    expected_resale: float          # recency-weighted robust centre
    conservative_resale: float      # what Stage 3 should actually bid against
    p25: float
    p75: float
    n_effective: float
    confidence: Confidence
    monthly_drift: float | None     # fractional price change per month
    sales_per_month: float | None
    active_listings: int | None
    est_days_to_sell: float | None
    adjustments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # A high-value SKU whose price spread is too wide to trust as a single
    # number: quotable, but a human must confirm the exact copy before paying.
    needs_verify: bool = False

    @property
    def quotable(self) -> bool:
        return self.confidence is not Confidence.NONE

    @property
    def spread_ratio(self) -> float:
        """p75/p25. Above ~2.0 the 'market price' is a fiction."""
        return self.p75 / self.p25 if self.p25 > 0 else float("inf")

    def report(self) -> str:
        lines = [
            f"{self.sku}",
            f"  expected     ${self.expected_resale:7.2f}    "
            f"conservative ${self.conservative_resale:7.2f}",
            f"  p25-p75      ${self.p25:7.2f} - ${self.p75:.2f}  "
            f"(spread {self.spread_ratio:.2f}x)",
            f"  n_effective  {self.n_effective:5.1f}         "
            f"confidence   {self.confidence.value}",
        ]
        if self.est_days_to_sell is not None:
            lines.append(
                f"  velocity     {self.sales_per_month:.1f}/mo, "
                f"{self.active_listings} listed -> "
                f"~{self.est_days_to_sell:.0f} days to sell"
            )
        if self.monthly_drift is not None:
            lines.append(f"  trend        {self.monthly_drift * 100:+.1f}%/month")
        for a in self.adjustments:
            lines.append(f"  adj  {a}")
        for w in self.warnings:
            lines.append(f"  WARN {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------

def weighted_quantile(values: Sequence[float], weights: Sequence[float],
                      q: float) -> float:
    """Weighted quantile with midpoint interpolation.

    Pure stdlib. numpy is a native wheel, and this whole module has to run
    inside an APK where every C extension is a build risk; the arrays here
    are a few hundred elements, so vectorising bought nothing anyway.

    Midpoint (rather than lower or upper) interpolation keeps the estimate
    stable as recency weights decay smoothly.
    """
    pairs = sorted(zip(values, weights))
    if not pairs:
        return float("nan")
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(statistics.median([v for v, _ in pairs]))

    # Cumulative weight at each point's midpoint, normalised to 0..1.
    cum, xs, ys = 0.0, [], []
    for v, w in pairs:
        cum += w
        xs.append((cum - 0.5 * w) / total)
        ys.append(v)

    if q <= xs[0]:
        return float(ys[0])
    if q >= xs[-1]:
        return float(ys[-1])
    for i in range(1, len(xs)):
        if q <= xs[i]:
            span = xs[i] - xs[i - 1]
            if span <= 0:
                return float(ys[i])
            t = (q - xs[i - 1]) / span
            return float(ys[i - 1] + t * (ys[i] - ys[i - 1]))
    return float(ys[-1])


def mad_filter(log_prices: Sequence[float], k: float = OUTLIER_K) -> list[bool]:
    """Keep-mask via median absolute deviation on log prices.

    Log space because price contamination is multiplicative: a graded copy
    at 8x and a miscategorised lot at 0.15x are symmetric errors there and
    wildly asymmetric in dollars. Standard deviation would let the 8x point
    drag the threshold far enough to keep itself.
    """
    n = len(log_prices)
    if n < 4:
        return [True] * n
    med = statistics.median(log_prices)
    mad = statistics.median([abs(x - med) for x in log_prices])
    if mad <= 0:
        return [True] * n
    scaled = 1.4826 * mad          # consistent with sigma under normality
    return [abs(x - med) <= k * scaled for x in log_prices]


def recency_weights(sold_dates: Sequence[date], today: date,
                    half_life: float = RECENCY_HALF_LIFE_DAYS) -> list[float]:
    return [0.5 ** (max((today - d).days, 0) / half_life) for d in sold_dates]


def log_price_trend(prices: Sequence[float], sold_dates: Sequence[date],
                    today: date) -> float | None:
    """Fractional price drift per 30 days, via OLS on log price vs age.

    Returns None below six points, where the slope is noise with a sign.
    """
    if len(prices) < 6:
        return None
    ages = [float(-(today - d).days) for d in sold_dates]
    if statistics.pstdev(ages) < 1.0:
        return None
    ys = [math.log(p) for p in prices]

    n = len(ages)
    mx = sum(ages) / n
    my = sum(ys) / n
    sxx = sum((a - mx) ** 2 for a in ages)
    if sxx <= 0:
        return None
    sxy = sum((a - mx) * (y - my) for a, y in zip(ages, ys))
    slope = sxy / sxx                      # log-dollars per day
    return float(math.exp(slope * 30.0) - 1.0)


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------

class CompSource(Protocol):
    """Anything that can answer 'what does this sell for'.

    Two shapes, because the two useful sources differ fundamentally:
    reference sources publish a tier price; marketplaces expose individual
    completed sales. Implementations provide whichever they have.
    """

    name: str

    def quote(self, title: str, region: Region) -> dict[Completeness, CompQuote]:
        """Tier prices, e.g. PriceCharting. Empty dict if unsupported."""
        ...

    def sold_records(self, title: str, region: Region,
                     since: date) -> list[SoldRecord]:
        """Individual completed sales. Empty list if unsupported."""
        ...

    def active_listing_count(self, title: str, region: Region) -> int | None:
        """Current supply, for sell-through. None if unsupported."""
        ...


# ---------------------------------------------------------------------------
# The adapter: fine SKU -> coarse source
# ---------------------------------------------------------------------------

def interpolate_disc_case(loose: float, cib: float) -> float:
    """Price a disc+case copy, which no reference source publishes.

    Sits between loose and CIB, nearer loose, because the case is the cheap
    half of the gap and the manual the expensive half.
    """
    return loose + (1.0 - MANUAL_PREMIUM_SHARE) * max(cib - loose, 0.0)


def demix_variant(aggregate: float, variant: Variant,
                  has_budget_reprint: bool) -> tuple[float, str | None]:
    """Split an all-variants aggregate price into a per-variant price.

    A source that does not separate black label from Greatest Hits is
    reporting a population mixture:

        aggregate = w_bl * P_bl + w_gh * P_gh,   P_gh = r * P_bl

    so  P_bl = aggregate / (w_bl + w_gh * r)  and  P_gh = r * P_bl.

    Deriving both from the mixture keeps them consistent. Applying a naive
    0.55 multiplier to the aggregate to get Greatest Hits would double-count
    the discount, because the aggregate already contains mostly GH sales.
    """
    if not has_budget_reprint:
        return aggregate, None
    if variant not in (Variant.BLACK_LABEL, Variant.GREATEST_HITS):
        return aggregate, None

    w_gh = GH_POPULATION_SHARE
    w_bl = 1.0 - w_gh
    denom = w_bl + w_gh * GH_PRICE_RATIO
    p_bl = aggregate / denom

    if variant is Variant.BLACK_LABEL:
        return p_bl, (f"de-mixed aggregate to black label "
                      f"(x{1 / denom:.2f}, assumes {w_gh:.0%} of pool is GH)")
    p_gh = GH_PRICE_RATIO * p_bl
    return p_gh, (f"de-mixed aggregate to Greatest Hits "
                  f"(x{p_gh / aggregate:.2f})")


def tier_price(quotes: dict[Completeness, CompQuote],
               want: Completeness) -> tuple[float | None, str | None]:
    """Pull the requested tier out of a reference quote, interpolating
    disc+case and falling back conservatively when a tier is absent."""
    if want in quotes:
        return quotes[want].price, None

    loose = quotes.get(Completeness.LOOSE)
    cib = quotes.get(Completeness.CIB)

    if want is Completeness.DISC_CASE and loose and cib:
        p = interpolate_disc_case(loose.price, cib.price)
        return p, (f"disc+case interpolated between loose ${loose.price:.2f} "
                   f"and CIB ${cib.price:.2f}")

    if want is Completeness.SEALED:
        # Sealed is its own market with its own fraud profile. Never
        # extrapolate it from used prices.
        return None, "no sealed comp — sealed is not extrapolable from used"

    if want is Completeness.NO_DISC:
        # Case and/or manual with no game in it. The fallback below would
        # price this as a LOOSE DISC -- the one component it definitionally
        # does not have -- returning ~78% of CIB for an empty case. Packaging
        # sells for a few dollars regardless of how valuable the game is, and
        # there is no comp source for it, so refuse rather than guess.
        return None, ("no disc — packaging alone has no usable comp and is "
                      "worth a few dollars at most")

    if loose:
        return loose.price, f"tier {want.value} unavailable, fell back to loose"
    return None, f"no comp for tier {want.value}"


# ---------------------------------------------------------------------------
# Filtering sold records down to the SKU we care about
# ---------------------------------------------------------------------------

def filter_records(records: Sequence[SoldRecord], want_completeness: Completeness,
                   want_variant: Variant,
                   want_region: Region) -> tuple[list[SoldRecord], list[str], int, int]:
    """Keep only sales of the same thing. Report what was dropped.

    Also returns (n_in_region, n_variant_labelled):

      n_in_region        — the denominator for velocity. Active-listing
                           counts are title+region scoped, so dividing a
                           completeness-and-variant-filtered sale count by a
                           whole-title supply count mixes scopes and makes
                           every liquid title look like dead stock.
      n_variant_labelled — how many kept sales actually state the variant we
                           want, as opposed to being unlabelled and therefore
                           a population mixture.
    """
    notes: list[str] = []
    kept = list(records)

    before = len(kept)
    kept = [r for r in kept if r.region is want_region
            or r.region is Region.UNKNOWN]
    if len(kept) < before:
        notes.append(f"dropped {before - len(kept)} out-of-region sales")
    n_in_region = len(kept)

    before = len(kept)
    kept = [r for r in kept if r.completeness is want_completeness]
    if len(kept) < before:
        notes.append(f"dropped {before - len(kept)} other-completeness sales")

    # Variant filtering is lenient: most sold listings do not state a
    # variant, and discarding every UNKNOWN would empty the sample.
    n_labelled = 0
    if want_variant in (Variant.BLACK_LABEL, Variant.GREATEST_HITS):
        before = len(kept)
        kept = [r for r in kept
                if r.variant is want_variant or r.variant is Variant.UNKNOWN]
        if len(kept) < before:
            notes.append(f"dropped {before - len(kept)} other-variant sales")
        n_labelled = sum(1 for r in kept if r.variant is want_variant)
        unlabelled = len(kept) - n_labelled
        if unlabelled > 0.5 * max(len(kept), 1):
            notes.append(
                f"{unlabelled}/{len(kept)} comps do not state a variant — "
                "sample is a variant mixture"
            )
    else:
        n_labelled = len(kept)
    return kept, notes, n_in_region, n_labelled


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def value_sku(
    *,
    title: str,
    region: Region,
    variant: Variant,
    completeness: Completeness,
    source: CompSource,
    has_budget_reprint: bool,
    today: date | None = None,
    lookback_days: int = 365,
) -> Valuation:
    """Estimate resale value for one fully-resolved SKU."""
    today = today or date.today()
    since = today - timedelta(days=lookback_days)
    sku = f"{title}|{region.value}|{variant.value}|{completeness.value}"

    adjustments: list[str] = []
    warnings: list[str] = []

    # --- individual sold comps are the primary signal ---------------------
    records = source.sold_records(title, region, since)
    kept, notes, n_in_region, n_labelled = filter_records(
        records, completeness, variant, region)
    adjustments.extend(notes)

    # The kept sample is usually a variant MIXTURE: a few listings that state
    # the variant, plus a majority that say nothing. Its median therefore sits
    # between the black-label and Greatest Hits prices, which overstates a GH
    # SKU and understates a black-label one. Two ways out, in order:
    #
    #   1. If enough sales actually state our variant, use only those. Clean
    #      signal, smaller n -- usually the right trade.
    #   2. Otherwise keep the mixture and de-mix the centre afterwards with
    #      the same population model applied to reference prices, so both
    #      paths make the same assumption instead of contradicting each other.
    MIN_LABELLED = 6
    demix_sold = False
    if variant in (Variant.BLACK_LABEL, Variant.GREATEST_HITS):
        if n_labelled >= MIN_LABELLED:
            kept = [r for r in kept if r.variant is variant]
            adjustments.append(
                f"used {len(kept)} variant-labelled sales only "
                f"(clean {variant.value} signal)"
            )
        elif has_budget_reprint:
            demix_sold = True

    prices = [r.total for r in kept]
    dates = [r.sold_on for r in kept]

    n_eff = 0.0
    centre = p25 = p75 = float("nan")
    drift = None
    demix_factor = 1.0

    if prices:
        keep = mad_filter([math.log(p) for p in prices])
        dropped = sum(1 for k in keep if not k)
        if dropped:
            adjustments.append(f"MAD filter removed {dropped} outlier sale(s)")
        prices = [v for v, k in zip(prices, keep) if k]
        dates = [d for d, k in zip(dates, keep) if k]

    if prices:
        w = recency_weights(dates, today)
        n_eff = float(sum(w))
        centre = weighted_quantile(prices, w, 0.50)
        p25 = weighted_quantile(prices, w, 0.25)
        p75 = weighted_quantile(prices, w, 0.75)
        drift = log_price_trend(prices, dates, today)

        if demix_sold:
            # De-mix by taking the QUANTILE the wanted variant occupies in
            # the mixture, not by rescaling the mixture median.
            #
            # demix_variant() inverts  agg = w_bl*P_bl + w_gh*P_gh,  which is
            # an identity about MEANS. Applying it to a weighted median of a
            # bimodal sample is simply the wrong operation: the median of a
            # 60/40 mixture sits near the dominant mode, not at the weighted
            # average, so the result carried a systematic error of roughly
            # -8% that barely responded to the true ratio. Stage 4's planted-
            # bias test is what surfaced this.
            #
            # If Greatest Hits copies are the cheaper w_gh share of the pool,
            # they occupy the bottom w_gh of the sorted mixture, so their own
            # median is the (w_gh / 2) quantile of the whole. Black label
            # sits above them at w_gh + w_bl/2. Approximate where the two
            # distributions overlap, but unbiased where they separate --
            # and, unlike the mean formula, it moves correctly with the
            # true ratio.
            w_gh = GH_POPULATION_SHARE
            if variant is Variant.GREATEST_HITS:
                q_lo, q_mid, q_hi = 0.0, w_gh / 2.0, w_gh
            else:
                q_lo, q_mid, q_hi = w_gh, w_gh + (1.0 - w_gh) / 2.0, 1.0

            scaled = weighted_quantile(prices, w, q_mid)
            if scaled > 0 and centre > 0:
                demix_factor = scaled / centre
                centre = scaled
                # Spread within the component, not across the whole mixture.
                p25 = weighted_quantile(prices, w, q_lo + 0.25 * (q_hi - q_lo))
                p75 = weighted_quantile(prices, w, q_lo + 0.75 * (q_hi - q_lo))
                adjustments.append(
                    f"sold sample is a variant mixture; took the "
                    f"{q_mid:.0%} quantile as the {variant.value} component "
                    f"(assumes {w_gh:.0%} of the pool is the budget reprint)"
                )

    # --- reference source as fallback or cross-check ----------------------
    quotes = source.quote(title, region)
    ref_price = None
    if quotes:
        raw, note = tier_price(quotes, completeness)
        if note:
            adjustments.append(note)
        if raw is not None:
            quote_obj = quotes.get(completeness) or next(iter(quotes.values()))
            if quote_obj.variant_split:
                ref_price = raw
            else:
                ref_price, dnote = demix_variant(raw, variant, has_budget_reprint)
                if dnote:
                    adjustments.append(dnote)
            # Normalise to delivered cost. Sold comps are what the buyer
            # paid; reference tiers are item price. Skipping this makes the
            # cross-check below fire on every sub-$20 title, where postage
            # alone is a third of the delivered price.
            if not quote_obj.includes_shipping:
                ref_price += TYPICAL_SHIPPING
                adjustments.append(
                    f"reference price is item-only; added ${TYPICAL_SHIPPING:.2f} "
                    "shipping to compare on delivered cost"
                )

    if n_eff < 3.0 and ref_price is not None:
        adjustments.append(
            f"thin sold data (n_eff={n_eff:.1f}) — using reference price "
            f"${ref_price:.2f} as the centre"
        )
        centre = ref_price
        # A reference tier price is a point estimate with no dispersion.
        # Inventing a narrow band around it would be the most dangerous
        # thing this module could do, so assume a wide one.
        p25, p75 = ref_price * 0.70, ref_price * 1.35
    elif ref_price is not None and n_eff >= 3.0:
        gap = abs(centre - ref_price) / max(ref_price, 1e-6)
        if gap > 0.35:
            warnings.append(
                f"sold comps (${centre:.2f}) and reference (${ref_price:.2f}) "
                f"disagree by {gap:.0%} — check for a variant or region mix-up"
            )

    if not math.isfinite(centre):
        return Valuation(
            sku=sku, expected_resale=0.0, conservative_resale=0.0,
            p25=0.0, p75=0.0, n_effective=0.0, confidence=Confidence.NONE,
            monthly_drift=None, sales_per_month=None, active_listings=None,
            est_days_to_sell=None, adjustments=adjustments,
            # Prefer the SPECIFIC reason the tier refused (sealed is a
            # different market; packaging alone has no comp) over the generic
            # message, which reads like a database gap rather than a
            # deliberate refusal and invites the user to retry forever.
            warnings=warnings + ([a for a in adjustments
                                  if "no sealed comp" in a or "no disc —" in a]
                                 or ["no usable comp data"]),
        )

    # --- confidence -------------------------------------------------------
    if n_eff >= 15:
        conf = Confidence.HIGH
    elif n_eff >= 7:
        conf = Confidence.MEDIUM
    elif n_eff >= 3:
        conf = Confidence.LOW
    else:
        conf = Confidence.NONE if ref_price is None else Confidence.LOW

    # --- wide-spread guard ------------------------------------------------
    # A wide p75/p25 means the median is not a real market price. Pull the
    # pricing confidence down a tier (so `conservative` below bids against a
    # lower quantile) and, above the value floor, flag for manual verification.
    # Only for a REAL distribution (>=4 sold): the reference-only fallback uses
    # a synthetic 0.70-1.35 band that would trip this on every thin title.
    spread_ratio = (p75 / p25) if p25 > 0 else float("inf")
    needs_verify = False
    if len(prices) >= 4 and spread_ratio > SPREAD_WIDE:
        lowered = _SPREAD_DOWNGRADE.get(conf, conf)
        note = (f"prices disagree {spread_ratio:.1f}x — mixed variants/conditions, "
                "not one market price")
        if lowered is not conf:
            note += f"; confidence lowered to {lowered.value}"
            conf = lowered
        warnings.append(note + ". Verify the exact copy before paying.")
        if centre >= VERIFY_VALUE_FLOOR:
            needs_verify = True
            warnings.append("High value + wide spread — do not bid on this "
                            "number alone; confirm the variant and completeness.")

    # The conservative quantile has to ride the same de-mix scaling as the
    # centre, or it lands on a different variant's price scale entirely and
    # silently understates every black-label SKU.
    conservative = (
        weighted_quantile(prices, recency_weights(dates, today),
                          CONSERVATIVE_QUANTILE[conf]) * demix_factor
        if len(prices) >= 4
        else centre * (0.75 if conf is Confidence.LOW else 0.85)
    )
    # Invariant: never quote a "conservative" number above the centre.
    conservative = min(conservative, centre)

    # --- velocity ---------------------------------------------------------
    sales_per_month = None
    days_to_sell = None
    active = source.active_listing_count(title, region)
    active_sku = active
    if kept:
        span_days = max((today - min(r.sold_on for r in kept)).days, 30)
        sales_per_month = len(kept) * 30.0 / span_days
        if active is not None and sales_per_month > 0:
            # Active counts are title+region scoped; our sale count is
            # filtered down to one completeness and variant. Scale the supply
            # by the same share, or the ratio compares a slice of demand
            # against the whole shelf and every liquid title reads as dead
            # stock. (Assumes the active mix resembles the sold mix, which is
            # itself optimistic: slow-moving variants pile up in the active
            # pool. Erring that way is fine -- it overstates days-to-sell.)
            share = len(kept) / max(n_in_region, 1)
            active_sku = max(active * share, 1.0)
            # Classic sell-through: you are one listing in a queue of
            # `active_sku`, clearing at `sales_per_month`.
            days_to_sell = 30.0 * (active_sku + 1) / sales_per_month

    # --- warnings ---------------------------------------------------------
    # (Wide-spread is handled above, where it also lowers confidence.)
    if drift is not None and drift < -0.04:
        warnings.append(f"price falling {abs(drift) * 100:.0f}%/month")
    if days_to_sell is not None and days_to_sell > 120:
        warnings.append(
            f"~{days_to_sell:.0f} days to sell — capital locked for months"
        )
    if completeness is Completeness.SEALED:
        warnings.append("sealed market: verify seal, resealing is common")

    return Valuation(
        sku=sku,
        expected_resale=round(float(centre), 2),
        conservative_resale=round(float(conservative), 2),
        p25=round(float(p25), 2),
        p75=round(float(p75), 2),
        n_effective=round(n_eff, 2),
        confidence=conf,
        monthly_drift=drift,
        sales_per_month=sales_per_month,
        active_listings=int(active_sku) if active_sku is not None else None,
        est_days_to_sell=days_to_sell,
        adjustments=adjustments,
        warnings=warnings,
        needs_verify=needs_verify,
    )
