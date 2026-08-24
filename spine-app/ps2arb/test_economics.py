"""
test_economics.py — invariants for Stage 3.

The centrepiece is `boundary_consistency`. `max_bid_for()` solves the profit
equation in closed form; `evaluate()` computes profit directly. They are two
implementations of the same maths, and nothing forces them to agree. When
they drift, the pipeline recommends a maximum bid that its own evaluator
then rejects — and there is no exception, no warning, just a quietly
incoherent answer. That bug was live in this file's first version, where
`evaluate()` still used a single-channel risk model after `max_bid_for()`
had moved to two channels.

The rest are monotonicity checks. They are cheap and they catch sign errors,
which are the other way cost models go wrong without anyone noticing.
"""

from __future__ import annotations

from datetime import date

import decide
import economics as ec
import mock_sources as ms

TODAY = date(2026, 8, 22)

SCENARIOS = [
    # (resale, ship_in, days)
    (30.0, 0.0, 30.0),
    (45.0, 4.00, 45.0),
    (60.0, 4.00, 60.0),
    (90.0, 5.00, 60.0),
    (135.0, 5.00, 90.0),
    (250.0, 6.00, 120.0),
    (400.0, 6.00, 150.0),
]

RISKS = [
    ("baseline", ec.RiskModel()),
    ("high repro", ec.RiskModel().scaled("high", "low")),
    ("medium repro", ec.RiskModel().scaled("medium", "medium")),
]

CHECKS: list[tuple[str, object, str]] = []


def check(name: str, why: str):
    def deco(fn):
        CHECKS.append((name, fn, why))
        return fn
    return deco


@check("boundary_consistency",
       "max_bid must be exactly the highest ask that evaluate() accepts.")
def _boundary():
    bad = []
    for resale, ship, days in SCENARIOS:
        for label, risk in RISKS:
            mb = ec.max_bid_for(resale, ship, ec.FeeModel(), ec.OpsModel(),
                                risk, ec.Hurdle(), days)
            if mb <= 0:
                # Must be gated by something no price can fix.
                d = ec.evaluate(sku="", ask=0.01, ship_in=ship, resale=resale,
                                days_to_sell=days, risk=risk)
                if d.take:
                    bad.append(f"{label} r={resale}: max_bid 0 but a $0.01 "
                               f"buy is accepted")
                continue
            at = ec.evaluate(sku="", ask=mb, ship_in=ship, resale=resale,
                             days_to_sell=days, risk=risk)
            over = ec.evaluate(sku="", ask=mb + 0.25, ship_in=ship,
                               resale=resale, days_to_sell=days, risk=risk)
            if not at.take:
                bad.append(f"{label} r={resale} d={days}: evaluate rejects its "
                           f"own max_bid ${mb:.2f} — {at.reasons[:1]}")
            if over.take:
                bad.append(f"{label} r={resale} d={days}: ask ${mb + 0.25:.2f} "
                           f"above max_bid ${mb:.2f} still accepted")
    return bad


@check("profit_monotonic_in_ask", "Paying more must never earn more.")
def _profit_monotonic():
    bad = []
    for resale, ship, days in SCENARIOS:
        prev = None
        for ask in (1.0, 5.0, 10.0, 20.0, 40.0, 80.0):
            d = ec.evaluate(sku="", ask=ask, ship_in=ship, resale=resale,
                            days_to_sell=days)
            if prev is not None and d.expected_profit > prev + 1e-9:
                bad.append(f"r={resale}: profit rose from {prev:.2f} to "
                           f"{d.expected_profit:.2f} as ask went to ${ask}")
            prev = d.expected_profit
    return bad


@check("maxbid_monotonic_in_resale", "A more valuable item must support a higher bid.")
def _maxbid_resale():
    bad = []
    prev = None
    for resale in (30, 45, 60, 90, 135, 200, 300):
        mb = ec.max_bid_for(float(resale), 4.0, ec.FeeModel(), ec.OpsModel(),
                            ec.RiskModel(), ec.Hurdle(), 60.0)
        if prev is not None and mb < prev - 1e-9:
            bad.append(f"resale {resale}: max_bid fell to ${mb:.2f} from ${prev:.2f}")
        prev = mb
    return bad


