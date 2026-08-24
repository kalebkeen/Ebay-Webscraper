"""
economics.py — Stage 3. Does this trade clear, and what may I pay for it?

Stage 2 says what a thing sells for. This module answers the only question
that matters: after every cost and every way it can go wrong, what is the
highest price at which buying it still beats leaving the money alone.

Three things this gets right that back-of-envelope reseller maths usually
does not:

  FEES COMPOUND ON THE WRONG BASE. eBay charges the final value fee on the
  total the buyer paid -- item, shipping, AND the sales tax eBay collected
  and remitted. That last part is invisible on your payout statement and
  adds roughly a percent to the effective rate.

  THE FIXED COSTS DOMINATE THE CHEAP END. Postage, supplies and the
  per-order fee are ~$6.60 regardless of price. On a $12 game that is 55%
  of revenue before the percentage fee touches it. Most of the PS2 catalog
  is structurally unbuyable for this reason, and no sourcing skill fixes it.

  CAPITAL HAS A CLOCK. A $40 profit in 9 months is worse than $12 in three
  weeks. Ranking on margin instead of profit-per-day-of-capital is how you
  end up with a spare room full of correctly-valued, slow inventory.

Every constant here is a lever you should calibrate against your own
realised sales. They are defaults, not measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

# --------------------------------------------------------------------------
# Fee model
# --------------------------------------------------------------------------

# eBay's standard 2026 category rate. Sources disagree on whether "Video
# Games & Consoles" sits here or in the higher media tier alongside Books,
# Movies and Music -- verify YOUR subcategory on eBay's fee page before
# trusting any number this module produces. `sensitivity()` shows how much
# the answer moves if you have this wrong.
STANDARD_FVF = 0.136
MEDIA_FVF = 0.153

# Average US sales-tax rate a buyer pays. Enters the model twice, and for
# opposite reasons: eBay's FVF is charged on a base that includes it, and
# you pay it yourself on the buy side unless you hold a resale certificate.
AVG_SALES_TAX = 0.07


@dataclass(frozen=True)
class FeeModel:
    fvf_rate: float = STANDARD_FVF
    per_order_low: float = 0.30        # orders of $10 or less
    per_order_high: float = 0.40       # orders above $10
    per_order_threshold: float = 10.00
    promoted_rate: float = 0.0         # 2-12% if you want visibility
    international_rate: float = 0.0    # +1.65% on cross-border sales
    inad_surcharge: float = 0.0        # +5% if your INAD rate is "Very High"
    buyer_tax_rate: float = AVG_SALES_TAX

    @property
    def effective_rate(self) -> float:
        """Percentage fees as a share of the delivered price.

        The FVF base includes buyer sales tax, so the rate you actually pay
        on your own revenue is higher than the headline number.
        """
        return ((self.fvf_rate + self.inad_surcharge + self.international_rate)
                * (1.0 + self.buyer_tax_rate)
                + self.promoted_rate)

    def per_order(self, delivered: float) -> float:
        return (self.per_order_low if delivered <= self.per_order_threshold
                else self.per_order_high)


@dataclass(frozen=True)
class OpsModel:
    """Costs and timings that are yours, not the platform's."""
    postage_out: float = 5.75          # USPS Ground Advantage; Media Mail
                                       # does NOT cover video games
    supplies: float = 0.45             # mailer, sleeve, label
    handling_days: float = 2.0         # payment to dropoff
    inbound_tax_rate: float = AVG_SALES_TAX
    resale_certificate: bool = False   # exempts inbound sales tax
    capital_floor: float = 0.0         # min cash you refuse to tie up below

    @property
    def fixed_sale_cost(self) -> float:
        return self.postage_out + self.supplies


