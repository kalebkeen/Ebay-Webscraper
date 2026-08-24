"""
decide.py — the full pipeline, end to end.

    raw listing -> Stage 1 identify -> Stage 2 value -> Stage 3 price -> rank

Also answers the question that should be asked before writing a scraper at
all: given real fee structures, how much of the PS2 catalog can this
business model touch? `viability_scan()` is that answer, and it is not
encouraging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import catalog
import comps
import economics as ec
import mock_sources as ms
import pipeline
from comps import Confidence
from listing_parser import Completeness as C, Region as R, Variant as V

TODAY = date(2026, 8, 22)


@dataclass
class Candidate:
    """One listing, all the way through."""
    raw_title: str
    ask: float
    ship_in: float
    target: pipeline.PricingTarget
    valuation: comps.Valuation | None
    deal: ec.Deal | None
    blocked_at: str | None = None      # which stage stopped it, if any

    @property
    def score(self) -> float:
        """Ranking key: dollars of expected profit per day of tied-up capital."""
        return self.deal.profit_per_day if self.deal else float("-inf")

    def line(self) -> str:
        if self.blocked_at:
            return (f"  {'—':>7}  {self.raw_title[:46]:<48} "
                    f"blocked at {self.blocked_at}")
        d = self.deal
        head = (f"  {d.profit_per_day:7.3f}  {self.raw_title[:46]:<48} "
                f"ask ${d.ask:.2f} -> resale ${d.resale:.2f}  "
                f"profit ${d.expected_profit:+.2f}  "
                f"max bid ${d.max_bid:.2f}  "
                f"{'TAKE' if d.take else 'pass'}")
        if d.take:
            return head
        # A pass sitting next to a large positive profit reads as a bug unless
        # the binding constraint is named. Usually it's the clock: no purchase
        # price converts a 190-day flip into a 180-day one, which is why
        # max_bid is $0.00 on trades that look richly profitable.
        return head + f"\n{'':11}└─ {d.reasons[0]}"


def _entry(title: str):
    return next((t for t in catalog.CATALOG if t.canonical == title), None)


# Reasons are accumulated in the order they are discovered, which is not the
# order of importance. "variant unstated" is an informational note that
# accompanies almost every listing; "no catalog match" is the thing that
# actually stopped us. Reporting the first item in the list told users the
# variant was unclear when the truth was that we had no idea what the game
# was — a misleading answer to the only question that mattered.
_REASON_PRIORITY = (
    "no catalog match",
    "ambiguous",
    "contested identity",
    "weak title match",
    "blocking:",
    "major:",
    "repro",
)


def _blocking_reason(target) -> str:
    """The reason that actually stopped this listing, not the first noted."""
    reasons = list(target.reasons or [])
    if not reasons:
        return "rejected"
    for probe in _REASON_PRIORITY:
        for reason in reasons:
            if probe in reason.lower():
                return reason
    return reasons[0]


def assess(
    raw_title: str,
    description: str,
    ask: float,
    ship_in: float,
    source,
    *,
    fees: ec.FeeModel | None = None,
    ops: ec.OpsModel | None = None,
    hurdle: ec.Hurdle | None = None,
) -> Candidate:
    """Run one listing through all three stages."""
    target = pipeline.resolve(raw_title, description)

    if target.verdict.value == "reject":
        return Candidate(raw_title, ask, ship_in, target, None, None,
                         blocked_at=f"stage1 ({_blocking_reason(target)})")
    if target.title is None:
        return Candidate(raw_title, ask, ship_in, target, None, None,
                         blocked_at="stage1 (unidentified)")

    entry = _entry(target.title)
    region = target.region if target.region is not R.UNKNOWN else R.NTSC_U

    val = comps.value_sku(
        title=target.title, region=region, variant=target.variant,
        completeness=target.completeness, source=source,
        has_budget_reprint=bool(entry.has_greatest_hits) if entry else True,
        today=TODAY,
    )
    if not val.quotable:
        return Candidate(raw_title, ask, ship_in, target, val, None,
                         blocked_at="stage2 (no usable comps)")

    # Risk scales with how much we actually know. A thin quote on a
    # high-repro-risk title is the exact profile of a trade that looks
    # brilliant and isn't.
    risk = ec.RiskModel().scaled(
        repro_risk=entry.repro_risk if entry else "medium",
        comp_confidence=val.confidence.value,
        liquidity=entry.liquidity if entry else "medium",
    )

    deal = ec.evaluate(
        sku=val.sku,
        ask=ask,
        ship_in=ship_in,
        # Price against the conservative quantile, never the centre. Stage 2
        # already explained why: the median of SOLD listings is
        # survivorship-inflated relative to what a new listing achieves.
        resale=val.conservative_resale,
        days_to_sell=val.est_days_to_sell,
        fees=fees, ops=ops, risk=risk, hurdle=hurdle,
    )

    if target.verdict.value == "review" and deal.take:
        deal.take = False
        deal.reasons.append("stage 1 flagged for review — clears economics "
                            "but needs a human to look at the photos")

    return Candidate(raw_title, ask, ship_in, target, val, deal)


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """
    Sort by profit per day of capital, not by margin or by absolute profit.

    Margin ranking buys expensive slow inventory; absolute-profit ranking
    does the same. Capital velocity is what compounds.
    """
    live = [c for c in candidates if c.deal and c.deal.take]
    rest = [c for c in candidates if not (c.deal and c.deal.take)]
    live.sort(key=lambda c: c.score, reverse=True)
    rest.sort(key=lambda c: c.score, reverse=True)
    return live + rest


# --------------------------------------------------------------------------
# Viability
# --------------------------------------------------------------------------

def viability_scan(source, *, fees=None, ops=None, hurdle=None) -> str:
    """
    For every title in the catalog: what is the most you could pay, and is
    there any purchase price at which the trade works?

    This is the number that decides whether to build the scraper. If only a
    handful of SKUs are reachable, the constraint on this business is
    sourcing rare inventory, not detecting mispricing -- and a scraper
    solves the wrong problem.
    """
    fees = fees or ec.FeeModel()
    ops = ops or ec.OpsModel()
    hurdle = hurdle or ec.Hurdle()

    rows = []
    viable = 0
    total = 0
    for entry in catalog.CATALOG:
        for comp in (C.LOOSE, C.CIB):
            val = comps.value_sku(
                title=entry.canonical, region=R.NTSC_U, variant=V.UNKNOWN,
                completeness=comp, source=source,
                has_budget_reprint=entry.has_greatest_hits, today=TODAY)
            if not val.quotable:
                continue
            total += 1
            risk = ec.RiskModel().scaled(entry.repro_risk, val.confidence.value,
                                         entry.liquidity)
            days = val.est_days_to_sell or 90.0
            mb = ec.max_bid_for(val.conservative_resale, 0.0, fees, ops,
                                risk, hurdle, days)
            # The gate tests the RETURN-INFLATED clock, not raw days-to-sell.
            # Showing raw days made every gated row look inexplicable: a
            # reader sees 159 < 180 and cannot tell why the max bid is zero.
            eff_days = ops.handling_days + days * (1.0 + risk.p_return)
            # A trade needs headroom, not just a positive max bid: you must
            # be able to buy meaningfully below resale to have any edge.
            ratio = mb / val.conservative_resale if val.conservative_resale else 0.0
            ok = mb > 0 and ratio > 0.05
            if ok:
                viable += 1
            why = ""
            if not ok:
                if eff_days > hurdle.max_days:
                    why = f"clock: {eff_days:.0f}d eff > {hurdle.max_days:.0f}"
                elif val.conservative_resale < ec.breakeven_delivered(fees, ops):
                    why = "below structural floor"
                else:
                    why = "no headroom after fees+risk"
            rows.append((mb, entry.canonical, comp.value, val.conservative_resale,
                         eff_days, ratio, ok, entry.liquidity, why))

    rows.sort(reverse=True)
    out = [f"  {'max bid':>9} {'resale':>8} {'ratio':>6} {'eff d':>6}  "
           f"{'liq':<7} SKU"]
    for mb, title, comp, resale, days, ratio, ok, liq, why in rows:
        mark = " " if ok else "x"
        tail = f"  <- {why}" if why else ""
        out.append(f" {mark}{mb:9.2f} {resale:8.2f} {ratio:6.0%} {days:6.0f}  "
                   f"{liq:<7} {title} [{comp}]{tail}")
    out.append("")
    out.append(f"  {viable}/{total} SKUs have any workable purchase price "
               f"at the current hurdle.")
    return "\n".join(out)


# --------------------------------------------------------------------------

DEMO_LISTINGS = [
    ("Rule of Rose PS2 CIB authentic black label", "Original US release, mint.", 260.00, 6.00),
    ("Haunting Ground PS2 complete black label", "Disc case manual, tested.", 95.00, 5.00),
    ("Silent Hill 2 PS2 Greatest Hits complete", "Tested and working.", 38.00, 4.50),
    ("Grand Theft Auto San Andreas PS2 greatest hits disc only", "Works.", 4.00, 4.00),
    ("Kingdom Hearts PS2 CIB tested working", "", 9.00, 4.50),
    ("God Hand PS2 black label complete", "Rare Capcom action game.", 55.00, 5.00),
    ("Persona 3 FES PS2 complete", "Black label, disc case manual.", 40.00, 5.00),
    ("Okami PS2 repro custom cover", "Burned backup disc.", 12.00, 4.00),
    ("Shadow of the Colossus PS2 disc only untested", "No way to test.", 5.00, 4.00),
    ("Fatal Frame 3 The Tormented PS2 CIB", "Complete, black label.", 110.00, 6.00),
]


def main() -> None:
    src = ms.CombinedSource(ms.MockMarketplace(seed=7, today=TODAY),
                            ms.MockReference(TODAY))

    print("=" * 104)
    print("COST STRUCTURE")
    print("=" * 104)
    f, o = ec.FeeModel(), ec.OpsModel()
    print(f"  effective fee rate  {f.effective_rate:.2%}  "
          f"(headline {f.fvf_rate:.1%}, inflated because the FVF base "
          f"includes buyer sales tax)")
    print(f"  fixed cost per sale ${o.fixed_sale_cost + f.per_order_high:.2f}  "
          f"(postage + supplies + per-order fee)")
    print(f"  structural floor    ${ec.breakeven_delivered(f, o):.2f} delivered — "
          f"below this a FREE copy still loses money")
    print()
    print("  DEAD ZONE (buying at 50% of delivered resale, 60 days to sell)")
    print(ec.dead_zone_table())

    print()
    print("=" * 104)
    print("CANDIDATE LISTINGS, RANKED BY PROFIT PER DAY OF CAPITAL")
    print("=" * 104)
    cands = [assess(t, d, ask, ship, src) for t, d, ask, ship in DEMO_LISTINGS]
    for c in rank(cands):
        print(c.line())

    print()
    print("=" * 104)
    print("CATALOG VIABILITY — is there ANY price at which each SKU works?")
    print("=" * 104)
    print(viability_scan(src))

    print()
    print("=" * 104)
    print("SENSITIVITY — a $90 resale bought at $40")
    print("=" * 104)
    print(ec.sensitivity(resale=90.0, ask=40.0))

    print()
    print("=" * 104)
    print("THROUGHPUT — is this a business or a hobby?")
    print("=" * 104)
    # Median profile of the SKUs that actually clear the hurdle, rather than
    # the best one: ranking on the winner is how every reseller spreadsheet
    # talks itself into the plan.
    print(ec.throughput_report(profit_per_flip=22.0, days_to_sell=110.0,
                               capital_per_flip=45.0))


if __name__ == "__main__":
    main()