@check("maxbid_monotonic_in_days", "Slower inventory must support a lower bid.")
def _maxbid_days():
    bad = []
    prev = None
    for days in (10, 30, 60, 90, 120, 150):
        mb = ec.max_bid_for(120.0, 4.0, ec.FeeModel(), ec.OpsModel(),
                            ec.RiskModel(), ec.Hurdle(), float(days))
        if prev is not None and mb > prev + 1e-9:
            bad.append(f"days {days}: max_bid rose to ${mb:.2f} from ${prev:.2f}")
        prev = mb
    return bad


@check("maxbid_monotonic_in_risk", "More risk must never permit a higher bid.")
def _maxbid_risk():
    bad = []
    for resale, ship, days in SCENARIOS:
        base = ec.max_bid_for(resale, ship, ec.FeeModel(), ec.OpsModel(),
                              ec.RiskModel(), ec.Hurdle(), days)
        hot = ec.max_bid_for(resale, ship, ec.FeeModel(), ec.OpsModel(),
                             ec.RiskModel().scaled("high", "low"),
                             ec.Hurdle(), days)
        if hot > base + 1e-9:
            bad.append(f"r={resale}: high-risk max_bid ${hot:.2f} > "
                       f"baseline ${base:.2f}")
    return bad


@check("fee_tier_sensitivity", "The higher media fee tier must lower the max bid.")
def _fee_tier():
    bad = []
    for resale, ship, days in SCENARIOS:
        std = ec.max_bid_for(resale, ship, ec.FeeModel(ec.STANDARD_FVF),
                             ec.OpsModel(), ec.RiskModel(), ec.Hurdle(), days)
        med = ec.max_bid_for(resale, ship, ec.FeeModel(ec.MEDIA_FVF),
                             ec.OpsModel(), ec.RiskModel(), ec.Hurdle(), days)
        if med > std + 1e-9:
            bad.append(f"r={resale}: media tier bid ${med:.2f} > standard ${std:.2f}")
    return bad


@check("no_free_lunch", "You can never justify paying more than the item sells for.")
def _no_free_lunch():
    bad = []
    for resale, ship, days in SCENARIOS:
        mb = ec.max_bid_for(resale, ship, ec.FeeModel(), ec.OpsModel(),
                            ec.RiskModel(), ec.Hurdle(), days)
        if mb >= resale:
            bad.append(f"resale ${resale}: max_bid ${mb:.2f} >= resale")
    return bad


@check("structural_floor", "Below breakeven, a free copy must still lose money.")
def _floor():
    f, o = ec.FeeModel(), ec.OpsModel()
    floor = ec.breakeven_delivered(f, o)
    bad = []
    for resale in (floor - 2.0, floor - 0.5):
        if ec.net_proceeds(resale, f, o) > 0:
            bad.append(f"net_proceeds(${resale:.2f}) > 0 below floor ${floor:.2f}")
    if ec.net_proceeds(floor + 2.0, f, o) <= 0:
        bad.append(f"net_proceeds above floor ${floor:.2f} is not positive")
    return bad


@check("days_gate", "Past the holding-period limit there must be no bid at any price.")
def _days_gate():
    h = ec.Hurdle(max_days=180.0)
    mb = ec.max_bid_for(400.0, 0.0, ec.FeeModel(), ec.OpsModel(),
                        ec.RiskModel(), h, 400.0)
    return [] if mb == 0.0 else [f"400-day flip still returned max_bid ${mb:.2f}"]


@check("pipeline_coherence",
       "Every TAKE from the full pipeline must satisfy ask <= max_bid.")
def _pipeline():
    src = ms.CombinedSource(ms.MockMarketplace(seed=7, today=TODAY),
                            ms.MockReference(TODAY))
    bad = []
    for title, desc, ask, ship in decide.DEMO_LISTINGS:
        c = decide.assess(title, desc, ask, ship, src)
        if c.deal is None:
            continue
        if c.deal.take and c.deal.ask > c.deal.max_bid:
            bad.append(f"{title[:40]}: TAKE at ${c.deal.ask:.2f} above "
                       f"max_bid ${c.deal.max_bid:.2f}")
        if c.deal.take and c.deal.expected_profit <= 0:
            bad.append(f"{title[:40]}: TAKE with profit "
                       f"${c.deal.expected_profit:.2f}")
    return bad