@dataclass(frozen=True)
class RiskModel:
    """
    Probabilities that the trade does not go as modelled.

    Two failure channels, deliberately separate, because collapsing them into
    one "misgrade" rate with one recovery rate misprices both:

      COUNTERFEIT — a repro disc, a burned backup, a fake case. Near-total
        loss. You cannot honestly resell it, and a claim only sometimes
        succeeds. Recovery is a small fraction of what you paid.

      VARIANT ERROR — it's Greatest Hits, not black label; the manual is
        missing; the disc was resurfaced. The item is still real and still
        sells, just one rung down. Loss is the gap between the two prices,
        which scales with RESALE VALUE, not with what you paid.

    A single 60%-of-landed recovery rate is far too lenient for the first and
    slightly harsh on the second — and since repro risk concentrates on
    exactly the high-value titles this system is built to find, the lenient
    direction is the dangerous one.

    Defaults are deliberately pessimistic. Under-modelling risk empties a bank
    account; over-modelling it only costs you deals, and there are always
    more listings.
    """
    p_return: float = 0.08              # buyer returns it
    p_variant_error: float = 0.06       # real item, worth less than modelled
    p_counterfeit: float = 0.010        # not a genuine retail disc
    p_unsold: float = 0.10              # never clears at the modelled price
    return_postage: float = 5.75        # you eat this on an INAD
    variant_error_ratio: float = 0.55   # what it's really worth, vs modelled
    counterfeit_recovery: float = 0.15  # share of landed clawed back on a fake
    liquidation_ratio: float = 0.45     # recovery when dumped into a bulk lot
    p_disc_swap: float = 0.01           # returned item isn't the one you sent

    def scaled(self, repro_risk: str, comp_confidence: str,
               liquidity: str = "medium") -> "RiskModel":
        """Raise the risk floor for titles and quotes that deserve it.

        Repro risk drives the counterfeit channel; quote confidence drives
        the variant channel; liquidity drives the dead-stock channel. They
        are different failures with different causes, so they scale on
        different inputs.
        """
        cf, ve, ret = self.p_counterfeit, self.p_variant_error, self.p_return
        un = self.p_unsold
        if repro_risk == "high":
            cf *= 12.0
            ret *= 1.5
        elif repro_risk == "medium":
            cf *= 4.0
            ret *= 1.2
        if comp_confidence == "low":
            ve *= 2.0
            ret *= 1.2
        # Thin titles are where inventory dies. The comp layer already warns
        # that a thin quote is a hint rather than a price; this is the same
        # fact expressed in dollars.
        un *= {"high": 0.5, "medium": 1.0, "low": 2.0, "thin": 3.5}.get(liquidity, 1.0)
        return replace(
            self,
            p_counterfeit=min(cf, 0.35),
            p_variant_error=min(ve, 0.45),
            p_return=min(ret, 0.40),
            p_unsold=min(un, 0.50),
        )


# --------------------------------------------------------------------------
# Core arithmetic
# --------------------------------------------------------------------------

def net_proceeds(delivered: float, fees: FeeModel, ops: OpsModel) -> float:
    """What lands in your account after a sale at `delivered` dollars.

    `delivered` is the total the buyer pays. Whether you split it into item
    price plus shipping or bundle it as free shipping makes no difference:
    eBay charges the fee on the sum either way.
    """
    if delivered <= 0:
        return 0.0
    return (delivered
            - delivered * fees.effective_rate
            - fees.per_order(delivered)
            - ops.fixed_sale_cost)


def landed_cost(bid: float, ship_in: float, ops: OpsModel) -> float:
    """Total cash out to acquire the item."""
    subtotal = bid + ship_in
    if ops.resale_certificate:
        return subtotal
    return subtotal * (1.0 + ops.inbound_tax_rate)


def breakeven_delivered(fees: FeeModel, ops: OpsModel) -> float:
    """
    Lowest delivered price at which a FREE item still nets a cent.

    Below this, no sourcing skill helps: the fixed costs exceed revenue no
    matter what you paid. This is the hard floor of the dead zone.
    """
    fixed = ops.fixed_sale_cost + fees.per_order_high
    denom = 1.0 - fees.effective_rate
    return fixed / denom if denom > 0 else float("inf")


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------

def return_friction(delivered: float, fees: FeeModel, ops: OpsModel,
                    risk: RiskModel) -> float:
    """
    Cost of a return that you ultimately resell successfully.

    eBay credits the percentage fee on a full refund, so the damage is the
    postage both ways, the per-order fee, fresh supplies, and the small
    chance the disc that comes back is not the one that went out.
    """
    return (ops.postage_out
            + risk.return_postage
            + fees.per_order(delivered)
            + ops.supplies
            + risk.p_disc_swap * delivered)


