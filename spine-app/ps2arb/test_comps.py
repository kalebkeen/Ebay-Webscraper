"""
test_comps.py — invariants and known-failure probes for the comp layer.

Statistical code fails quietly. A parser bug shows up as a wrong label; an
estimator bug shows up as a number that is merely somewhat wrong, and you
find out three months later when the inventory hasn't sold. So this suite
asserts structural invariants rather than specific values: relationships
that must hold no matter how the anchors or the noise model change.

The recovery tests are the exception — they check that the estimator lands
near the mock's known true price, which is the only end-to-end evidence that
the contamination handling works at all.
"""

from __future__ import annotations

from datetime import date, timedelta

import catalog
import comps
import mock_sources as ms
import pipeline
from listing_parser import Completeness as C, Region as R, Variant as V

TODAY = date(2026, 8, 22)


def build_source(seed: int = 7, contamination: float | None = None):
    return ms.CombinedSource(
        ms.MockMarketplace(seed=seed, today=TODAY, contamination=contamination),
        ms.MockReference(TODAY),
    )


# --------------------------------------------------------------- bridge

def value_target(target: pipeline.PricingTarget, source) -> comps.Valuation | None:
    """
    Stage 1 -> Stage 2. The only place that knows both shapes.

    Returns None when Stage 1 refused to identify the item, because there is
    nothing to look up. Region UNKNOWN is passed through as NTSC_U with a
    warning rather than guessed silently: on eBay.com the base rate strongly
    favours NTSC-U, but that assumption belongs in the audit trail.
    """
    if target.title is None:
        return None
    entry = next((t for t in catalog.CATALOG if t.canonical == target.title), None)
    region = target.region
    assumed_region = region is R.UNKNOWN
    if assumed_region:
        region = R.NTSC_U

    val = comps.value_sku(
        title=target.title,
        region=region,
        variant=target.variant,
        completeness=target.completeness,
        source=source,
        has_budget_reprint=bool(entry.has_greatest_hits) if entry else True,
        today=TODAY,
    )
    if assumed_region:
        val.warnings.append(
            "region was not stated; assumed NTSC-U — set this from seller "
            "location at ingest, a PAL copy is worth ~30% less"
        )
    return val


# --------------------------------------------------------------- checks

CHECKS: list[tuple[str, callable, str]] = []


def check(name: str, why: str):
    def deco(fn):
        CHECKS.append((name, fn, why))
        return fn
    return deco


@check("ordering", "A quote whose bounds cross is not a quote.")
def _ordering(src):
    bad = []
    for title in list(ms.ANCHORS)[:12]:
        for comp in (C.LOOSE, C.CIB):
            v = comps.value_sku(title=title, region=R.NTSC_U,
                                variant=V.GREATEST_HITS, completeness=comp,
                                source=src, has_budget_reprint=True, today=TODAY)
            if not v.quotable:
                continue
            if not (v.p25 <= v.expected_resale <= v.p75):
                bad.append(f"{title}/{comp.value}: p25<=centre<=p75 violated "
                           f"({v.p25} {v.expected_resale} {v.p75})")
            if v.conservative_resale > v.expected_resale:
                bad.append(f"{title}/{comp.value}: conservative > expected "
                           f"({v.conservative_resale} > {v.expected_resale})")
    return bad


@check("cib_premium", "CIB must not price below loose for the same SKU.")
def _cib_premium(src):
    bad = []
    for title in list(ms.ANCHORS)[:12]:
        lo = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                             completeness=C.LOOSE, source=src,
                             has_budget_reprint=True, today=TODAY)
        ci = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                             completeness=C.CIB, source=src,
                             has_budget_reprint=True, today=TODAY)
        if lo.quotable and ci.quotable and ci.expected_resale < lo.expected_resale:
            bad.append(f"{title}: cib ${ci.expected_resale} < loose ${lo.expected_resale}")
    return bad


@check("variant_ordering", "Black label must price above Greatest Hits.")
def _variant_ordering(src):
    bad = []
    for title in ("Silent Hill 2", "Grand Theft Auto: San Andreas",
                  "Kingdom Hearts", "God of War", "Final Fantasy X"):
        bl = comps.value_sku(title=title, region=R.NTSC_U, variant=V.BLACK_LABEL,
                             completeness=C.LOOSE, source=src,
                             has_budget_reprint=True, today=TODAY)
        gh = comps.value_sku(title=title, region=R.NTSC_U, variant=V.GREATEST_HITS,
                             completeness=C.LOOSE, source=src,
                             has_budget_reprint=True, today=TODAY)
        if bl.quotable and gh.quotable and bl.expected_resale <= gh.expected_resale:
            bad.append(f"{title}: black_label ${bl.expected_resale} "
                       f"<= greatest_hits ${gh.expected_resale}")
    return bad


