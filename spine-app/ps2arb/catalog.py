"""
Canonical PS2 catalog + title matcher.

Two tiers:
  * a hand-researched seed (`_SEED`, ~30 titles) with verified
    has_greatest_hits / liquidity / repro_risk, and
  * the full title list from Wikipedia (`catalog_data.BULK`, ~4,200 titles
    across NA/EU/JP), appended by `_load_bulk()` with *safe defaults* — see
    that function for why has_greatest_hits defaults to True for these.

`curated` distinguishes the two. A future pass should replace the bulk
defaults with real data: Redump (definitive disc dumps: serials + region) and
PriceCharting IDs for pricing and per-title Greatest Hits status. Wikipedia
gives titles, regions and alternate-name aliases but NOT barcodes/UPCs, disc
serials, or prices — those still have to come from elsewhere.

The `has_greatest_hits` field is load-bearing: if a title never had a budget
reprint, an unconfirmed variant is low-risk. If it did, unconfirmed means
assume budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fuzzy

import sequel
from listing_parser import normalize

# Words that appear in nearly every listing and destroy match quality.
STOPWORDS = {
    "ps2", "ps", "playstation", "play", "station", "sony",
    "game", "games", "video", "disc", "disk", "cib", "complete", "loose",
    "tested", "working", "authentic", "original", "rare", "htf", "oop",
    "mint", "vg", "good", "excellent", "condition", "free", "shipping",
    "fast", "ship", "us", "usa", "new", "used", "greatest", "hits",
    "black", "label", "platinum", "manual", "case", "no", "with", "w",
    "and", "the", "a", "an", "for", "in", "of", "lot", "bundle", "vintage",
    "retro", "collection", "pal", "ntsc", "region", "import", "japan",
}


@dataclass
class Title:
    canonical: str
    aliases: list[str] = field(default_factory=list)
    has_greatest_hits: bool = False
    # Rough liquidity tier: how many sell per month on eBay (NTSC-U, loose).
    # 'high' = 20+, 'medium' = 5-20, 'low' = 1-5, 'thin' = <1
    liquidity: str = "medium"
    repro_risk: str = "low"   # low | medium | high
    # Regions the title released in, subset of {"NA", "EU", "JP"}.
    regions: list[str] = field(default_factory=lambda: ["NA"])
    # True for the hand-researched seed below; False for bulk Wikipedia imports
    # whose has_greatest_hits / liquidity / repro_risk are safe defaults, not
    # verified. Downstream uses this to flag that the economics rest on guesses.
    curated: bool = False

    def search_keys(self) -> list[str]:
        return [self.canonical] + self.aliases


# Hand-researched seed. These have verified has_greatest_hits / liquidity /
# repro_risk; the loop below stamps curated=True on every one. The far larger
# bulk list from Wikipedia is appended afterwards with safe defaults.
_SEED: list[Title] = [
    # --- high-value / high repro risk ---
    Title("Rule of Rose", ["rule of the rose"], False, "thin", "high"),
    Title("Haunting Ground", ["demento"], False, "thin", "high"),
    Title("Kuon", [], False, "thin", "high"),
    Title("Michigan: Report from Hell", ["michigan report from hell"], False, "thin", "high"),
    Title("Persona 3 FES", ["persona 3", "p3 fes", "shin megami tensei persona 3 fes"], False, "low", "high"),
    Title("Suikoden V", ["suikoden 5"], False, "low", "high"),
    Title(".hack//Quarantine", ["hack quarantine", "dot hack quarantine"], False, "thin", "medium"),
    Title("Silent Hill: Shattered Memories", ["shattered memories"], False, "thin", "medium"),
    Title("Fatal Frame III: The Tormented", ["fatal frame 3", "project zero 3", "the tormented"], False, "thin", "high"),

    # --- mid-tier collectible ---
    Title("Silent Hill 2", ["silent hill ii", "sh2"], True, "medium", "medium"),
    Title("Silent Hill 3", ["silent hill iii", "sh3"], False, "low", "medium"),
    Title("Shadow of the Colossus", ["sotc", "shadow of colossus"], True, "high", "low"),
    Title("Ico", [], True, "medium", "low"),
    Title("Okami", [], False, "low", "medium"),
    Title("Katamari Damacy", ["katamari"], True, "medium", "low"),
    Title("We Love Katamari", [], False, "low", "low"),
    Title("Devil May Cry 3: Dante's Awakening", ["dmc3", "devil may cry 3"], True, "medium", "low"),
    Title("God Hand", [], False, "low", "medium"),

    # --- high liquidity commons ---
    Title("Grand Theft Auto: San Andreas", ["gta san andreas", "gta sa", "san andreas"], True, "high", "low"),
    Title("Grand Theft Auto: Vice City", ["gta vice city", "vice city"], True, "high", "low"),
    Title("Grand Theft Auto III", ["gta 3", "gta iii"], True, "high", "low"),
    Title("Kingdom Hearts", ["kh1", "kingdom hearts 1"], True, "high", "low"),
    Title("Kingdom Hearts II", ["kingdom hearts 2", "kh2"], True, "high", "low"),
    Title("Final Fantasy X", ["ffx", "final fantasy 10"], True, "high", "low"),
    Title("Final Fantasy XII", ["ffxii", "final fantasy 12"], True, "high", "low"),
    Title("God of War", ["gow"], True, "high", "low"),
    Title("God of War II", ["god of war 2", "gow2"], True, "high", "low"),
    Title("Metal Gear Solid 3: Snake Eater", ["mgs3", "snake eater"], True, "medium", "low"),
    Title("Guitar Hero II", ["guitar hero 2", "gh2"], False, "high", "low"),
    Title("Tony Hawk's Pro Skater 4", ["thps4", "tony hawk 4"], True, "high", "low"),
]

for _seed in _SEED:
    _seed.curated = True


def _load_bulk() -> list[Title]:
    """Append the full Wikipedia-sourced title list.

    Every bulk entry is uncurated, so its economics fields take *safe*
    defaults rather than the dataclass defaults:

    - has_greatest_hits=True prices an unconfirmed variant as the cheap budget
      reprint. This is the pessimistic assumption pricing_variant() wants when
      a title is unresearched: guessing Black Label and being wrong overpays
      3-5x, so we never do it for a title we haven't checked. (The dataclass
      default of False would do exactly the wrong thing here.)
    - liquidity is a conservative offline PRIOR carried on each catalog_data
      row (region breadth + known mega-franchises), to be replaced by measured
      eBay data later; see tools/refine_liquidity.py. repro_risk='low' stays a
      placeholder.
    """
    try:
        import catalog_data
    except ImportError:
        return []
    seen = {strip_noise(k) for t in _SEED for k in t.search_keys()}
    bulk: list[Title] = []
    for row in catalog_data.BULK:
        canonical, regions, aliases = row[0], row[1], row[2]
        liquidity = row[3] if len(row) > 3 else "low"
        if strip_noise(canonical) in seen:
            continue
        bulk.append(Title(
            canonical=canonical,
            aliases=list(aliases),
            has_greatest_hits=True,
            liquidity=liquidity,
            repro_risk="low",
            regions=list(regions),
            curated=False,
        ))
    return bulk


@dataclass
class MatchResult:
    title: Title | None
    score: float          # 0-100
    matched_alias: str = ""

    @property
    def confident(self) -> bool:
        return self.title is not None and self.score >= 88.0


def strip_noise(text: str) -> str:
    """Remove boilerplate so the actual title dominates the comparison.

    Platform phrases go first and as whole phrases, so the "2" in
    "PlayStation 2" is never confused with the "2" in "Silent Hill 2".
    """
    cleaned = sequel.strip_platform(normalize(text))
    tokens = [w for w in cleaned.split() if w not in STOPWORDS]
    return " ".join(tokens)


# Assembled once strip_noise exists (it de-dupes bulk against the seed).
CATALOG: list[Title] = _SEED + _load_bulk()


# Build the lookup once: every alias maps back to its Title.
_INDEX: dict[str, Title] = {}
_KEY_NUM: dict[str, int | None] = {}
for _t in CATALOG:
    for _key in _t.search_keys():
        _clean = strip_noise(_key)
        if not _clean:
            # Title made entirely of stopwords (e.g. "Black", "Retro"). It is
            # unmatchable under the current scheme anyway — a listing for it
            # also strips to empty — so keep it out of the index rather than
            # let one such title shadow another under the "" key.
            continue
        _INDEX[_clean] = _t
        _KEY_NUM[_clean] = sequel.extract(_clean)
_CHOICES = list(_INDEX.keys())

# Token -> candidate keys, so a query is scored only against catalog entries
# that share at least one word with it. With a 30-entry seed the full O(N)
# scan was free; at ~4,200 titles it was ~300 ms/listing, which made the
# backtest and any feed scan intractable. A candidate that shares no token
# with the query scores ~0 on token_set_ratio, so blocking on shared tokens
# cannot drop a real winner; queries that block to nothing fall back to the
# full scan, preserving fuzzy tolerance for the rare all-typo title.
_TOKEN_INDEX: dict[str, list[str]] = {}
for _clean in _CHOICES:
    for _tok in set(_clean.split()):
        _TOKEN_INDEX.setdefault(_tok, []).append(_clean)


def _candidates(cleaned: str) -> list[str]:
    cands: set[str] = set()
    for tok in set(cleaned.split()):
        cands.update(_TOKEN_INDEX.get(tok, ()))
    return list(cands) if cands else _CHOICES


def match(listing_title: str, threshold: float = 80.0) -> MatchResult:
    """
    Fuzzy-match a listing title to the catalog.

    token_set_ratio handles the common cases well: extra words, reordering,
    and subset matches ("God of War" inside a 15-word listing title).
    """
    cleaned = strip_noise(listing_title)
    if not cleaned:
        return MatchResult(None, 0.0)

    q_num = sequel.extract(cleaned)

    # Score everything (within the token-blocked candidate set), then filter on
    # the hard constraint. Filtering after scoring (rather than using
    # score_cutoff alone) means a wrong-numbered 100-scorer cannot crowd out
    # the correct 92-scorer.
    choices = _candidates(cleaned)
    scored = fuzzy.extract(
        cleaned, choices, scorer=fuzzy.token_set_ratio, limit=len(choices)
    )

    best: tuple[str, float] | None = None
    for key, score, _ in scored:
        if score < threshold:
            break
        if not sequel.compatible(q_num, _KEY_NUM[key]):
            continue
        # token_set_ratio ignores extra tokens in the candidate. Penalise
        # candidates that are much longer than the query so "ico" cannot
        # sail into a longer title on a subset match.
        length_gap = abs(len(key.split()) - len(cleaned.split()))
        adjusted = score - min(length_gap * 4.0, 20.0)
        if best is None or adjusted > best[1]:
            best = (key, adjusted)

    if best is None:
        return MatchResult(None, 0.0)
    return MatchResult(_INDEX[best[0]], best[1], best[0])


def ambiguity_check(listing_title: str, margin: float = 6.0) -> list[tuple[str, float]]:
    """
    Return runner-up matches within `margin` of the best score.

    Non-empty output means the match is contested — e.g. 'God of War' vs
    'God of War II', or 'Kingdom Hearts' vs 'Kingdom Hearts II'. These
    numbered-sequel collisions are the single most common way a naive
    matcher prices the wrong game.
    """
    cleaned = strip_noise(listing_title)
    if not cleaned:
        return []
    q_num = sequel.extract(cleaned)
    winner = match(listing_title)

    seen: set[str] = set()
    if winner.title:
        seen.add(winner.title.canonical)

    rivals: list[tuple[str, float]] = []
    choices = _candidates(cleaned)
    for key, score, _ in fuzzy.extract(
        cleaned, choices, scorer=fuzzy.token_set_ratio, limit=len(choices)
    ):
        if score < 70.0:
            break
        if not sequel.compatible(q_num, _KEY_NUM[key]):
            continue
        canon = _INDEX[key].canonical
        if canon in seen:          # same product under an alias, not a rival
            continue
        if winner.score - score > margin:
            continue
        seen.add(canon)
        rivals.append((canon, score))
    return rivals