def counterfeit_loss(landed: float, risk: RiskModel) -> float:
    """Loss when the disc is not genuine. Scales with what you PAID.

    Recovery is platform-dependent and the default already assumes
    marketplace buyer protection with an imperfect success rate. Cash-in-hand
    channels -- Facebook, car boot, estate sale -- should set
    `counterfeit_recovery=0.0`, which changes the maths on rare titles
    completely.
    """
    return landed * (1.0 - risk.counterfeit_recovery)


def variant_error_loss(delivered: float, fees: FeeModel, ops: OpsModel,
                       risk: RiskModel) -> float:
    """Loss when the item is genuine but worth less than modelled.

    Scales with RESALE VALUE, not with what you paid: you still sell it, you
    just sell it as the cheaper thing. Buying below market does not shrink
    this loss, which is why it has to be modelled separately from a fake.
    """
    good = net_proceeds(delivered, fees, ops)
    degraded = net_proceeds(delivered * risk.variant_error_ratio, fees, ops)
    return max(good - degraded, 0.0)


def unsold_loss(delivered: float, fees: FeeModel, ops: OpsModel,
                risk: RiskModel) -> float:
    """Loss when the item never clears at the modelled price.

    Not a total write-off: you eventually dump it into a bulk lot, take a
    local-pickup offer, or relist far below comp. Like the variant channel
    this scales with RESALE, not with what you paid -- buying cheaply does
    not make dead stock less dead, it only means you lose less overall.

    Omitting this channel is the most flattering assumption a reseller model
    can make, because the titles most likely to die are the thin, high-value
    ones the rest of the pipeline works hardest to surface.
    """
    good = net_proceeds(delivered, fees, ops)
    dumped = net_proceeds(delivered * risk.liquidation_ratio, fees, ops)
    return max(good - dumped, 0.0)


# --------------------------------------------------------------------------
# Deal evaluation
# --------------------------------------------------------------------------

@dataclass
class Deal:
    sku: str
    ask: float                     # listed price
    ship_in: float
    resale: float                  # delivered price we expect to achieve
    landed: float
    gross_net: float               # proceeds after fees, before risk
    gross_profit: float
    expected_profit: float         # after risk deductions
    expected_days: float
    profit_per_day: float
    roi: float                     # on landed cost
    annualised_roi: float
    max_bid: float                 # highest ask that still clears the hurdle
    take: bool
    reasons: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)

    def report(self) -> str:
        verdict = "TAKE" if self.take else "PASS"
        lines = [
            f"[{verdict}] {self.sku}",
            f"  ask ${self.ask:.2f} + ${self.ship_in:.2f} ship "
            f"-> landed ${self.landed:.2f}",
            f"  resale ${self.resale:.2f} delivered -> net ${self.gross_net:.2f}",
            f"  profit ${self.gross_profit:+.2f} gross / "
            f"${self.expected_profit:+.2f} risk-adjusted",
            f"  {self.expected_days:.0f} days  "
            f"${self.profit_per_day:+.3f}/day  "
            f"ROI {self.roi:+.0%}  annualised {self.annualised_roi:+.0%}",
            f"  max bid ${self.max_bid:.2f}",
        ]
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Hurdle:
    """What a trade has to beat to be worth doing."""
    min_profit: float = 8.00           # absolute dollars, per flip
    min_roi: float = 0.30              # on landed cost
    min_annualised_roi: float = 0.60   # capital has alternatives
    max_days: float = 180.0            # beyond this it isn't a flip
    max_capital: float = 400.00        # per-item concentration limit


