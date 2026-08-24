"""
Test corpus of realistic PS2 listing titles.

These are written to mirror how sellers actually title things: inconsistent
capitalisation, jammed keywords, abbreviations, and defects buried at the
end. Grow this file every time the parser gets something wrong in the wild —
it is the regression suite that keeps the rules honest.
"""

import catalog
import listing_parser as lp
from listing_parser import Completeness, Region, Variant, Verdict

# (title, description, expected_variant, expected_completeness, expected_verdict)
CASES = [
    (
        "Grand Theft Auto San Andreas (Sony PlayStation 2, 2004) PS2 Greatest Hits Complete",
        "Disc, case and manual all included. Tested and works great.",
        Variant.GREATEST_HITS, Completeness.CIB, Verdict.PROCEED,
    ),
    (
        "Rule of Rose PS2 CIB Authentic Black Label RARE Survival Horror",
        "Original US release, not a reproduction. Disc is near mint.",
        Variant.BLACK_LABEL, Completeness.CIB, Verdict.PROCEED,
    ),
    (
        "Kingdom Hearts PS2 Disc Only Tested Working",
        "",
        Variant.UNKNOWN, Completeness.LOOSE, Verdict.REVIEW,
    ),
    (
        "PS2 Lot of 12 Games - Untested - As Is",
        "Found in storage unit, no way to test. Sold as-is.",
        Variant.UNKNOWN, Completeness.UNKNOWN, Verdict.REVIEW,
    ),
    (
        "Haunting Ground Playstation 2 PAL Version Platinum",
        "European PAL copy, will not play on US consoles.",
        Variant.PLATINUM, Completeness.UNKNOWN, Verdict.REVIEW,
    ),
    (
        "Silent Hill 2 Greatest Hits PS2 Case & Manual Only NO DISC",
        "Please note this is the case and manual only, the disc is missing.",
        Variant.GREATEST_HITS, Completeness.NO_DISC, Verdict.REJECT,
    ),
    (
        "God of War (PlayStation 2) Black Label Complete w/ Manual",
        "No scratches on the disc at all. Plays perfectly.",
        Variant.BLACK_LABEL, Completeness.CIB, Verdict.PROCEED,
    ),
    (
        "Persona 3 FES PS2 Brand New Factory Sealed",
        "Sealed in original shrinkwrap.",
        Variant.UNKNOWN, Completeness.SEALED, Verdict.REVIEW,
    ),
    (
        "Final Fantasy X PS2 Greatest Hits - resurfaced, works",
        "Disc had some scratches, professionally resurfaced. Plays fine now.",
        Variant.GREATEST_HITS, Completeness.UNKNOWN, Verdict.REVIEW,
    ),
    (
        "Okami PS2 Repro Case Custom Cover Art Disc Included",
        "Custom printed case, burned disc backup copy.",
        Variant.UNKNOWN, Completeness.UNKNOWN, Verdict.REJECT,
    ),
    (
        "Kuon PS2 Survival Horror - Won't load, for parts",
        "Disc will not read in my console. Selling for parts or repair.",
        Variant.UNKNOWN, Completeness.UNKNOWN, Verdict.REJECT,
    ),
    (
        "Shadow of the Colossus PS2 Disc Only - light scratches, tested",
        "A few surface scratches but loads and plays without issue.",
        Variant.UNKNOWN, Completeness.LOOSE, Verdict.REVIEW,
    ),
    (
        "Metal Gear Solid 3 Snake Eater PS2 Complete Black Label No Scratches",
        "Disc is flawless, no scratches whatsoever. Includes manual.",
        Variant.BLACK_LABEL, Completeness.CIB, Verdict.PROCEED,
    ),
    (
        "Suikoden V PS2 Sealed - possibly resealed, sold as is",
        "Shrinkwrap looks slightly off, may have been resealed. As-is.",
        Variant.UNKNOWN, Completeness.UNKNOWN, Verdict.REJECT,
    ),
    (
        "Devil May Cry 3 Dante's Awakening PS2 Greatest Hits disc and case",
        "No manual included.",
        Variant.GREATEST_HITS, Completeness.DISC_CASE, Verdict.PROCEED,
    ),
]


def run() -> int:
    failures = 0
    print(f"{'':2} {'TITLE':<58} {'VARIANT':<15} {'COMPLETE':<11} {'VERDICT':<8}")
    print("-" * 100)

    for title, desc, exp_var, exp_comp, exp_verdict in CASES:
        p = lp.parse(title, desc)
        got_verdict = p.verdict()

        ok = (p.variant.value is exp_var
              and p.completeness.value is exp_comp
              and got_verdict is exp_verdict)
        if not ok:
            failures += 1

        mark = "  " if ok else "!!"
        short = title[:56] + ".." if len(title) > 58 else title
        print(f"{mark} {short:<58} {p.variant.value.value:<15} "
              f"{p.completeness.value.value:<11} {got_verdict.value:<8}")

        if not ok:
            if p.variant.value is not exp_var:
                print(f"     -> variant: expected {exp_var.value}")
            if p.completeness.value is not exp_comp:
                print(f"     -> completeness: expected {exp_comp.value}")
            if got_verdict is not exp_verdict:
                print(f"     -> verdict: expected {exp_verdict.value}")
        if p.flags:
            print(f"     flags: {', '.join(repr(f) for f in p.flags)}")

    print("-" * 100)
    print(f"{len(CASES) - failures}/{len(CASES)} passed\n")
    return failures


def show_matching() -> None:
    print("CATALOG MATCHING")
    print("-" * 100)
    samples = [
        "Grand Theft Auto San Andreas (Sony PlayStation 2, 2004) PS2 Greatest Hits Complete",
        "GTA SA ps2 disc only",
        "God of War (PlayStation 2) Black Label Complete w/ Manual",
        "God of War II PS2 CIB tested",
        "Kingdom Hearts PS2 Disc Only Tested Working",
        "Fatal Frame 3 The Tormented PS2 NTSC-U",
        "Some Random Sports Game 2004 PS2",
    ]
    for s in samples:
        m = catalog.match(s)
        name = m.title.canonical if m.title else "NO MATCH"
        flag = "" if m.confident else "  <- LOW CONFIDENCE"
        print(f"  {s[:52]:<54} -> {name:<32} {m.score:5.1f}{flag}")
        amb = catalog.ambiguity_check(s)
        if amb:
            rivals = ", ".join(f"{k} ({s_:.0f})" for k, s_ in amb)
            print(f"     ambiguous with: {rivals}")
    print()


def show_pricing_stance() -> None:
    print("CONSERVATIVE PRICING STANCE (what we value it as, not what it claims)")
    print("-" * 100)
    for title, desc, *_ in CASES[:8]:
        p = lp.parse(title, desc)
        print(f"  {title[:52]:<54} detected={p.variant.value.value:<14} "
              f"price_as={p.pricing_variant().value:<15} {p.pricing_completeness().value}")
    print()


if __name__ == "__main__":
    fails = run()
    show_matching()
    show_pricing_stance()
    raise SystemExit(1 if fails else 0)
