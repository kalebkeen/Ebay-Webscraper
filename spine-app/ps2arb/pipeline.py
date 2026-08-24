"""
Integration layer: catalog match + listing parse -> one pricing decision.

Keeps the two halves decoupled. `listing_parser` knows nothing about which
games exist; `catalog` knows nothing about condition language. This module
is the only place that needs both, which is also the only place that can
answer the question the comp layer will actually ask:

    "Which exact SKU should I look up a sold price for, and how much do I
     trust that identification?"

Stage 3 (comps) consumes `PricingTarget`. Nothing downstream should ever
touch a raw listing string again.
"""

from __future__ import annotations

from dataclasses import dataclass

import catalog
import listing_parser as lp
from listing_parser import Completeness, Region, Variant, Verdict


@dataclass
class PricingTarget:
    """A resolved SKU plus the confidence and caveats attached to it."""
    raw_title: str
    title: str | None                 # canonical catalog name
    variant: Variant                  # what we will PRICE it as
    completeness: Completeness        # what we will PRICE it as
    region: Region
    verdict: Verdict
    match_score: float
    liquidity: str                    # high | medium | low | thin
    repro_risk: str                   # low | medium | high
    reasons: list[str]
    rivals: list[tuple[str, float]]

    @property
    def priceable(self) -> bool:
        """Only PROCEED with a confident SKU is safe to auto-price."""
        return (self.verdict is Verdict.PROCEED
                and self.title is not None
                and self.match_score >= 88.0)

    def sku(self) -> str | None:
        if self.title is None:
            return None
        return f"{self.title}|{self.region.value}|{self.variant.value}|{self.completeness.value}"


def resolve(title: str, description: str = "") -> PricingTarget:
    parsed = lp.parse(title, description)
    match = catalog.match(title)
    rivals = catalog.ambiguity_check(title)

    entry = match.title
    has_gh = entry.has_greatest_hits if entry else None

    reasons: list[str] = []
    verdict = parsed.verdict()

    for flag in parsed.blocking:
        reasons.append(f"blocking:{flag.name} <{flag.evidence}>")
    for flag in parsed.major:
        reasons.append(f"major:{flag.name} <{flag.evidence}>")

    # A review queue with no stated reason is a queue nobody works. Every
    # non-PROCEED verdict has to say what a human should go look at.
    if parsed.variant.confidence < 0.6:
        if has_gh is False:
            reasons.append("variant unstated (no budget reprint exists — "
                           "priced as original)")
        else:
            reasons.append("variant unstated — priced as budget reprint")
    if parsed.completeness.confidence < 0.6:
        reasons.append("completeness unstated — priced as loose")
    if parsed.region.value is Region.UNKNOWN:
        reasons.append("region unstated — set from seller location at ingest")

    # Identification failures are their own rejection class, separate from
    # condition. We can price a scratched disc; we cannot price a game we
    # cannot name.
    if entry is None:
        reasons.append("no catalog match")
        verdict = Verdict.REJECT
    elif rivals:
        names = ", ".join(f"{n} ({s:.0f})" for n, s in rivals)
        reasons.append(f"contested identity: {names}")
        verdict = Verdict.REVIEW if verdict is Verdict.PROCEED else verdict
    elif not match.confident:
        reasons.append(f"weak title match ({match.score:.0f})")
        verdict = Verdict.REVIEW if verdict is Verdict.PROCEED else verdict

    # A high-repro-risk title with an unverified variant is a manual-review
    # item no matter how clean the listing text reads. Text cannot prove
    # authenticity; only photos can.
    if entry and entry.repro_risk == "high" and verdict is Verdict.PROCEED:
        reasons.append("high repro-risk title — photo verification required")
        verdict = Verdict.REVIEW

    return PricingTarget(
        raw_title=title,
        title=entry.canonical if entry else None,
        variant=parsed.pricing_variant(has_budget_reprint=has_gh),
        completeness=parsed.pricing_completeness(),
        region=parsed.region.value,
        verdict=verdict,
        match_score=match.score,
        liquidity=entry.liquidity if entry else "unknown",
        repro_risk=entry.repro_risk if entry else "unknown",
        reasons=reasons,
        rivals=rivals,
    )


DEMO = [
    ("Rule of Rose PS2 complete tested", ""),
    ("Kuon PS2 CIB", ""),
    ("Grand Theft Auto San Andreas PS2 disc only", "Works fine."),
    ("God of War PS2 Complete Black Label", "Disc, case, manual. Mint."),
    ("Kingdom Hearts PS2 CIB tested working", ""),
    ("Silent Hill 3 PS2 Black Label Complete Rare",
     "Ships fast. Note: small crack near the centre hub."),
    ("Okami PS2 repro case custom cover", "Burned backup disc."),
    ("Some Unknown Sports Title 2004 PS2 CIB", ""),
]


def main() -> None:
    print(f"{'LISTING':<44} {'VERDICT':<8} {'SKU'}")
    print("-" * 108)
    for title, desc in DEMO:
        t = resolve(title, desc)
        sku = t.sku() or "-"
        print(f"{title[:42]:<44} {t.verdict.value:<8} {sku}")
        if t.reasons:
            for r in t.reasons:
                print(f"{'':44} └─ {r}")
        if t.priceable:
            print(f"{'':44}    liquidity={t.liquidity} repro_risk={t.repro_risk}")
    print("-" * 108)
    n = sum(1 for x, d in DEMO if resolve(x, d).priceable)
    print(f"{n}/{len(DEMO)} auto-priceable; the rest need review or are rejected.")


if __name__ == "__main__":
    main()