def max_bid_for(resale: float, ship_in: float, fees: FeeModel, ops: OpsModel,
                risk: RiskModel, hurdle: Hurdle, days: float) -> float:
    """
    Invert the profit equation: the highest ask that still clears every
    hurdle. This is the number you actually act on -- it turns the whole
    pipeline into a single Best Offer figure.

    Closed form rather than a search, because the profit function is linear
    in the bid once the risk deductions are fixed.
    """
    # `max_days` is a GATE, not a price constraint. No purchase price makes a
    # 250-day flip into a 180-day one, so if the clock fails there is no bid,
    # and returning a positive number here is how a max_bid ends up failing
    # the very check it was solved against.
    expected_days = ops.handling_days + days * (1.0 + risk.p_return)
    if expected_days > hurdle.max_days:
        return 0.0

    net = net_proceeds(resale, fees, ops)

    # Risk costs that don't depend on the bid: the return cycle, the
    # variant-error haircut, and dead stock -- all three scale with resale
    # rather than with what we pay, so they stay constants in the inversion.
    net_after_risk = (net
                      - risk.p_return * return_friction(resale, fees, ops, risk)
                      - risk.p_variant_error
                      * variant_error_loss(resale, fees, ops, risk)
                      - risk.p_unsold * unsold_loss(resale, fees, ops, risk))

    tax = 1.0 if ops.resale_certificate else (1.0 + ops.inbound_tax_rate)

    # Counterfeit loss IS proportional to landed cost, so it enters as a
    # coefficient rather than a constant:
    #   profit = net_r - landed - p_cf * landed * (1 - recovery)
    #          = net_r - landed * (1 + p_cf * (1 - recovery))
    cf_coeff = 1.0 + risk.p_counterfeit * (1.0 - risk.counterfeit_recovery)

    candidates = []

    # 1. absolute profit floor
    candidates.append((net_after_risk - hurdle.min_profit) / cf_coeff)

    # 2. ROI on landed cost
    candidates.append(net_after_risk / (cf_coeff + hurdle.min_roi))

    # 3. annualised ROI, which tightens as days-to-sell grows. Must use the
    #    SAME clock as evaluate(), computed above.
    if expected_days > 0:
        required = hurdle.min_annualised_roi * (expected_days / 365.0)
        candidates.append(net_after_risk / (cf_coeff + required))

    # 4. concentration limit
    candidates.append(hurdle.max_capital)

    landed_max = min(candidates)
    bid = landed_max / tax - ship_in
    if bid <= 0:
        return 0.0
    # Floor to the cent rather than round. The closed form lands exactly ON
    # each hurdle, so rounding up produces a bid that fails its own strict
    # inequality by a float's width.
    return math.floor(bid * 100.0) / 100.0