@check("unsold_reduces_maxbid",
       "Dead-stock risk must lower what you can pay, monotonically.")
def _unsold_monotonic():
    bad = []
    f, o, h = ec.FeeModel(), ec.OpsModel(), ec.Hurdle()
    prev = None
    for p_unsold in (0.0, 0.05, 0.15, 0.30, 0.50):
        r = ec.RiskModel(p_unsold=p_unsold)
        mb = ec.max_bid_for(90.0, 4.0, f, o, r, h, 60.0)
        if prev is not None and mb > prev + 1e-9:
            bad.append(f"p_unsold {p_unsold}: max_bid rose to ${mb:.2f} from ${prev:.2f}")
        prev = mb
    return bad


@check("liquidity_scales_unsold",
       "Thin titles must carry more dead-stock risk than liquid ones.")
def _liquidity_scaling():
    bad = []
    base = ec.RiskModel()
    order = ["high", "medium", "low", "thin"]
    vals = [base.scaled("low", "high", liq).p_unsold for liq in order]
    for a, b, la, lb in zip(vals, vals[1:], order, order[1:]):
        if b < a:
            bad.append(f"{la}={a:.3f} but {lb}={b:.3f} — not increasing")
    return bad


@check("boundary_fuzz",
       "Across randomised inputs, max_bid must be exactly the take/pass edge.")
def _boundary_fuzz():
    """
    The forward and inverse paths are written twice and must agree. Any new
    risk channel added to one and not the other shows up here as a max_bid
    that evaluate() then rejects -- a contradiction with no error message.
    """
    import random
    rng = random.Random(11)
    bad = []
    for _ in range(400):
        resale = rng.uniform(8.0, 500.0)
        ship_in = rng.uniform(0.0, 12.0)
        days = rng.uniform(5.0, 200.0)
        risk = ec.RiskModel(
            p_return=rng.uniform(0.0, 0.3),
            p_variant_error=rng.uniform(0.0, 0.3),
            p_counterfeit=rng.uniform(0.0, 0.2),
            p_unsold=rng.uniform(0.0, 0.4),
        )
        f, o, h = ec.FeeModel(), ec.OpsModel(), ec.Hurdle()
        mb = ec.max_bid_for(resale, ship_in, f, o, risk, h, days)
        if mb <= 0:
            continue
        at = ec.evaluate(sku="", ask=mb, ship_in=ship_in, resale=resale,
                         days_to_sell=days, fees=f, ops=o, risk=risk, hurdle=h)
        over = ec.evaluate(sku="", ask=mb + 0.05, ship_in=ship_in, resale=resale,
                           days_to_sell=days, fees=f, ops=o, risk=risk, hurdle=h)
        if not at.take:
            bad.append(f"resale={resale:.0f} d={days:.0f}: max_bid ${mb:.2f} "
                       f"rejected by evaluate ({at.reasons[0] if at.reasons else '?'})")
        if over.take:
            bad.append(f"resale={resale:.0f} d={days:.0f}: ${mb + 0.05:.2f} "
                       f"above max_bid still accepted")
        if len(bad) >= 5:
            break
    return bad


@check("roc_invariant_to_volume",
       "Return on capital must not depend on deal flow — only margin and velocity.")
def _roc_invariance():
    bad = []
    vals = {h: ec.Throughput(hit_rate=h).project(22.0, 110.0, 45.0)
            ["annual_return_on_capital"] for h in (0.005, 0.02, 0.08)}
    if len(set(round(v, 6) for v in vals.values())) != 1:
        bad.append(f"ROC varied with hit_rate: {vals}")
    return bad


def main() -> int:
    failures = 0
    print(f"{'CHECK':<30} RESULT")
    print("-" * 96)
    for name, fn, why in CHECKS:
        problems = fn()
        if problems:
            failures += 1
            print(f"!! {name:<28} FAILED ({len(problems)})")
            print(f"   {why}")
            for p in problems[:5]:
                print(f"     - {p}")
        else:
            print(f"   {name:<28} ok")
    print("-" * 96)
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} checks passed")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
