"""
Sequel-number extraction.

The most expensive failure mode in fuzzy title matching is collapsing a base
game into its sequel (or vice versa). Substring-tolerant scorers like
token_set_ratio rate "god of war" against "god of war ii" at 100, because
every token in the query appears in the candidate.

The fix is to treat the installment number as a hard constraint rather than
a scoring input: if two titles disagree on their number, they are different
products regardless of how similar the strings look.
"""

from __future__ import annotations

import re

ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
    "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12,
}

WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Platform boilerplate, matched as whole phrases so the trailing "2" in
# "playstation 2" is never mistaken for an installment number.
PLATFORM_PHRASES = [
    r"\bsony play ?station ?2\b",
    r"\bplay ?station ?2\b",
    r"\bplay ?station ?ii\b",
    r"\bps ?2\b",
    r"\bpstwo\b",
    r"\bps ?two\b",
]

# Years, not sequels. "Madden 2004", "(Sony PlayStation 2, 2004)".
YEAR = re.compile(r"\b(19|20)\d{2}\b")


def strip_platform(text: str) -> str:
    """Remove console boilerplate and 4-digit years from normalized text."""
    for phrase in PLATFORM_PHRASES:
        text = re.sub(phrase, " ", text)
    text = YEAR.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract(text: str) -> int | None:
    """
    Pull the installment number from a platform-stripped, normalized title.

    Returns None when the title carries no number, which is itself meaningful:
    a numberless query should not match a numbered candidate.
    """
    tokens = strip_platform(text).split()
    found: list[int] = []

    for tok in tokens:
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= 12:          # installment numbers, not "2004" or "007"
                found.append(n)
        elif tok in ROMAN:
            found.append(ROMAN[tok])
        elif tok in WORD_NUM:
            found.append(WORD_NUM[tok])

    if not found:
        return None
    # Titles like "Devil May Cry 3" put the number last; "Fatal Frame III: The
    # Tormented" also. Trailing number is the more reliable signal.
    return found[-1]


def compatible(query_num: int | None, candidate_num: int | None) -> bool:
    """
    Hard constraint. Two titles are compatible only if their numbers agree,
    or the candidate is numberless.

    Asymmetric on purpose: a listing titled "Kingdom Hearts" must not match
    "Kingdom Hearts II", but a listing titled "Kingdom Hearts 2" matching the
    numberless catalog entry would already have failed the reverse check.
    """
    if query_num == candidate_num:
        return True
    if candidate_num is None:
        # Query says 2, catalog entry has no number: allow, but the caller
        # should treat this as low confidence.
        return query_num is None
    return False