def evaluate(
    *,
    sku: str,
    ask: float,
    ship_in: float,
    resale: float,
    days_to_sell: float | None,
    fees: FeeModel | None = None,
    ops: OpsModel | None = None,
    risk: RiskModel | None = None,
    hurdle: Hurdle | None = None,
) -> Deal:
    """Full economics for one candidate purchase."""
    fees = fees or FeeModel()
    ops = ops or OpsModel()
    risk = risk or RiskModel()
    hurdle = hurdle or Hurdle()

    days_sell = days_to_sell if days_to_sell is not None else 90.0
    landed = landed_cost(ask, ship_in, ops)
    net = net_proceeds(resale, fees, ops)
    gross_profit = net - landed

    # Must mirror max_bid_for() exactly. When these two drift apart, max_bid
    # returns a price that evaluate() then rejects -- the pipeline contradicts
    # itself and there is no error to notice. test_economics asserts they
    # agree at the boundary.
    ret_cost = return_friction(resale, fees, ops, risk)
    cf_cost = counterfeit_loss(landed, risk)
    ve_cost = variant_error_loss(resale, fees, ops, risk)
    un_cost = unsold_loss(resale, fees, ops, risk)
    expected_profit = (gross_profit
                       - risk.p_return * ret_cost
                       - risk.p_counterfeit * cf_cost
                       - risk.p_variant_error * ve_cost
                       - risk.p_unsold * un_cost)

    # A return doesn't destroy the item, it restarts the clock on it.
    expected_days = ops.handling_days + days_sell * (1.0 + risk.p_return)

    profit_per_day = expected_profit / expected_days if expected_days else 0.0
    roi = expected_profit / landed if landed > 0 else 0.0
    annualised = roi * (365.0 / expected_days) if expected_days else 0.0

    mb = max_bid_for(resale, ship_in, fees, ops, risk, hurdle, days_sell)

    reasons: list[str] = []
    if resale < breakeven_delivered(fees, ops):
        reasons.append(
            f"below structural floor: a free copy nets nothing under "
            f"${breakeven_delivered(fees, ops):.2f} delivered"
        )
    if expected_profit < hurdle.min_profit:
        reasons.append(f"profit ${expected_profit:.2f} < ${hurdle.min_profit:.2f} floor")
    if roi < hurdle.min_roi:
        reasons.append(f"ROI {roi:.0%} < {hurdle.min_roi:.0%}")
    if annualised < hurdle.min_annualised_roi:
        reasons.append(f"annualised {annualised:.0%} < {hurdle.min_annualised_roi:.0%}")
    if expected_days > hurdle.max_days:
        reasons.append(f"{expected_days:.0f} days to clear > {hurdle.max_days:.0f}")
    if landed > hurdle.max_capital:
        reasons.append(f"landed ${landed:.2f} exceeds ${hurdle.max_capital:.2f} cap")
    if ask > mb and mb > 0:
        reasons.append(f"ask ${ask:.2f} above max bid ${mb:.2f} — offer, don't buy")

    return Deal(
        sku=sku, ask=ask, ship_in=ship_in, resale=resale, landed=round(landed, 2),
        gross_net=round(net, 2), gross_profit=round(gross_profit, 2),
        expected_profit=round(expected_profit, 2),
        expected_days=round(expected_days, 1),
        profit_per_day=round(profit_per_day, 3),
        roi=roi, annualised_roi=annualised, max_bid=round(mb, 2),
        take=not reasons,
        reasons=reasons,
        breakdown={
            "fvf": round(resale * fees.effective_rate, 2),
            "per_order": fees.per_order(resale),
            "postage_out": ops.postage_out,
            "supplies": ops.supplies,
            "inbound_tax": round(landed - (ask + ship_in), 2),
            "exp_return_cost": round(risk.p_return * ret_cost, 2),
            "exp_counterfeit_cost": round(risk.p_counterfeit * cf_cost, 2),
            "exp_variant_error_cost": round(risk.p_variant_error * ve_cost, 2),
            "exp_unsold_cost": round(risk.p_unsold * un_cost, 2),
        },
    )


# --------------------------------------------------------------------------
# Throughput — per-flip economics are not a business
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Throughput:
    """
    Deal flow and labour. Converts "$28 per flip" into "$X/month at $Y/hour".

    This is the model that answers whether to build any of this. A healthy
    per-flip margin means nothing if the funnel only yields four buyable
    listings a week and each one costs forty minutes of handling.

    The defaults are deliberately generous to the idea: a 2% hit rate on
    reviewed listings and a 35% win rate are optimistic for a public
    marketplace where dozens of people run the same scan.
    """
    listings_scanned_per_week: int = 4000    # what a scraper can pull
    reviewable_rate: float = 0.05            # survive Stage 1 identification
    hit_rate: float = 0.02                   # of those, clear the hurdle
    win_rate: float = 0.35                   # of those, you actually get it
    minutes_per_review: float = 1.5          # human eyes on flagged photos
    minutes_per_purchase: float = 6.0        # offer, pay, track
    minutes_per_sale: float = 14.0           # photograph, list, pack, ship
    minutes_ops_per_week: float = 90.0       # maintenance, returns, admin
    reviews_per_hit: float = 12.0            # photo checks per buyable deal

    def project(self, profit_per_flip: float, days_to_sell: float,
                capital_per_flip: float) -> dict:
        weeks = 4.345
        candidates = (self.listings_scanned_per_week
                      * self.reviewable_rate * self.hit_rate)
        acquired_pw = candidates * self.win_rate
        acquired_pm = acquired_pw * weeks

        minutes_pw = (self.minutes_ops_per_week
                      + acquired_pw * self.reviews_per_hit * self.minutes_per_review
                      + acquired_pw * (self.minutes_per_purchase
                                       + self.minutes_per_sale))
        hours_pm = minutes_pw * weeks / 60.0

        monthly_profit = acquired_pm * profit_per_flip
        # Capital is recycled, not spent once: you need enough to cover
        # everything bought but not yet sold.
        in_flight = acquired_pm * (days_to_sell / 30.0)
        capital_required = in_flight * capital_per_flip

        return {
            "acquired_per_month": round(acquired_pm, 1),
            "hours_per_month": round(hours_pm, 1),
            "monthly_profit": round(monthly_profit, 2),
            "hourly_rate": round(monthly_profit / hours_pm, 2) if hours_pm else 0.0,
            "capital_required": round(capital_required, 2),
            "annual_return_on_capital": (
                round(monthly_profit * 12 / capital_required, 3)
                if capital_required else 0.0),
        }


