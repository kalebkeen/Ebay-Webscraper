"""
backtest.py — Stage 4. Does any of this work?

The harness replays a timeline: at each signal date it runs the full
pipeline using ONLY data available then, records what it predicted, and
scores that prediction against what actually happened afterwards.

Three questions, deliberately separated, because a system can pass one and
fail the others and the remedies are completely different.

  CALIBRATION — were the price estimates right?
      Pure forecasting. Compare predicted resale against the comp median
      that materialised in the evaluation window. Reports bias (systematic
      over/under), absolute error, and interval coverage. A model can be
      unbiased on average and still useless if its intervals are so wide
      they contain everything.

  DISCRIMINATION — does TAKE differ from PASS?
      The question people forget. If flagged deals and rejected deals have
      the same realised outcome, the model has no edge and the correct
      response is to stop, regardless of how profitable the TAKEs look --
      profitable TAKEs with equally profitable PASSes means you found a
      rising market, not a signal.

  REALISED P&L — would the trades have made money?
      Simulated at real fees, against the price that actually cleared.

The honest caveat, stated once and meant: run against synthetic data this
validates PLUMBING, not strategy. The generator's assumptions are the
model's assumptions, so agreement proves very little. Its value is that it
is the harness you point at real logged signals, and that `test_backtest`
plants known errors in the generator and checks this code detects them.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

import catalog
import comps
import decide
import economics as ec
import pipeline
import timeline as tl
from comps import Confidence
from listing_parser import Region as R


@dataclass
class Funnel:
    """Where candidates die. Without this, a zero-TAKE backtest is a mystery
    rather than a finding -- and 'no trades cleared' is itself the most
    important result this harness can return."""
    seen: int = 0
    stage1_reject: int = 0
    stage2_no_quote: int = 0
    reached_economics: int = 0
    economics_take: int = 0
    review_blocked: int = 0
    binding: dict = field(default_factory=dict)

    def report(self) -> str:
        def pct(n):
            return f"{n / self.seen:5.1%}" if self.seen else "    -"
        lines = [
            f"  listings seen              {self.seen:6d}",
            f"  rejected at stage 1        {self.stage1_reject:6d}  {pct(self.stage1_reject)}",
            f"  no usable comps (stage 2)  {self.stage2_no_quote:6d}  {pct(self.stage2_no_quote)}",
            f"  priced at stage 3          {self.reached_economics:6d}  {pct(self.reached_economics)}",
            f"  cleared economics          {self.economics_take:6d}  {pct(self.economics_take)}",
            f"  ...then held for review    {self.review_blocked:6d}",
            f"  actionable TAKE            {self.economics_take - self.review_blocked:6d}",
        ]
        if self.binding:
            lines.append("  binding constraint at stage 3:")
            for k, v in sorted(self.binding.items(), key=lambda kv: -kv[1])[:6]:
                lines.append(f"    {v:5d}  {k}")
        return "\n".join(lines)


@dataclass
class Signal:
    """Everything the pipeline believed at signal time. Frozen."""
    signal_date: date
    event: tl.ListingEvent
    sku: str
    predicted_centre: float      # comps' best guess -- the calibration target
    predicted_resale: float      # the conservative quantile we priced against
    p25: float
    p75: float
    confidence: str
    n_effective: float
    max_bid: float
    expected_profit: float
    expected_days: float
    take: bool
    verdict: str


@dataclass
class Outcome:
    signal: Signal
    realised_resale: float | None      # comp median after the signal
    realised_n: int
    source_sold: bool                  # did the listing we saw actually sell?
    source_days: int | None
    realised_profit: float | None      # P&L had we bought at ask

    @property
    def error(self) -> float | None:
        """Centre error. The conservative quantile is scored separately by
        `beat_conservative` -- it is SUPPOSED to sit below the median, so
        grading it against one just re-measures the intended offset."""
        if self.realised_resale is None or not self.signal.predicted_centre:
            return None
        return (self.signal.predicted_centre - self.realised_resale) / self.realised_resale

    @property
    def beat_conservative(self) -> bool | None:
        """Did the market clear above the price we priced against?

        This is the number that matters for a buy decision. The conservative
        quantile targets roughly the 33rd percentile, so a well-behaved model
        should clear it around 65-75% of the time. Much higher means it is
        leaving money on the table; much lower means the buy prices are
        built on a resale that does not happen."""
        if self.realised_resale is None:
            return None
        return self.realised_resale >= self.signal.predicted_resale

    @property
    def covered(self) -> bool | None:
        """Did the realised price land inside the predicted p25-p75 band?"""
        if self.realised_resale is None:
            return None
        return self.signal.p25 <= self.realised_resale <= self.signal.p75


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _entry(title: str):
    return next((t for t in catalog.CATALOG if t.canonical == title), None)


def _reason_class(reason: str) -> str:
    for probe, label in (
        ("below structural floor", "resale below structural floor"),
        ("< $", "absolute profit floor"),
        ("ROI", "ROI floor"),
        ("annualised", "annualised ROI floor"),
        ("days to clear", "too slow (clock gate)"),
        ("exceeds", "capital concentration cap"),
        ("above max bid", "ask above max bid"),
    ):
        if probe in reason:
            return label
    return reason[:40]


def generate_signals(
    timeline: tl.Timeline,
    signal_dates: list[date],
    *,
    window_days: int = 21,
    max_per_date: int = 60,
    fees: ec.FeeModel | None = None,
    ops: ec.OpsModel | None = None,
    hurdle: ec.Hurdle | None = None,
) -> list[Signal]:
    """
    Walk forward. At each date, evaluate listings that appeared in the
    preceding `window_days`, using a source frozen at that date.
    """
    fees = fees or ec.FeeModel()
    ops = ops or ec.OpsModel()
    hurdle = hurdle or ec.Hurdle()
    out: list[Signal] = []
    funnel = Funnel()

    for when in signal_dates:
        source = tl.PointInTime(timeline, when)
        events = timeline.listings_between(when - timedelta(days=window_days), when)
        if len(events) > max_per_date:
            # Deterministic random subset. Slicing a date-sorted list biases
            # the sample toward whatever titles happen to sit early in the
            # window, which quietly distorts the feed-composition finding.
            rng = random.Random(f"sample|{when}")
            events = rng.sample(events, max_per_date)
        for event in events:
            funnel.seen += 1
            target = pipeline.resolve(event.raw_title, event.description)
            if target.verdict.value == "reject" or target.title is None:
                funnel.stage1_reject += 1
                continue

            entry = _entry(target.title)
            region = target.region if target.region is not R.UNKNOWN else R.NTSC_U
            val = comps.value_sku(
                title=target.title, region=region, variant=target.variant,
                completeness=target.completeness, source=source,
                has_budget_reprint=bool(entry.has_greatest_hits) if entry else True,
                today=when)
            if not val.quotable:
                funnel.stage2_no_quote += 1
                continue

            risk = ec.RiskModel().scaled(
                repro_risk=entry.repro_risk if entry else "medium",
                comp_confidence=val.confidence.value,
                liquidity=entry.liquidity if entry else "medium")

            deal = ec.evaluate(
                sku=val.sku, ask=event.ask, ship_in=event.ship_in,
                resale=val.conservative_resale,
                days_to_sell=val.est_days_to_sell,
                fees=fees, ops=ops, risk=risk, hurdle=hurdle)

            funnel.reached_economics += 1
            if deal.take:
                funnel.economics_take += 1
                if target.verdict.value == "review":
                    funnel.review_blocked += 1
            elif deal.reasons:
                k = _reason_class(deal.reasons[0])
                funnel.binding[k] = funnel.binding.get(k, 0) + 1

            take = deal.take and target.verdict.value != "review"
            out.append(Signal(
                signal_date=when, event=event, sku=val.sku,
                predicted_centre=val.expected_resale,
                predicted_resale=val.conservative_resale,
                p25=val.p25, p75=val.p75,
                confidence=val.confidence.value,
                n_effective=val.n_effective,
                max_bid=deal.max_bid, expected_profit=deal.expected_profit,
                expected_days=deal.expected_days, take=take,
                verdict=target.verdict.value))
    return out, funnel


def score(
    signals: list[Signal],
    timeline: tl.Timeline,
    *,
    horizon_days: int = 90,
    fees: ec.FeeModel | None = None,
    ops: ec.OpsModel | None = None,
) -> list[Outcome]:
    """
    Score each signal against what happened in the `horizon_days` after it.

    Realised resale is the median DELIVERED price of matching sales in the
    forward window -- the same basis the prediction used.
    """
    fees = fees or ec.FeeModel()
    ops = ops or ec.OpsModel()
    out: list[Outcome] = []

    for sig in signals:
        ev = sig.event
        start = sig.signal_date
        end = min(start + timedelta(days=horizon_days), timeline.end)
        # Match on TRUE variant, not the stated label. A variant-specific
        # prediction scored against a variant-mixed realisation is biased by
        # construction -- low for Greatest Hits SKUs, high for black label --
        # and that artefact would swamp any real calibration error.
        forward = [r for r, true_v in
                   timeline.sales_detailed(ev.title, ev.region, start, end)
                   if r.completeness is ev.completeness and true_v is ev.variant]

        realised = (statistics.median(r.total for r in forward)
                    if len(forward) >= 3 else None)

        profit = None
        if realised is not None:
            landed = ec.landed_cost(ev.ask, ev.ship_in, ops)
            profit = round(ec.net_proceeds(realised, fees, ops) - landed, 2)

        out.append(Outcome(
            signal=sig, realised_resale=realised, realised_n=len(forward),
            source_sold=ev.sold,
            source_days=((ev.sold_on - ev.listed_on).days if ev.sold else None),
            realised_profit=profit))
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(int(p * len(xs)), len(xs) - 1)
    return xs[i]


def feed_report(signals: list[Signal], fees: ec.FeeModel | None = None,
                ops: ec.OpsModel | None = None) -> str:
    """
    What is actually in the pipe, by value.

    Listing volume is inversely correlated with value: the cheap titles are
    the ones that get listed constantly, so a feed sampled from real supply
    is dominated by inventory that cannot clear its own fixed costs no
    matter how well it is bought. This is a property of the market, not of
    the sourcing, and no amount of detection skill changes it.
    """
    fees = fees or ec.FeeModel()
    ops = ops or ec.OpsModel()
    if not signals:
        return "  no signals"
    floor = ec.breakeven_delivered(fees, ops)
    resales = sorted(s.predicted_resale for s in signals)
    bands = [(0, floor, "below structural floor"),
             (floor, 27.0, "above floor, below workable"),
             (27.0, 130.0, "workable band"),
             (130.0, 1e9, "high value / slow")]
    lines = [f"  {len(signals)} signals, median resale "
             f"${statistics.median(resales):.2f}, "
             f"structural floor ${floor:.2f}"]
    for lo, hi, label in bands:
        n = sum(1 for r in resales if lo <= r < hi)
        bar = "#" * int(40 * n / len(resales))
        lines.append(f"    {label:<28} {n:5d} {n / len(resales):5.0%} {bar}")
    return "\n".join(lines)


def calibration_report(outcomes: list[Outcome]) -> str:
    """Were the price estimates right, and are the error bars honest?"""
    scored = [o for o in outcomes if o.error is not None]
    if not scored:
        return "  no scoreable outcomes"

    errs = [o.error for o in scored]
    covered = [o.covered for o in scored if o.covered is not None]
    lines = [
        f"  scoreable signals    {len(scored)}",
        f"  median bias          {statistics.median(errs):+.1%}   "
        f"(positive = predicted above what the market paid)",
        f"  mean abs error       {statistics.mean(abs(e) for e in errs):.1%}",
        f"  p10 / p90 error      {_pct(errs, 0.10):+.1%} / {_pct(errs, 0.90):+.1%}",
        f"  p25-p75 coverage     {sum(covered) / len(covered):.0%}   "
        f"(a calibrated 50% band lands near 50%)",
    ]

    beat = [o.beat_conservative for o in outcomes
            if o.beat_conservative is not None]
    if beat:
        lines.append(
            f"  cleared conservative {sum(beat) / len(beat):.0%}   "
            f"(target ~65-75%: it is the ~33rd percentile by design)")

    by_conf: dict[str, list[float]] = {}
    for o in scored:
        by_conf.setdefault(o.signal.confidence, []).append(o.error)
    lines.append("  bias by comp confidence:")
    for conf in ("high", "medium", "low"):
        if conf in by_conf:
            v = by_conf[conf]
            lines.append(f"    {conf:<8} n={len(v):<5} "
                         f"median {statistics.median(v):+.1%}  "
                         f"MAE {statistics.mean(abs(e) for e in v):.1%}")
    return "\n".join(lines)


def discrimination_report(outcomes: list[Outcome]) -> str:
    """
    The question that decides whether to continue.

    Two independent measures. The source-listing sell-through needs no
    assumptions at all: if a listing the model called a bargain sells no
    faster than one it rejected, the model is not seeing mispricing.
    """
    takes = [o for o in outcomes if o.signal.take]
    passes = [o for o in outcomes if not o.signal.take]
    if not takes or not passes:
        return f"  degenerate split: {len(takes)} take / {len(passes)} pass"

    def sell_rate(group):
        return sum(1 for o in group if o.source_sold) / len(group)

    def med_days(group):
        d = [o.source_days for o in group if o.source_days is not None]
        return statistics.median(d) if d else float("nan")

    def med_profit(group):
        p = [o.realised_profit for o in group if o.realised_profit is not None]
        return statistics.median(p) if p else float("nan")

    lines = [
        f"  {'':22} {'TAKE':>12} {'PASS':>12}",
        f"  {'count':<22} {len(takes):12d} {len(passes):12d}",
        f"  {'source sell-through':<22} {sell_rate(takes):11.0%} {sell_rate(passes):11.0%}",
        f"  {'median days to sell':<22} {med_days(takes):12.0f} {med_days(passes):12.0f}",
        f"  {'median realised P&L':<22} {med_profit(takes):12.2f} {med_profit(passes):12.2f}",
    ]
    edge = sell_rate(takes) - sell_rate(passes)
    lines.append("")
    if edge > 0.08:
        lines.append(f"  Sell-through edge {edge:+.0%}: flagged listings do clear faster, "
                     "which is\n  independent evidence the model finds real mispricing.")
    elif edge > 0.0:
        lines.append(f"  Sell-through edge only {edge:+.0%}. Weak. Could be noise.")
    else:
        lines.append(f"  NO EDGE ({edge:+.0%}). Flagged listings clear no faster than "
                     "rejected ones.\n  Whatever the P&L column says, this is not a signal.")
    return "\n".join(lines)


def pnl_report(outcomes: list[Outcome]) -> str:
    """Realised P&L on the trades the model said to make."""
    takes = [o for o in outcomes
             if o.signal.take and o.realised_profit is not None]
    if not takes:
        return "  no completed TAKE trades to score"

    profits = [o.realised_profit for o in takes]
    wins = [p for p in profits if p > 0]
    capital = sum(o.signal.event.ask + o.signal.event.ship_in for o in takes)
    predicted = sum(o.signal.expected_profit for o in takes)

    lines = [
        f"  trades               {len(takes)}",
        f"  win rate             {len(wins) / len(takes):.0%}",
        f"  total realised       ${sum(profits):+,.2f}",
        f"  total predicted      ${predicted:+,.2f}",
        f"  prediction gap       {(sum(profits) - predicted) / abs(predicted):+.0%}"
        if predicted else "  prediction gap       n/a",
        f"  median per trade     ${statistics.median(profits):+.2f}",
        f"  worst / best         ${min(profits):+.2f} / ${max(profits):+.2f}",
        f"  capital deployed     ${capital:,.2f}",
        f"  return on capital    {sum(profits) / capital:+.1%}",
    ]
    losses = sorted(p for p in profits if p <= 0)
    if losses:
        lines.append(f"  loss tail (worst 5)  "
                     + ", ".join(f"${p:.0f}" for p in losses[:5]))
    return "\n".join(lines)


def full_report(outcomes: list[Outcome]) -> str:
    return "\n".join([
        "CALIBRATION — were the price estimates right?",
        calibration_report(outcomes), "",
        "DISCRIMINATION — does TAKE differ from PASS?",
        discrimination_report(outcomes), "",
        "REALISED P&L — would the flagged trades have made money?",
        pnl_report(outcomes),
    ])


def hurdle_sweep(timeline: tl.Timeline, dates: list[date],
                 horizon_days: int = 90) -> str:
    """
    What would you have to accept to get any trades at all?

    At the default hurdle this strategy makes zero trades, for two separate
    reasons: the cheap majority cannot clear $6.60 of fixed costs, and the
    valuable minority cannot clear the 180-day clock. Those are different
    problems and only one of them is negotiable.

    Each row relaxes something and reports what the relaxation actually
    bought, scored against realised prices. The `edge` column is the one to
    read first -- realised profit means nothing if the rejected listings did
    just as well.
    """
    configs = {
        "default": ec.Hurdle(),
        "patient (1yr)": ec.Hurdle(max_days=365),
        "patient + $5 floor": ec.Hurdle(max_days=365, min_profit=5.0),
        "aggressive": ec.Hurdle(max_days=365, min_profit=5.0,
                                min_roi=0.20, min_annualised_roi=0.30),
        "no clock, $3 floor": ec.Hurdle(max_days=10_000, min_profit=3.0,
                                        min_roi=0.15, min_annualised_roi=0.15),
    }
    lines = [f"  {'hurdle':<21} {'takes':>6} {'win%':>6} {'realised':>10} "
             f"{'per trade':>10} {'ROC':>7} {'edge':>7}"]
    for name, h in configs.items():
        sigs, _ = generate_signals(timeline, dates, hurdle=h)
        outs = score(sigs, timeline, horizon_days=horizon_days)
        takes = [o for o in outs if o.signal.take and o.realised_profit is not None]
        if not takes:
            lines.append(f"  {name:<21} {0:6d} {'-':>6} {'-':>10} "
                         f"{'-':>10} {'-':>7} {'-':>7}")
            continue
        profits = [o.realised_profit for o in takes]
        capital = sum(o.signal.event.ask + o.signal.event.ship_in for o in takes)
        wins = sum(1 for x in profits if x > 0)
        passes = [o for o in outs if not o.signal.take]
        t_rate = sum(1 for o in takes if o.source_sold) / len(takes)
        p_rate = (sum(1 for o in passes if o.source_sold) / len(passes)
                  if passes else float("nan"))
        lines.append(
            f"  {name:<21} {len(takes):6d} {wins / len(takes):5.0%} "
            f"{sum(profits):+10.2f} {statistics.median(profits):+10.2f} "
            f"{sum(profits) / capital:6.0%} {t_rate - p_rate:+6.0%}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def run(seed: int = 23, truth: tl.TruthParams | None = None,
        n_dates: int = 8, horizon_days: int = 90, quiet: bool = False):
    """Build a timeline, walk it forward, score the results."""
    start, end = date(2024, 1, 1), date(2026, 8, 22)
    line = tl.Timeline(start, end, seed=seed, truth=truth)

    # Signal dates need a year of history behind them and a full horizon of
    # future ahead, or the scoring window is truncated and the last signals
    # are graded on partial data.
    first = start + timedelta(days=365)
    last = end - timedelta(days=horizon_days)
    step = max((last - first).days // n_dates, 1)
    dates = [first + timedelta(days=step * i) for i in range(n_dates)]

    signals, funnel = generate_signals(line, dates)
    outcomes = score(signals, line, horizon_days=horizon_days)
    if not quiet:
        print(f"  timeline {start} to {end}, {len(line._listings)} listings")
        print(f"  {len(dates)} signal dates, {len(signals)} signals, "
              f"{horizon_days}-day scoring horizon\n")
    return line, signals, outcomes, funnel


def main() -> None:
    print("=" * 92)
    print("STAGE 4 — WALK-FORWARD BACKTEST")
    print("=" * 92)
    _, signals, outcomes, funnel = run()
    print("FEED COMPOSITION — what is actually in the pipe")
    print(feed_report(signals))
    print()
    print("FUNNEL — where candidates die")
    print(funnel.report())
    print()
    print(full_report(outcomes))
    print()
    print("=" * 92)
    print("HURDLE SWEEP — what would you have to accept to trade at all?")
    print("=" * 92)
    start, end = date(2024, 1, 1), date(2026, 8, 22)
    line = tl.Timeline(start, end, seed=23)
    first, last = start + timedelta(days=365), end - timedelta(days=90)
    step = max((last - first).days // 8, 1)
    dates = [first + timedelta(days=step * i) for i in range(8)]
    print(hurdle_sweep(line, dates))
    print()
    print("=" * 92)
    print("Synthetic data: this validates the harness, not the strategy. The")
    print("generator's assumptions are the model's assumptions, so agreement")
    print("proves little. Point it at real logged signals before believing a")
    print("number in the P&L block.")
    print("=" * 92)


if __name__ == "__main__":
    main()