@check("recovery", "Estimator must land near the mock's true price despite 10% junk.")
def _recovery(src):
    """
    The mock knows the true anchor. Sold totals include ~$5.50 postage, so
    the target is anchor + shipping. Tolerance is wide on purpose: with
    log-normal noise at sigma=0.22 and small n, anything tighter would be
    testing the seed rather than the estimator.
    """
    bad = []
    for title, anchor in list(ms.ANCHORS.items())[:14]:
        v = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                            completeness=C.LOOSE, source=src,
                            has_budget_reprint=False, today=TODAY)
        if not v.quotable or v.n_effective < 5:
            continue
        truth = anchor.loose + 5.50
        err = abs(v.expected_resale - truth) / truth
        if err > 0.30:
            bad.append(f"{title}: got ${v.expected_resale:.2f} vs true "
                       f"${truth:.2f} ({err:+.0%})")
    return bad


@check("contamination", "Heavy contamination must not move the centre much.")
def _contamination(src):
    """
    The MAD filter's whole job. If a 30% junk rate shifts the estimate more
    than a 5% rate does by any meaningful amount, the filter is decorative.
    """
    clean = build_source(contamination=0.02)
    dirty = build_source(contamination=0.30)
    bad = []
    for title in ("Ico", "Okami", "Silent Hill 2", "Katamari Damacy",
                  "Shadow of the Colossus"):
        a = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                            completeness=C.LOOSE, source=clean,
                            has_budget_reprint=False, today=TODAY)
        b = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                            completeness=C.LOOSE, source=dirty,
                            has_budget_reprint=False, today=TODAY)
        if not (a.quotable and b.quotable):
            continue
        shift = abs(b.expected_resale - a.expected_resale) / a.expected_resale
        if shift > 0.20:
            bad.append(f"{title}: 2%->30% contamination moved centre "
                       f"{shift:+.0%} (${a.expected_resale:.2f} -> "
                       f"${b.expected_resale:.2f})")
    return bad


@check("refusal", "Unknown titles must produce no quote, not a zero.")
def _refusal(src):
    v = comps.value_sku(title="Nonexistent Game", region=R.NTSC_U,
                        variant=V.UNKNOWN, completeness=C.LOOSE, source=src,
                        has_budget_reprint=False, today=TODAY)
    return [] if not v.quotable else [f"quoted an unknown title: {v.expected_resale}"]


@check("sealed_refusal", "Sealed must never be extrapolated from used prices.")
def _sealed(src):
    bad = []
    for title in ("Ico", "Rule of Rose", "Okami"):
        v = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                            completeness=C.SEALED, source=src,
                            has_budget_reprint=False, today=TODAY)
        if v.quotable and v.n_effective < 3:
            bad.append(f"{title}: quoted sealed at ${v.expected_resale} "
                       f"on n_eff={v.n_effective}")
    return bad


@check("velocity_scope", "Days-to-sell must not exceed the whole title's inventory clearance.")
def _velocity(src):
    """
    A single SKU cannot take longer to clear than the entire title's active
    shelf does. If it does, the numerator and denominator are scoped
    differently -- the bug that made every liquid title read as dead stock.
    """
    bad = []
    mk = ms.MockMarketplace(seed=7, today=TODAY)
    for title, anchor in list(ms.ANCHORS.items())[:14]:
        v = comps.value_sku(title=title, region=R.NTSC_U, variant=V.UNKNOWN,
                            completeness=C.LOOSE, source=src,
                            has_budget_reprint=False, today=TODAY)
        if v.est_days_to_sell is None:
            continue
        whole_title_days = 30.0 * anchor.active_listings / anchor.sales_per_month
        if v.est_days_to_sell > whole_title_days * 1.6:
            bad.append(f"{title}: SKU {v.est_days_to_sell:.0f}d vs whole-title "
                       f"{whole_title_days:.0f}d")
    return bad


