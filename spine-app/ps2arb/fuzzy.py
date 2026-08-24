"""
fuzzy.py — pure-Python stand-in for the two rapidfuzz calls we used.

rapidfuzz is a C extension. Chaquopy has no wheel for it, and every native
dependency is a build risk inside an APK, so this reimplements exactly the
one scorer catalog.py needs and nothing else.

`token_set_ratio` is not a generic string similarity. It is specifically
designed for the case we have: a short canonical title buried in a long,
noisy listing title. The algorithm splits both strings into token sets and
compares three constructed strings:

    intersection
    intersection + query-only tokens
    intersection + candidate-only tokens

then takes the best ratio of the three. The consequence that matters here is
that a candidate which is a strict SUBSET of the query scores 100 — "god of
war" inside "god of war ps2 black label complete tested" is a perfect match,
not a partial one. catalog.py relies on that, and separately penalises
over-long candidates to stop "ico" sailing into a longer title.

Verified against rapidfuzz across the catalogue before rapidfuzz was
dropped; see test_fuzzy_parity.py.
"""

from __future__ import annotations

def _lcs_len(a: str, b: str) -> int:
    """Longest common subsequence length, two-row DP."""
    if len(a) < len(b):
        a, b = b, a
    n = len(b)
    prev = [0] * (n + 1)
    for ca in a:
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            if ca == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = prev[j] if prev[j] >= cur[j - 1] else cur[j - 1]
        prev = cur
    return prev[n]


def _ratio(a: str, b: str) -> float:
    """Normalised Indel similarity, 0-100. This is rapidfuzz's `fuzz.ratio`.

    Indel distance is len(a) + len(b) - 2*LCS, so the normalised similarity
    reduces to 200*LCS/(len(a)+len(b)).

    An earlier version used difflib.SequenceMatcher.ratio(), which looks
    equivalent and is not: difflib counts matching contiguous BLOCKS via a
    recursive longest-block search, which is bounded above by the LCS and
    usually well below it. Scores came out systematically low -- up to 31
    points -- and low scores in this scorer mean listings resolve to the
    wrong game. Verified against rapidfuzz in test_fuzzy_parity.py.
    """
    if not a and not b:
        return 100.0
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    total = len(a) + len(b)
    # Cheap upper bound: LCS cannot exceed the shorter string.
    if 200.0 * min(len(a), len(b)) / total < 1.0:
        return 0.0
    return 200.0 * _lcs_len(a, b) / total


def token_set_ratio(query: str, choice: str) -> float:
    """rapidfuzz.fuzz.token_set_ratio, reimplemented."""
    q_tokens = set(query.split())
    c_tokens = set(choice.split())
    if not q_tokens and not c_tokens:
        return 100.0
    if not q_tokens or not c_tokens:
        return 0.0

    shared = q_tokens & c_tokens
    q_only = q_tokens - c_tokens
    c_only = c_tokens - q_tokens

    # Sorted joins, exactly as rapidfuzz builds them.
    s_shared = " ".join(sorted(shared))
    s_q = (s_shared + " " + " ".join(sorted(q_only))).strip()
    s_c = (s_shared + " " + " ".join(sorted(c_only))).strip()

    # When one side's tokens are a subset of the other's, s_shared equals
    # that side's full string and the comparison returns 100. That is the
    # defining behaviour of this scorer, not an edge case.
    return max(_ratio(s_shared, s_q),
               _ratio(s_shared, s_c),
               _ratio(s_q, s_c))


def extract(query: str, choices, scorer=token_set_ratio, limit: int | None = None):
    """rapidfuzz.process.extract, reduced to what catalog.py uses.

    Returns (choice, score, index) triples sorted by descending score.
    Ties break on the original order so results stay deterministic across
    runs — important, because the caller walks this list and stops at the
    first candidate passing the sequel-number check.
    """
    scored = [(c, scorer(query, c), i) for i, c in enumerate(choices)]
    scored.sort(key=lambda t: (-t[1], t[2]))
    return scored if limit is None else scored[:limit]
