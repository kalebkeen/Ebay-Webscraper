"""
test_phash.py — offline cover matching. Stdlib only, no Pillow, no vault.

Two things are being protected here.

The first is the ABSTENTION contract. This matcher runs when nothing else can,
so there is no second opinion to catch it being wrong; a confident wrong title
feeds a bad price straight into a buy decision. It must refuse to answer when
the nearest cover is too far away, and — the case that actually bites — when
two DIFFERENT titles are both close, which is routine for sequels that share
box art.

The second is the ALGORITHM ITSELF. The hash is computed in JavaScript on the
phone and compared against hashes computed by Pillow on the desktop. Nothing
raises if those two drift apart; offline matching just quietly stops working.
So the known-answer test below pins the exact bit layout, and the JS in
static/index.html must keep reproducing it.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import phash_index

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def flip(hex_hash: str, bits: int) -> str:
    """The same hash with `bits` low bits flipped — a stand-in for a photo taken
    at a slightly different angle."""
    v = int(hex_hash, 16)
    for i in range(bits):
        v ^= (1 << i)
    return f"{v:016x}"


ICO = "f0e1d2c3b4a59687"
OKAMI = "0f1e2d3c4b5a6978"          # far from ICO


def main() -> int:
    # ---------------------------------------------------- the portable core
    # A known-answer test on the box-average hash. This is the contract the
    # JavaScript has to meet; if this changes, coverHash() in index.html is
    # wrong too. Built without Pillow so the suite stays stdlib-only.
    W, H, CELL = 9, 8, 16
    w, h = W * CELL, H * CELL
    # A left-to-right ramp: every cell is brighter than the one left of it, so
    # every comparison is "left > right" == False -> all bits 0.
    ramp = bytearray()
    for y in range(h):
        for x in range(w):
            v = (x * 255) // (w - 1)
            ramp += bytes((v, v, v, 255))
    check("ramp hashes to all zero bits",
          _boxhash_rgba_reference(ramp, w, h) == "0" * 16)

    # Reverse it and every comparison flips to True -> all 64 bits set.
    rev = bytearray()
    for y in range(h):
        for x in range(w):
            v = 255 - ((x * 255) // (w - 1))
            rev += bytes((v, v, v, 255))
    check("reversed ramp hashes to all one bits",
          _boxhash_rgba_reference(rev, w, h) == "f" * 16)

    # ---------------------------------------------------- store + load
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "phash_index.json"
        idx = phash_index.PhashIndex(path)
        check("missing file loads empty", idx.count() == 0)
        check("empty index abstains",
              idx.match(ICO)["matched"] is None
              and idx.match(ICO)["reason"] == "empty")

        n = idx.replace([{"h": ICO, "t": "Ico", "v": "black_label"},
                         {"h": OKAMI, "t": "Okami", "v": "unknown"}])
        check("replace stores both", n == 2 and idx.count() == 2)
        check("persisted to disk", path.is_file())
        check("survives a fresh instance",
              phash_index.PhashIndex(path).count() == 2)

        # A blank pull must never wipe a good index — losing it silently would
        # only show up when you were somewhere with no signal.
        keep = phash_index.PhashIndex(path)
        check("empty replace is refused", keep.replace([]) == 0
              and keep.count() == 2)
        check("malformed rows are skipped",
              phash_index.PhashIndex(path).replace(
                  [{"h": "nothex", "t": "Bad"}, {"t": "No hash"},
                   {"h": ICO, "t": "Ico"}]) == 1)

    # ---------------------------------------------------- matching
    idx = phash_index.PhashIndex(Path(tempfile.gettempdir()) / "_nope.json")
    idx.replace([{"h": ICO, "t": "Ico", "v": "black_label"},
                 {"h": OKAMI, "t": "Okami", "v": "unknown"}])

    r = idx.match(ICO)
    check("exact hash matches", r["matched"] and r["matched"]["title"] == "Ico")
    check("carries the variant through",
          r["matched"]["variant"] == "black_label")
    check("exact match is distance 0", r["matched"]["distance"] == 0)

    r = idx.match(flip(ICO, 6))
    check("a near photo still matches (6 bits)",
          r["matched"] and r["matched"]["title"] == "Ico")

    r = idx.match(flip(ICO, 20), cutoff=16)
    check("too far abstains", r["matched"] is None and r["reason"] == "far")
    check("reports how close it got", r.get("best_distance") == 20)

    r = idx.match(flip(ICO, 20), cutoff=24)
    check("a looser cutoff accepts it",
          r["matched"] and r["matched"]["title"] == "Ico")

    check("garbage hash abstains",
          idx.match("zzz")["reason"] == "no_hash")
    check("empty hash abstains", idx.match("")["reason"] == "no_hash")

    # ------------------------------------------- the sequel guard (the point)
    # Two different titles with near-identical art, exactly like the real
    # 'Air Ranger' / 'Air Ranger 2' pair in the vault.
    twin = phash_index.PhashIndex(Path(tempfile.gettempdir()) / "_nope2.json")
    twin.replace([{"h": ICO, "t": "Air Ranger"},
                  {"h": flip(ICO, 2), "t": "Air Ranger 2"}])
    r = twin.match(ICO, cutoff=16, margin=3)
    check("abstains between two near-identical titles",
          r["matched"] is None and r["reason"] == "ambiguous")
    check("ambiguity reports both distances",
          r.get("best_distance") == 0 and r.get("rival_distance") == 2)
    r = twin.match(ICO, cutoff=16, margin=0)
    check("margin 0 disables the guard (nearest wins)",
          r["matched"] and r["matched"]["title"] == "Air Ranger")

    # Several photos of the SAME title must corroborate, not compete.
    multi = phash_index.PhashIndex(Path(tempfile.gettempdir()) / "_nope3.json")
    multi.replace([{"h": ICO, "t": "Ico"},
                   {"h": flip(ICO, 3), "t": "Ico"},
                   {"h": OKAMI, "t": "Okami"}])
    r = multi.match(ICO, cutoff=16, margin=3)
    check("duplicate titles do not trigger the ambiguity guard",
          r["matched"] and r["matched"]["title"] == "Ico")

    check("stats counts covers and distinct titles",
          multi.stats() == {"covers": 3, "titles": 2})

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


def _boxhash_rgba_reference(px, w: int, h: int) -> str:
    """A local copy of vault._boxhash_rgba.

    Duplicated on purpose: vault.py is desktop-only and importing it would drag
    in Pillow, but this algorithm is the one thing the phone and the desktop
    MUST agree on, so it deserves a test that runs everywhere. If this and
    vault._boxhash_rgba ever disagree, one of them is the bug."""
    W, H = 9, 8
    n = W * H
    total = [0] * n
    count = [0] * n
    for y in range(h):
        gy = (y * H) // h
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            k = gy * W + ((x * W) // w)
            total[k] += px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114
            count[k] += 1
    cell = [(total[k] // count[k] if count[k] else 0) for k in range(n)]
    bits = 0
    for r in range(H):
        for c in range(W - 1):
            k = r * W + c
            bits = (bits << 1) | (1 if cell[k] > cell[k + 1] else 0)
    return f"{bits:016x}"


if __name__ == "__main__":
    raise SystemExit(main())