class _FakeSpread:
    """A source with a controllable price spread, for the wide-spread guard."""
    name = "fake-spread"

    def __init__(self, prices, quote_price=None):
        self._prices = prices
        self._quote = quote_price

    def sold_records(self, title, region, since):
        return [comps.SoldRecord(price=float(p), shipping=0.0,
                                 sold_on=TODAY - timedelta(days=i + 1),
                                 completeness=C.LOOSE, variant=V.UNKNOWN,
                                 region=R.NTSC_U)
                for i, p in enumerate(self._prices)]

    def quote(self, title, region):
        if self._quote is None:
            return {}
        return {C.LOOSE: comps.CompQuote(C.LOOSE, float(self._quote), n=0,
                                         as_of=TODAY, source=self.name)}

    def active_listing_count(self, title, region):
        return None


@check("spread_guard",
       "A wide-spread high-value SKU must lower confidence and flag verify; a "
       "tight or low-value one must not, and the reference-only path is exempt.")
def _spread_guard(src):
    bad = []

    def val(prices, quote_price=None):
        return comps.value_sku(title="X", region=R.NTSC_U, variant=V.UNKNOWN,
                               completeness=C.LOOSE,
                               source=_FakeSpread(prices, quote_price),
                               has_budget_reprint=False, today=TODAY)

    # Wide (700/1400 = 2x) + high value + 16 sales (HIGH base): must flag verify
    # and drop a tier (HIGH -> MEDIUM), and bid below the median.
    wide_high = val([700, 1400] * 8)
    if not wide_high.needs_verify:
        bad.append("wide+high spread did not set needs_verify")
    if wide_high.confidence is comps.Confidence.HIGH:
        bad.append("wide spread left confidence at HIGH (should drop a tier)")
    if not (wide_high.conservative_resale < wide_high.expected_resale):
        bad.append("wide+high conservative should sit below the median")

    # Tight + high value: no verify, confidence not dropped by the guard.
    tight_high = val([900, 950, 1000, 975, 925, 1010, 990, 960,
                      940, 1005, 995, 970, 930, 985, 1015, 945])
    if tight_high.needs_verify:
        bad.append("tight high-value spread wrongly flagged verify")

    # Wide but low value: below the value floor -> no verify.
    wide_low = val([5, 12] * 8)
    if wide_low.needs_verify:
        bad.append("low-value wide spread wrongly flagged verify")

    # Reference-only (no sold data): the synthetic band must not trip the guard.
    ref_only = val([], quote_price=500.0)
    if ref_only.needs_verify:
        bad.append("reference-only price wrongly tripped the spread guard")

    return bad


@check("end_to_end", "Stage 1 output must flow into Stage 2 without manual fixup.")
def _end_to_end(src):
    bad = []
    listings = [
        ("Rule of Rose PS2 CIB authentic", ""),
        ("God of War PS2 Complete Black Label", "Disc, case, manual."),
        ("Kingdom Hearts PS2 disc only tested", ""),
        ("Okami PS2 repro custom cover", "Burned disc."),
    ]
    for title, desc in listings:
        target = pipeline.resolve(title, desc)
        val = value_target(target, src)
        if target.verdict.value == "reject":
            continue
        if val is None:
            bad.append(f"{title}: resolved but produced no valuation")
    return bad


def main() -> int:
    src = build_source()
    failures = 0
    print(f"{'CHECK':<20} {'RESULT'}")
    print("-" * 96)
    for name, fn, why in CHECKS:
        problems = fn(src)
        if problems:
            failures += 1
            print(f"!! {name:<18} FAILED ({len(problems)})")
            print(f"   {why}")
            for p in problems[:6]:
                print(f"     - {p}")
        else:
            print(f"   {name:<18} ok")
    print("-" * 96)
    print(f"{len(CHECKS) - failures}/{len(CHECKS)} checks passed\n")
    return failures


def demo() -> None:
    src = build_source()
    print("STAGE 1 -> STAGE 2, end to end")
    print("=" * 96)
    for title, desc in [
        ("Rule of Rose PS2 CIB authentic black label", "Original US release."),
        ("Grand Theft Auto San Andreas PS2 greatest hits disc only", "Works."),
        ("Silent Hill 2 PS2 complete black label", "Disc, case, manual, mint."),
        ("Kuon PS2 disc only", ""),
    ]:
        target = pipeline.resolve(title, desc)
        print(f"\n{title}")
        print(f"  stage1: {target.verdict.value:<8} {target.sku()}")
        val = value_target(target, src)
        if val is None:
            print("  stage2: no valuation (unidentified)")
            continue
        for line in val.report().splitlines()[1:]:
            print(f"  {line}")


if __name__ == "__main__":
    fails = main()
    demo()
    raise SystemExit(1 if fails else 0)
