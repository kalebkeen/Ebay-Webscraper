"""
PS2 listing parser — extracts the fields that actually determine value.

Design principles:
  1. UNKNOWN is never free. If we can't confirm a variant, we assume the
     cheap one. Defaulting optimistically is how arbitrage loses money.
  2. Every extraction carries evidence (the substring that triggered it),
     so a human can audit any decision the pipeline makes.
  3. Blocking flags override price signals entirely. A 90% discount on a
     reproduction disc is not a deal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------- enums


class Variant(Enum):
    BLACK_LABEL = "black_label"        # original retail release
    GREATEST_HITS = "greatest_hits"    # NTSC-U budget reprint
    PLATINUM = "platinum"              # PAL budget reprint
    THE_BEST = "the_best"              # NTSC-J budget reprint
    COLLECTORS = "collectors"          # LE / CE / special edition
    DEMO = "demo"                      # demo / NFR / promo
    UNKNOWN = "unknown"


class Region(Enum):
    NTSC_U = "ntsc_u"
    PAL = "pal"
    NTSC_J = "ntsc_j"
    UNKNOWN = "unknown"


class Completeness(Enum):
    SEALED = "sealed"
    CIB = "cib"                # disc + case + manual
    DISC_CASE = "disc_case"    # disc + case, no manual
    LOOSE = "loose"            # disc only
    NO_DISC = "no_disc"        # case and/or manual only — not a game
    UNKNOWN = "unknown"


class Severity(Enum):
    BLOCKING = "blocking"   # reject outright
    MAJOR = "major"         # manual review, heavy discount
    MINOR = "minor"         # small discount


class Verdict(Enum):
    REJECT = "reject"
    REVIEW = "review"
    PROCEED = "proceed"


# ---------------------------------------------------------------- results


@dataclass
class Flag:
    name: str
    severity: Severity
    evidence: str

    def __repr__(self) -> str:
        return f"{self.name}({self.severity.value})"


@dataclass
class Extraction:
    """A single field value plus why we believe it."""
    value: object
    confidence: float          # 0.0 - 1.0
    evidence: str = ""

    def __repr__(self) -> str:
        v = self.value.value if isinstance(self.value, Enum) else self.value
        return f"{v}@{self.confidence:.2f}"


@dataclass
class ParsedListing:
    raw_title: str
    variant: Extraction
    region: Extraction
    completeness: Extraction
    flags: list[Flag] = field(default_factory=list)
    is_lot: bool = False
    lot_size: int | None = None

    @property
    def blocking(self) -> list[Flag]:
        return [f for f in self.flags if f.severity is Severity.BLOCKING]

    @property
    def major(self) -> list[Flag]:
        return [f for f in self.flags if f.severity is Severity.MAJOR]

    def verdict(self) -> Verdict:
        if self.blocking:
            return Verdict.REJECT
        # Two or more major flags on a single game means the listing carries
        # no usable information. On lots, UNTESTED is expected and does not
        # count toward the stack.
        majors = self.major
        if self.is_lot:
            majors = [f for f in majors if f.name != "UNTESTED"]
        if len(majors) >= 2:
            return Verdict.REJECT
        if majors or self.is_lot:
            return Verdict.REVIEW
        if self.variant.confidence < 0.5 or self.completeness.confidence < 0.5:
            return Verdict.REVIEW
        return Verdict.PROCEED

    def pricing_variant(self, has_budget_reprint: bool | None = None) -> Variant:
        """
        The variant to price against — NOT the detected one.

        If we couldn't confirm the variant, we price as the cheapest
        plausible option. A listing that turns out to be Black Label is a
        pleasant surprise; one you assumed was Black Label and isn't costs
        you 3-5x.

        `has_budget_reprint` comes from the catalog and closes the obvious
        hole in that rule: pessimism is only justified when the cheap
        variant actually exists. Rule of Rose, Kuon and Haunting Ground
        never had a Greatest Hits run, so defaulting them to budget prices
        the pipeline out of the highest-value titles in the catalog. Pass
        None when the title is unmatched and the pessimistic default holds.
        """
        confirmed = (self.variant.value is not Variant.UNKNOWN
                     and self.variant.confidence >= 0.6)
        if confirmed:
            return self.variant.value

        if has_budget_reprint is False:
            # Only one variant was ever pressed for this region.
            if self.region.value is Region.PAL:
                return Variant.UNKNOWN     # PAL originals have no single name
            return Variant.BLACK_LABEL

        if self.region.value is Region.PAL:
            return Variant.PLATINUM
        if self.region.value is Region.NTSC_J:
            return Variant.THE_BEST
        return Variant.GREATEST_HITS

    def pricing_completeness(self) -> Completeness:
        """Same conservatism: unconfirmed completeness prices as loose."""
        if (self.completeness.value is Completeness.UNKNOWN
                or self.completeness.confidence < 0.6):
            return Completeness.LOOSE
        return self.completeness.value


# ---------------------------------------------------------------- matching


# Phrases that look like defects but are region notes. Checked before the
# defect scan so a PAL disclaimer cannot masquerade as a broken disc.
REGION_NOTE = re.compile(
    r"\b(will |won'?t |does ?n'?t |cannot |can'?t )?"
    r"(not )?(play|work|boot|run)s? (on|with|in) "
    r"(a |an |the )?(us|usa|american|north american|ntsc|pal|european|uk|"
    r"japanese|jp|unmodded|stock|non[ -]?modded|standard) "
    r"(consoles?|systems?|ps2s?|machines?|region)?"
)

NEGATORS = (
    "no", "not", "non", "without", "free of", "zero", "never", "doesnt",
    "doesn t", "does not", "isnt", "is not", "hasnt", "has not", "minimal",
    "aside from", "other than", "except",
)

# How far back to look for a negator, in characters.
_NEG_WINDOW = 22


def normalize(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9#/+&']+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _negated(text: str, start: int) -> bool:
    """True if a negation cue appears shortly before position `start`."""
    window = text[max(0, start - _NEG_WINDOW):start]
    return any(re.search(rf"\b{re.escape(neg)}\b", window) for neg in NEGATORS)


def find(pattern: str, text: str, respect_negation: bool = False) -> str | None:
    """Return the matched substring, or None. Optionally skip negated hits."""
    for m in re.finditer(pattern, text):
        if respect_negation and _negated(text, m.start()):
            continue
        return m.group(0)
    return None


# ---------------------------------------------------------------- patterns

VARIANT_PATTERNS: list[tuple[Variant, str, float]] = [
    (Variant.DEMO,          r"\b(demo disc|demo|not for resale|nfr|promo(tional)? copy|kiosk|press kit|review copy)\b", 0.95),
    (Variant.GREATEST_HITS, r"\b(greatest hits|greatst hits|greates hits|gh label)\b", 0.95),
    (Variant.PLATINUM,      r"\b(platinum( range)?|essentials)\b", 0.85),
    # "The Best" alone is handled after region extraction -- see parse().
    # Punctuation is stripped during normalisation, so "PS2 - the best
    # racing game" collapses to "ps2 the best" and any adjacency guard on
    # the platform name is defeated. Only unambiguous line names live here.
    (Variant.THE_BEST,      r"\b(mega ?hits|superlite|playstation 2 the best)\b", 0.85),
    (Variant.COLLECTORS,    r"\b(collector'?s? edition|limited edition|special edition|steelbook|premium edition)\b", 0.80),
    (Variant.BLACK_LABEL,   r"\b(black label|blacklabel|original release|first print|1st print|not greatest hits)\b", 0.90),
]

REGION_PATTERNS: list[tuple[Region, str, float]] = [
    (Region.NTSC_J, r"\b(ntsc[ /-]?j|japan(ese)?( import| version)?|jp import|jpn)\b", 0.90),
    (Region.PAL,    r"\b(pal|uk|europe(an)?|australia(n)?|aus|eur import)\b", 0.85),
    (Region.NTSC_U, r"\b(ntsc[ /-]?u|us(a)? version|north america(n)?|na region)\b", 0.85),
]

# Order matters: NO_DISC checks must run before positive completeness.
COMPLETENESS_PATTERNS: list[tuple[Completeness, str, float]] = [
    (Completeness.NO_DISC,   r"\b(case only|manual only|artwork only|insert only|empty case|no disc|disc missing|missing disc|case and manual only)\b", 0.95),
    (Completeness.SEALED,    r"\b(factory sealed|brand new sealed|sealed brand new|still sealed|new sealed|shrink ?wrap(ped)?|y ?fold|sealed)\b", 0.85),
    (Completeness.CIB,       r"\b(cib|complete in box|complete w(ith)? manual|complete with manual|disc case (and|&|\+) manual|game case manual|100% complete|complete(?! edition| collection| set)\b)", 0.75),
    (Completeness.LOOSE,     r"\b(disc only|disk only|game only|loose|no case( or manual)?|cartridge only|unboxed)\b", 0.90),
    (Completeness.DISC_CASE, r"\b(disc (and|&|\+) case|game (and|&|\+) case|with case|w/ case)\b", 0.70),
]

FLAG_PATTERNS: list[tuple[str, Severity, str, bool]] = [
    # name, severity, pattern, respect_negation
    ("NOT_WORKING",     Severity.BLOCKING, r"\b(not working|doesn'?t work|does not work|won'?t (load|read|play|boot)|will not (load|read|play|boot)|dead disc|non[ -]?working|defective|broken|unplayable|freezes)\b", True),
    ("PARTS_ONLY",      Severity.BLOCKING, r"\b(for parts|parts only|parts or repair|spares or repair|junk|salvage)\b", True),
    ("REPRODUCTION",    Severity.BLOCKING, r"\b(repro(duction)?|bootleg|burn(ed|t)|backup copy|copy of the game|custom (case|cover|print)|home ?made|dvd-?r|fan (made|print)|counterfeit|fake)\b", True),
    ("NOT_AUTHENTIC",   Severity.BLOCKING, r"\b(not (an? )?(original|authentic|genuine|official)|unofficial release)\b", False),
    ("DISC_ROT",        Severity.BLOCKING, r"\b(disc rot|rotting|delamination|pinhol(e|ing)|bronzing)\b", True),
    ("CRACKED_DISC",    Severity.BLOCKING, r"\b(cracked (disc|hub)|hub crack|disc crack|chipped disc|snapped|hairline crack|cracks?(ed)? (near|by|at|around|in)( the)? (centre|center|middle|inner|hub|spindle))\b", True),

    ("UNTESTED",        Severity.MAJOR,    r"\b(untested|not tested|unable to test|cannot test|can'?t test|no way to test|sold as is untested)\b", False),
    ("AS_IS",           Severity.MAJOR,    r"\b(as[ -]?is|sold as seen|no returns|final sale)\b", False),
    ("READ_DESCRIPTION",Severity.MAJOR,    r"\b(read (the )?(full )?description|see description|read below|please read|read carefully)\b", False),
    ("HEAVY_WEAR",      Severity.MAJOR,    r"\b(heav(y|ily) (scratch|scuff|worn)|deep scratch|badly scratched|very scratched|lots of scratches|poor condition|rough shape|water damage|mold|mould)\b", True),
    ("RESEALED",        Severity.MAJOR,    r"\b(re[ -]?seal(ed)?|reshrink|re[ -]?wrapped|possibly resealed)\b", False),
    ("STOCK_PHOTO",     Severity.MAJOR,    r"\b(stock (photo|image)|photo for reference|image for illustration|actual item may (differ|vary)|random copy|you will receive a copy)\b", False),
    ("PLAYED_UNSURE",   Severity.MAJOR,    r"\b(unsure if (it )?works|don'?t know if it works|no console to test|haven'?t tested)\b", False),

    ("RESURFACED",      Severity.MINOR,    r"\b(resurfac(ed|ing)|buffed|polished|professionally cleaned)\b", False),
    ("LIGHT_WEAR",      Severity.MINOR,    r"\b(light (scratch|scuff|wear)|minor (scratch|scuff|wear)|few scratches|some scratches|surface scratch)\b", True),
    ("NO_MANUAL",       Severity.MINOR,    r"\b(no manual|missing (the )?manual|without (the )?manual|manual (is |was )?(not included|missing|gone|lost)|no instructions?|lacks (the )?manual)\b", False),
    ("GENERIC_CASE",    Severity.MINOR,    r"\b(generic case|replacement case|new case|blockbuster|rental|ex[ -]?rental|library copy)\b", False),
    ("WRITING",         Severity.MINOR,    r"\b(writing on|marker|sharpie|name written|initials|sticker residue)\b", True),
]

LOT_PATTERNS = [
    r"\blot of (\d+)\b",
    r"\b(\d+) game lot\b",
    r"\b(\d+)x? games?\b",
    r"\bbundle of (\d+)\b",
    r"\b(\d+) disc lot\b",
]
LOT_GENERIC = (
    r"\b(job ?lot|wholesale|bulk lot|games? lot|ps2 lot|disc lot"
    r"|lot of (games|ps2|discs|video ?games|titles)"
    r"|collection of games|bundle)\b"
)


# ---------------------------------------------------------------- parser


def parse(title: str, description: str = "") -> ParsedListing:
    """
    Parse a listing. Title is weighted higher than description because
    sellers are more careful with titles and eBay truncates descriptions.
    """
    t = normalize(title)
    d = normalize(description)
    both = f"{t} . {d}"

    variant = _extract(VARIANT_PATTERNS, t, d, Variant.UNKNOWN)
    region = _extract(REGION_PATTERNS, t, d, Region.UNKNOWN)
    completeness = _extract(COMPLETENESS_PATTERNS, t, d, Completeness.UNKNOWN,
                            pessimism=COMPLETENESS_PESSIMISM)

    # Region inference: budget-line names are region-locked.
    if region.value is Region.UNKNOWN:
        if variant.value is Variant.GREATEST_HITS:
            region = Extraction(Region.NTSC_U, 0.75, "inferred from Greatest Hits")
        elif variant.value is Variant.PLATINUM:
            region = Extraction(Region.PAL, 0.75, "inferred from Platinum")
        elif variant.value is Variant.THE_BEST:
            region = Extraction(Region.NTSC_J, 0.70, "inferred from The Best")

    # "The Best" is a Japanese budget line, but it is also the single most
    # common phrase in enthusiastic seller copy. Only read it as a variant
    # when the listing is independently identified as Japanese.
    if (variant.value is Variant.UNKNOWN
            and region.value is Region.NTSC_J
            and re.search(r"\bthe best\b", both)):
        variant = Extraction(Variant.THE_BEST, 0.75, "the best + JP region")

    # Blank out region-compatibility disclaimers before scanning for defects.
    # "will not play on US consoles" describes a region lock, not a fault.
    scan_text = REGION_NOTE.sub(" region_note ", both)

    flags: list[Flag] = []
    for name, severity, pattern, respect_neg in FLAG_PATTERNS:
        hit = find(pattern, scan_text, respect_negation=respect_neg)
        if hit:
            flags.append(Flag(name, severity, hit))

    # A "case only" listing is not a game — promote to blocking.
    if completeness.value is Completeness.NO_DISC:
        flags.append(Flag("NO_DISC", Severity.BLOCKING, completeness.evidence))

    # Sealed + resealed is a fraud pattern, not a condition note.
    if completeness.value is Completeness.SEALED and any(f.name == "RESEALED" for f in flags):
        completeness = Extraction(Completeness.UNKNOWN, 0.2, "sealed claim contradicted by reseal language")

    # "Complete" in the title, "manual is missing" in the description. The
    # title is marketing; the description is where the truth gets buried.
    # Downgrade rather than reject — disc+case is still a sellable item.
    if completeness.value is Completeness.CIB and any(f.name == "NO_MANUAL" for f in flags):
        completeness = Extraction(
            Completeness.DISC_CASE,
            round(completeness.confidence * 0.8, 2),
            "CIB claim downgraded: manual stated missing",
        )

    is_lot, lot_size = _detect_lot(both)

    return ParsedListing(
        raw_title=title,
        variant=variant,
        region=region,
        completeness=completeness,
        flags=flags,
        is_lot=is_lot,
        lot_size=lot_size,
    )


# Worst-first ordering, used to break confidence ties. A listing that says
# both "complete" and "disc only" is a disc-only listing: the specific claim
# is the true one, and in any case the pessimistic read is the safe buy.
COMPLETENESS_PESSIMISM = [
    Completeness.NO_DISC,
    Completeness.LOOSE,
    Completeness.DISC_CASE,
    Completeness.CIB,
    Completeness.SEALED,
]


def _extract(patterns, title_norm: str, desc_norm: str, default,
             pessimism: list | None = None) -> Extraction:
    """
    Score every pattern against title and description, then take the
    strongest signal — not the first one in list order.

    First-match-wins made pattern ordering load-bearing in a way that was
    impossible to reason about: a vague 0.75 rule listed above a specific
    0.90 rule silently won. Ranking by confidence makes the table
    order-independent, so adding a rule can't break an unrelated one.

    Title hits keep full confidence; description hits are discounted to 85%
    because titles are what the seller commits to up front.
    """
    candidates: list[tuple[object, float, str]] = []
    for value, pattern, conf in patterns:
        hit = find(pattern, title_norm)
        if hit:
            candidates.append((value, conf, hit))
            continue
        hit = find(pattern, desc_norm)
        if hit:
            candidates.append((value, round(conf * 0.85, 3), hit))

    if not candidates:
        return Extraction(default, 0.0, "")

    def rank(c):
        value, conf, _ = c
        worst = pessimism.index(value) if pessimism and value in pessimism else 99
        return (-conf, worst)

    candidates.sort(key=rank)
    value, conf, evidence = candidates[0]

    # A competing value of comparable strength means the listing contradicts
    # itself. Keep the pessimistic winner but discount confidence, which
    # pushes the listing toward REVIEW instead of PROCEED.
    rivals = [c for c in candidates[1:]
              if c[0] is not value and c[1] >= conf - 0.15]
    if rivals:
        conf = round(conf * 0.7, 2)
        evidence = f"{evidence} (contested by '{rivals[0][2]}')"

    return Extraction(value, conf, evidence)


def _detect_lot(text: str) -> tuple[bool, int | None]:
    for pattern in LOT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            n = int(m.group(1))
            if n > 1:
                return True, n
    if re.search(LOT_GENERIC, text):
        return True, None
    return False, None