def throughput_report(profit_per_flip: float, days_to_sell: float,
                      capital_per_flip: float,
                      scenarios: dict[str, Throughput] | None = None) -> str:
    scenarios = scenarios or {
        "optimistic": Throughput(hit_rate=0.030, win_rate=0.45),
        "base": Throughput(),
        "pessimistic": Throughput(hit_rate=0.010, win_rate=0.25,
                                  minutes_per_sale=20.0),
    }
    lines = [f"  assuming ${profit_per_flip:.2f}/flip, {days_to_sell:.0f} days, "
             f"${capital_per_flip:.2f} tied up each",
             f"  {'scenario':<13} {'flips/mo':>9} {'hrs/mo':>7} "
             f"{'profit/mo':>10} {'$/hour':>8} {'capital':>9} {'ROC/yr':>8}"]
    for name, t in scenarios.items():
        r = t.project(profit_per_flip, days_to_sell, capital_per_flip)
        lines.append(
            f"  {name:<13} {r['acquired_per_month']:9.1f} "
            f"{r['hours_per_month']:7.1f} {r['monthly_profit']:10.2f} "
            f"{r['hourly_rate']:8.2f} {r['capital_required']:9.0f} "
            f"{r['annual_return_on_capital']:7.0%}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

def sensitivity(resale: float, ask: float, ship_in: float = 4.00) -> str:
    """How much does the answer move on assumptions you aren't sure of?"""
    rows = []
    base = dict(sku="x", ask=ask, ship_in=ship_in, resale=resale, days_to_sell=60)
    variants = [
        ("baseline", FeeModel(), OpsModel(), RiskModel()),
        ("media fee tier 15.3%", FeeModel(fvf_rate=MEDIA_FVF), OpsModel(), RiskModel()),
        ("promoted 5%", FeeModel(promoted_rate=0.05), OpsModel(), RiskModel()),
        ("resale certificate", FeeModel(), OpsModel(resale_certificate=True), RiskModel()),
        ("high repro risk", FeeModel(), OpsModel(),
         RiskModel().scaled("high", "low")),
    ]
    for label, f, o, r in variants:
        d = evaluate(**base, fees=f, ops=o, risk=r)
        rows.append(f"  {label:<24} profit ${d.expected_profit:+7.2f}   "
                    f"max bid ${d.max_bid:6.2f}")
    return "\n".join(rows)


def dead_zone_table(fees: FeeModel | None = None, ops: OpsModel | None = None,
                    hurdle: Hurdle | None = None,
                    buy_ratio: float = 0.50, days: float = 60.0) -> str:
    """
    At what resale price does this business start working?

    Assumes you can source at `buy_ratio` of delivered resale, which is
    already an optimistic sourcing assumption for a public marketplace.
    """
    fees = fees or FeeModel()
    ops = ops or OpsModel()
    hurdle = hurdle or Hurdle()
    lines = [f"  {'resale':>8} {'buy':>8} {'landed':>8} {'net':>8} "
             f"{'profit':>8} {'ROI':>7} {'/day':>7}  verdict"]
    for resale in (8, 10, 12, 15, 20, 25, 30, 40, 60, 90, 150, 250, 400):
        ask = resale * buy_ratio
        d = evaluate(sku="", ask=ask, ship_in=0.0, resale=float(resale),
                     days_to_sell=days, fees=fees, ops=ops, hurdle=hurdle)
        lines.append(
            f"  {resale:8.0f} {ask:8.2f} {d.landed:8.2f} {d.gross_net:8.2f} "
            f"{d.expected_profit:8.2f} {d.roi:6.0%} {d.profit_per_day:7.3f}  "
            f"{'TAKE' if d.take else 'pass'}"
        )
    return "\n".join(lines)
