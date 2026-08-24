"""
Adversarial corpus — cases built to break the parser, not confirm it.

test_corpus.py is a regression suite: it locks in behaviour we already got
right. This file is the opposite. Every case here is a phrasing that a naive
rule set gets wrong, drawn from the ways real sellers actually write.

Two failure classes, and they cost differently:

  FALSE ACCEPT  — we buy a repro, a PAL disc, or a Greatest Hits copy priced
                  as black label. Direct cash loss, roughly the spread.
  FALSE REJECT  — we skip a real deal. Opportunity cost only, and with
                  thousands of listings a day, cheap.

So the tuning target is asymmetric: tolerate false rejects, hunt false
accepts. But a *systematic* false reject is still a bug worth fixing, because
it silently removes a whole category from the funnel.
"""

import catalog
import listing_parser as lp
from listing_parser import Completeness, Region, Severity, Variant, Verdict


# (label, title, description, assertion_fn, why_it_matters)
def _no_flag(name):
    return lambda p: not any(f.name == name for f in p.flags)


def _has_flag(name):
    return lambda p: any(f.name == name for f in p.flags)


def _verdict(v):
    return lambda p: p.verdict() is v


def _variant(v):
    return lambda p: p.variant.value is v


def _complete(c):
    return lambda p: p.completeness.value is c


def _region(r):
    return lambda p: p.region.value is r


def _all(*fns):
    return lambda p: all(f(p) for f in fns)


CASES = [
    # ---- negation and disclaimers -------------------------------------
    (
        "authenticity assertion misread as repro",
        "Dark Cloud 2 PS2 100% Authentic Original Not a Reproduction",
        "This is the genuine retail disc, not a burned copy or bootleg.",
        _no_flag("REPRODUCTION"),
        "Sellers of genuine rare discs pre-empt the repro question. "
        "Flagging them removes the exact listings we want.",
    ),
    (
        "denied parts listing",
        "Gran Turismo 4 PS2 - this is NOT for parts, fully tested and working",
        "",
        _no_flag("PARTS_ONLY"),
        "PARTS_ONLY ignores negation, so a denial reads as a confession.",
    ),
    (
        "negated damage in title",
        "Ico PS2 Black Label - no scratches, no cracks, mint disc",
        "",
        _all(_no_flag("HEAVY_WEAR"), _no_flag("CRACKED_DISC")),
        "Condition boasts are phrased as negations more often than not.",
    ),
    (
        "region lock is not a fault",
        "Kuon PS2 NTSC-J Japanese Import",
        "Japanese region disc. Will not work on a standard US console "
        "unless your PS2 is modded.",
        _no_flag("NOT_WORKING"),
        "Import sellers all write this. Reading it as a defect kills the "
        "entire NTSC-J supply.",
    ),

    # ---- title tokens that collide with rule vocabulary ---------------
    (
        "'Le' in a game title",
        "Le Mans 24 Hours PS2 Racing Complete",
        "Disc, case and manual.",
        lambda p: p.variant.value is not Variant.COLLECTORS,
        r"The COLLECTORS pattern includes \ble\b, which fires on any title "
        "containing the French article.",
    ),
    (
        "'the best' as marketing copy",
        "Burnout 3 Takedown PS2 - the best racing game on the system!",
        "Greatest racing game ever made. Disc only.",
        lambda p: p.variant.value is not Variant.THE_BEST,
        "'The Best' is a JP budget line, but it's also a phrase every "
        "enthusiastic seller uses.",
    ),
    (
        "'lot' inside prose",
        "Katamari Damacy PS2 CIB - I bought a lot of these and am selling "
        "them individually",
        "Single copy, complete, tested.",
        lambda p: not p.is_lot,
        "Generic 'lot' matching turns single-item listings into lots and "
        "routes them off the main pricing path.",
    ),
    (
        "'complete' as part of an edition name",
        "Ace Combat 4 PS2 Shattered Skies Complete Edition disc only",
        "Disc only, no case or manual.",
        _complete(Completeness.LOOSE),
        "'complete' scores CIB at 0.75 and runs before LOOSE in the "
        "pattern list, so an edition name beats an explicit disc-only claim.",
    ),

    # ---- ordering and contradiction -----------------------------------
    (
        "contradiction: complete then no manual",
        "Final Fantasy XII PS2 Complete",
        "Case and disc are here, manual is missing unfortunately.",
        lambda p: p.completeness.value is not Completeness.CIB,
        "Title says complete, description contradicts. Title-first "
        "extraction takes the seller's optimistic framing.",
    ),
    (
        "defect buried after boilerplate",
        "Silent Hill 3 PS2 Black Label Complete Rare Horror Game",
        "Fast shipping from a smoke-free home. Ships same day. "
        "Please note there is a small crack near the centre hub.",
        _verdict(Verdict.REJECT),
        "The money-losing pattern: clean title, defect in the last "
        "sentence of a long description.",
    ),

    # ---- bare listings ------------------------------------------------
    (
        "no information at all",
        "Okami PS2",
        "",
        _verdict(Verdict.REVIEW),
        "Zero-signal listings must not reach PROCEED on default values.",
    ),

    # ---- variant / region inference -----------------------------------
    (
        "title with no budget reprint priced as budget",
        "Persona 3 FES PS2 Complete with manual, tested",
        "Black label original.",
        lambda p: p.pricing_variant() is not Variant.GREATEST_HITS,
        "P3 FES never had a Greatest Hits run. Defaulting to budget "
        "underprices every high-value title — systematic false reject.",
    ),
    (
        "PAL inferred from Platinum",
        "Ico PS2 Platinum",
        "",
        _region(Region.PAL),
        "Budget-line names are region-locked and should backfill region.",
    ),
]


def run() -> int:
    failures = []
    print(f"{'':3} {'CASE':<44} {'RESULT'}")
    print("-" * 100)

    for label, title, desc, assertion, why in CASES:
        p = lp.parse(title, desc)
        ok = assertion(p)
        if not ok:
            failures.append((label, title, desc, why, p))
        print(f"{'   ' if ok else '!! '}{label:<44} "
              f"variant={p.variant.value.value:<14} "
              f"complete={p.completeness.value.value:<10} "
              f"{p.verdict().value}")

    print("-" * 100)
    print(f"{len(CASES) - len(failures)}/{len(CASES)} passed\n")

    if failures:
        print("=" * 100)
        print("FAILURES")
        print("=" * 100)
        for label, title, desc, why, p in failures:
            print(f"\n[{label}]")
            print(f"  title : {title}")
            if desc:
                print(f"  desc  : {desc[:88]}")
            print(f"  got   : variant={p.variant.value.value} "
                  f"region={p.region.value.value} "
                  f"complete={p.completeness.value.value} "
                  f"verdict={p.verdict().value}")
            if p.flags:
                print(f"  flags : {', '.join(f'{f.name}<{f.evidence}>' for f in p.flags)}")
            print(f"  why   : {why}")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
