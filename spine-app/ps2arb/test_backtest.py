"""
test_backtest.py — does the harness have any power?

A backtest that reports "no edge" is only informative if it *could* have
reported an edge. A calibration report that shows no bias is only
reassuring if it would catch a bias that was there. So these tests do two
things beyond checking plumbing:

  PLANT KNOWN ERRORS. The timeline carries a `TruthParams` describing the
  process that generated it. Set a parameter to something the model does
  not assume, and the calibration report must notice. If it doesn't, every
  clean report the harness has ever produced was meaningless.

  PLANT A KNOWN EDGE. Feed the discrimination metric an oracle that flags
  genuinely underpriced listings. If the sell-through edge does not light
  up, then "NO EDGE" on the real model was never evidence of anything.

The look-ahead tests are the boring ones and also the ones most worth
having: a leak there invalidates every number downstream, and it is the
single most common way a backtest reports a strategy that isn't real.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

import backtest as bt
import timeline as tl
from listing_parser import Region as R, Variant as V

START, END = date(2024, 1, 1), date(2026, 8, 22)
CHECKS: list[tuple[str, callable, str]] = []


def check(name: str, why: str):
    def deco(fn):
        CHECKS.append((name, fn, why))
        return fn
    return deco


@check("no_lookahead", "A point-in-time source must never reveal the future.")
def _no_lookahead():
    line = tl.Timeline(START, END, seed=23)
    bad = []
    for as_of in (date(2024, 9, 1), date(2025, 6, 1), date(2026, 3, 1)):
        src = tl.PointInTime(line, as_of)
        for title in ("Silent Hill 2", "Ico", "God of War"):
            # Deliberately request a window that runs past the as-of date.
            recs = src.sold_records(title, R.NTSC_U, START)
            future = [r for r in recs if r.sold_on > as_of]
            if future:
                bad.append(f"{title} @ {as_of}: {len(future)} future sales leaked "
                           f"(latest {max(r.sold_on for r in future)})")
            q = src.quote(title, R.NTSC_U)
            for tier, cq in q.items():
                if cq.as_of and cq.as_of > as_of:
                    bad.append(f"{title} @ {as_of}: quote dated {cq.as_of}")
    return bad


@check("time_consistency", "The past must not change when the clock moves.")
def _time_consistency():
    line = tl.Timeline(START, END, seed=23)
    bad = []
    cut = date(2025, 6, 1)
    early = tl.PointInTime(line, cut).sold_records("Silent Hill 2", R.NTSC_U, START)
    late = [r for r in tl.PointInTime(line, END)
            .sold_records("Silent Hill 2", R.NTSC_U, START) if r.sold_on <= cut]
    if early != late:
        bad.append(f"history differs by clock: {len(early)} vs {len(late)} records "
                   f"for the same period")
    return bad


@check("active_count_is_pit", "Active supply must be as-of, not end-of-history.")
def _active_pit():
    line = tl.Timeline(START, END, seed=23)
    bad = []
    early = line.active_count("Silent Hill 2", R.NTSC_U, date(2024, 3, 1))
    late = line.active_count("Silent Hill 2", R.NTSC_U, date(2026, 6, 1))
    # Not asserting a direction -- only that the clock is actually consulted.
    if early == late and early == 1:
        bad.append("active_count looks constant; the as_of argument may be ignored")
    return bad


@check("detects_planted_bias",
       "A known error in the generator must show up in the calibration report.")
def _planted_bias():
    """
    Plant an error in the parameter the estimator actually depends on.

    Two earlier versions of this test failed to detect anything, and each
    failure was informative rather than a nuisance:

      1. Planting a GH_PRICE_RATIO error with normal variant labelling
         detected nothing, because the model measures the ratio from
         labelled sales and the prior never binds.
      2. Suppressing labelling to force the de-mix path detected nothing
         either -- but for a bad reason. The old mean-based de-mix carried
         a constant ~-8% error that swamped the signal. Fixing it made the
         estimator quantile-based, and a quantile de-mix does not use the
         price ratio at all: it finds the cheap component wherever it sits.

    What the quantile estimator DOES assume is the population SHARE. If
    most surviving copies are budget reprints and the model thinks it is
    60/40, it reads the wrong quantile and the error is real.
    """
    dates = [date(2025, 4, 1) + timedelta(days=90 * i) for i in range(4)]

    def bias_for(share: float):
        line = tl.Timeline(START, END, seed=31,
                           truth=tl.TruthParams(gh_population_share=share,
                                                variant_label_rate=0.02))
        sigs, _ = bt.generate_signals(line, dates, max_per_date=45)
        outs = bt.score(sigs, line, horizon_days=90)
        gh = [o.error for o in outs
              if o.error is not None and o.signal.event.variant is V.GREATEST_HITS]
        if len(gh) < 12:
            return float("nan"), float("nan"), 0
        # Standard error of a median ~ 1.253 * sigma / sqrt(n), with sigma
        # estimated robustly so a couple of wild errors don't inflate it.
        med = statistics.median(gh)
        mad = statistics.median(abs(e - med) for e in gh)
        se = 1.253 * (mad * 1.4826) / math.sqrt(len(gh))
        return med, se, len(gh)

    honest, se_h, n_h = bias_for(0.60)     # matches comps.GH_POPULATION_SHARE
    planted, se_p, n_p = bias_for(0.92)    # model now reads the wrong quantile

    if honest != honest or planted != planted:
        return ["not enough Greatest Hits signals to measure bias"]

    # Detection means the shift is large relative to SAMPLING NOISE, not
    # relative to a threshold picked by eye. A fixed cutoff would either
    # fail a genuine detection on a quiet dataset or wave through noise on
    # a small one.
    shift = planted - honest
    noise = math.sqrt(se_h ** 2 + se_p ** 2)
    z = abs(shift) / noise if noise else float("inf")
    if z < 2.5:
        return [f"planted a 0.60->0.92 population-share error: bias "
                f"{honest:+.1%} -> {planted:+.1%} (shift {shift:+.1%}, "
                f"noise +/-{noise:.1%}, z={z:.1f}). Not separable from "
                f"sampling error, so the harness cannot detect a known bug."]
    if shift > 0:
        return [f"detected a shift (z={z:.1f}) but in the wrong direction: "
                f"more budget reprints in the pool should make the model "
                f"read too LOW a quantile and under-predict."]
    return []


@check("prior_not_binding_when_data_exists",
       "With labelled comps available, the GH prior must not drive the estimate.")
def _prior_independence():
    """
    The flip side, and worth locking in: when sellers do label variants, a
    wrong prior should wash out because the model measures the ratio
    instead of assuming it. If this ever starts failing, the estimator has
    become prior-dependent somewhere it should not be.
    """
    dates = [date(2025, 4, 1) + timedelta(days=90 * i) for i in range(4)]

    def bias_for(ratio: float) -> float:
        line = tl.Timeline(START, END, seed=31,
                           truth=tl.TruthParams(gh_price_ratio=ratio,
                                                variant_label_rate=0.35))
        sigs, _ = bt.generate_signals(line, dates, max_per_date=45)
        outs = bt.score(sigs, line, horizon_days=90)
        gh = [o.error for o in outs
              if o.error is not None and o.signal.event.variant is V.GREATEST_HITS]
        return statistics.median(gh) if len(gh) >= 12 else float("nan")

    honest, planted = bias_for(0.55), bias_for(0.30)
    if planted != planted or honest != honest:
        return []
    if abs(planted - honest) > 0.15:
        return [f"prior leaked through despite labelled data: "
                f"{honest:+.1%} -> {planted:+.1%}"]
    return []


@check("detects_planted_edge",
       "The discrimination metric must light up when a real edge exists.")
def _planted_edge():
    """
    Replace the model with an oracle that flags listings priced below their
    true value. Sell-through edge must go strongly positive. If it doesn't,
    a 'NO EDGE' verdict on the real model was never evidence of anything.
    """
    line = tl.Timeline(START, END, seed=23)
    events = line.listings_between(START, END)[:2500]

    outs = []
    for ev in events:
        # The oracle cheats: it sees the true price the generator drew from.
        import random as _r
        fair = line._true_price(ev.title, ev.completeness, ev.variant,
                                ev.listed_on, _r.Random(0), R.NTSC_U)
        underpriced = ev.ask < fair * 0.65
        sig = bt.Signal(
            signal_date=ev.listed_on, event=ev, sku=f"{ev.title}|x|x|x",
            predicted_centre=fair, predicted_resale=fair * 0.9,
            p25=fair * 0.8, p75=fair * 1.2, confidence="high",
            n_effective=20.0, max_bid=fair * 0.6, expected_profit=10.0,
            expected_days=60.0, take=underpriced, verdict="proceed")
        outs.append(bt.Outcome(signal=sig, realised_resale=None, realised_n=0,
                               source_sold=ev.sold,
                               source_days=((ev.sold_on - ev.listed_on).days
                                            if ev.sold else None),
                               realised_profit=None))

    takes = [o for o in outs if o.signal.take]
    passes = [o for o in outs if not o.signal.take]
    if not takes or not passes:
        return ["oracle produced a degenerate split"]
    edge = (sum(1 for o in takes if o.source_sold) / len(takes)
            - sum(1 for o in passes if o.source_sold) / len(passes))
    if edge < 0.15:
        return [f"oracle edge only {edge:+.0%} — the sell-through metric is "
                f"too blunt to detect mispricing that is known to be there"]
    return []


@check("coverage_sane", "Interval coverage must be in a believable range.")
def _coverage():
    line = tl.Timeline(START, END, seed=23)
    dates = [date(2025, 4, 1) + timedelta(days=110 * i) for i in range(4)]
    sigs, _ = bt.generate_signals(line, dates, max_per_date=45)
    outs = bt.score(sigs, line, horizon_days=90)
    cov = [o.covered for o in outs if o.covered is not None]
    if len(cov) < 30:
        return ["too few scoreable outcomes to judge coverage"]
    rate = sum(cov) / len(cov)
    # A p25-p75 band scored against a MEDIAN should over-cover: a median is
    # far less dispersed than the individual sales the band describes. Below
    # 40% means the band is misplaced, not merely narrow.
    if rate < 0.40:
        return [f"p25-p75 coverage {rate:.0%} — band is systematically "
                f"misplaced, not just tight"]
    return []


@check("scoring_variant_matched",
       "Scoring must compare like with like across variants.")
def _variant_matched():
    """
    Regression guard. Scoring a variant-specific prediction against a
    variant-mixed realisation produced a large phantom bias that looked
    exactly like a model error.
    """
    line = tl.Timeline(START, END, seed=23)
    dates = [date(2025, 6, 1), date(2025, 11, 1)]
    sigs, _ = bt.generate_signals(line, dates, max_per_date=40)
    outs = bt.score(sigs, line, horizon_days=90)

    gh = [o.error for o in outs if o.error is not None
          and o.signal.event.variant is V.GREATEST_HITS]
    bl = [o.error for o in outs if o.error is not None
          and o.signal.event.variant is V.BLACK_LABEL]
    if len(gh) < 8 or len(bl) < 8:
        return []
    gap = abs(statistics.median(gh) - statistics.median(bl))
    if gap > 0.35:
        return [f"GH bias {statistics.median(gh):+.0%} vs BL "
                f"{statistics.median(bl):+.0%} — {gap:.0%} apart, which "
                f"suggests the populations are still mismatched"]
    return []


def main() -> int:
    failures = 0
    print(f"{'CHECK':<28} RESULT")
    print("-" * 92)
    for name, fn, why in CHECKS:
        problems = fn()
        if problems:
            failures += 1
            print(f"!! {name:<26} FAILED")
            print(f"   {why}")
            for p in problems[:4]:
                print(f"     - {p}")
        else:
            print(f"   {name:<26} ok")
    print("-" * 92)
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} checks passed\n")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
