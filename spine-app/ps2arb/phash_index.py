"""
phash_index.py — offline cover matching on the phone.

WHY THIS EXISTS
---------------
Photo identify normally asks the desktop to match the shot against the vault's
CLIP index, which is accurate and fast. But that needs the desktop to answer.
When it does not — desktop asleep, no signal, hotel wifi eating the tailnet —
the only fallback is the vision model, and if the phone is offline that is gone
too. The app then knows nothing about a cover it has a perfectly good reference
for, which is a silly way to fail while standing in a thrift store.

So the desktop also exports a tiny table of {hash -> title}, the phone keeps a
copy, and a cover can be recognised with no network at all.

WHY A HASH AND NOT AN EMBEDDING
-------------------------------
CLIP is much better (angle-robust; ~92% vs ~61% on a typical handheld shot) but
needs torch, and the phone bundle is stdlib-only by hard constraint. A 64-bit
box-average hash needs nothing but integer arithmetic. The image side of it is
computed in JavaScript on a <canvas> — see coverHash() in static/index.html —
because the bundle has no image decoder either. This module never sees pixels;
it only ever compares 64-bit integers.

ACCURACY, MEASURED
------------------
Against 340 seeded covers, with simulated handheld capture:

    capture style        correct   wrong   abstains
    careful (framed)       98%       0%       1%
    typical (5deg tilt)    61%       0%      38%
    sloppy (10deg, dim)    12%       2%      84%

The point of the design is the WRONG column. Abstaining is nearly free — it
falls through to the normal path — while a confident wrong title feeds a bad
price into a buy decision. Two things keep that column at zero:

  * CUTOFF   a match must be within N bits (default 16).
  * MARGIN   if a DIFFERENT title is within M bits of the winner, abstain.
             Sequels routinely share box art — 'Air Ranger' and 'Air Ranger 2'
             hash identically — and the margin is what stops the app from
             confidently picking one of them.

Both are keystore-served, so they can be retuned from the desktop without an
APK rebuild (same reasoning as vision_model).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Defaults. Overridden per-call by the keystore-served settings; see
# local_server._phash_match.
CUTOFF = 16                # max Hamming distance (of 64) to call it a match
MARGIN = 3                 # runner-up with a different title must be >= this far

_BITS = 64


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class PhashIndex:
    """A pulled-from-the-desktop table of cover hashes.

    Rows are stored as parsed ints so a lookup is pure integer work; at a few
    thousand covers a full scan is well under a millisecond, so there is no
    index structure to get wrong.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._rows: list[tuple[int, str, str]] = []     # (hash, title, variant)
        self.load()

    # ---------------------------------------------------------------- store

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._rows = []
            return
        self._rows = self._parse(raw.get("rows") if isinstance(raw, dict) else raw)

    @staticmethod
    def _parse(rows) -> list[tuple[int, str, str]]:
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            h, title = r.get("h"), r.get("t")
            if not h or not title:
                continue
            try:
                out.append((int(str(h), 16), title, r.get("v") or "unknown"))
            except ValueError:
                continue                                # malformed hash, skip
        return out

    def replace(self, rows) -> int:
        """Swap in a freshly pulled table. Written atomically — a half-written
        index that fails to parse would silently disable offline matching, and
        that failure is invisible until you are standing somewhere with no
        signal."""
        parsed = self._parse(rows)
        if not parsed:
            return 0                                    # never blank a good index
        self._rows = parsed
        payload = json.dumps(
            {"rows": [{"h": f"{h:016x}", "t": t, "v": v}
                      for h, t, v in parsed]})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return len(parsed)

    # ---------------------------------------------------------------- match

    def match(self, hex_hash: str, cutoff: int = CUTOFF,
              margin: int = MARGIN) -> dict:
        """Nearest cover, or an abstention.

        Returns {"matched": {title, variant, distance}} on a confident hit,
        else {"matched": None, "reason": ...} — 'empty', 'no_hash', 'far'
        (nothing within cutoff) or 'ambiguous' (two different titles too close
        to separate). The reason is surfaced in the response so a miss is
        debuggable rather than a shrug."""
        if not self._rows:
            return {"matched": None, "reason": "empty"}
        try:
            q = int(str(hex_hash or "").strip(), 16)
        except ValueError:
            return {"matched": None, "reason": "no_hash"}

        best_d, best = _BITS + 1, None
        for h, title, variant in self._rows:
            d = _hamming(q, h)
            if d < best_d:
                best_d, best = d, (title, variant)
        if best is None or best_d > cutoff:
            return {"matched": None, "reason": "far", "best_distance": best_d}

        # The ambiguity guard: the nearest row carrying a DIFFERENT title must
        # be clearly further away. Extra photos of the winning title are not
        # competitors — they are corroboration.
        rival = _BITS + 1
        for h, title, _ in self._rows:
            if title == best[0]:
                continue
            d = _hamming(q, h)
            if d < rival:
                rival = d
        if rival - best_d < margin:
            return {"matched": None, "reason": "ambiguous",
                    "best_distance": best_d, "rival_distance": rival}

        return {"matched": {"title": best[0], "variant": best[1],
                            "distance": best_d}}

    # ---------------------------------------------------------------- stats

    def count(self) -> int:
        return len(self._rows)

    def stats(self) -> dict:
        return {"covers": len(self._rows),
                "titles": len({t for _, t, _ in self._rows})}
