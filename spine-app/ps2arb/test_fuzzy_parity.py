"""
Parity check: fuzzy.token_set_ratio vs rapidfuzz.

Run this while rapidfuzz is still installed. It is the evidence that
dropping the C extension did not quietly change which game a listing
resolves to -- the failure mode being that a title starts matching a
different game and every price downstream is for the wrong disc.

Once rapidfuzz is gone from the target environment this skips cleanly.
"""
import itertools, random, sys

try:
    from rapidfuzz import fuzz as rf
    from rapidfuzz import process as rfp
except ImportError:
    print("rapidfuzz not installed — parity check skipped")
    sys.exit(0)

import catalog, fuzzy

WORDS = ["ps2", "playstation", "2", "greatest", "hits", "black", "label",
         "complete", "cib", "disc", "only", "tested", "working", "rare",
         "silent", "hill", "god", "of", "war", "ico", "okami", "kuon",
         "final", "fantasy", "x", "xii", "kingdom", "hearts", "3", "ii"]

rng = random.Random(4)
pairs = []

# Real catalogue keys against realistic noisy listing titles.
keys = [c for c in catalog._CHOICES]
for k in keys:
    pairs.append((k, k))
    for _ in range(6):
        noise = " ".join(rng.sample(WORDS, rng.randint(1, 6)))
        pairs.append((f"{k} {noise}".strip(), k))
        pairs.append((noise, k))

# Adversarial: near-identical titles, the case that actually costs money.
for a, b in itertools.combinations(keys[:26], 2):
    pairs.append((a, b))

for _ in range(1500):
    a = " ".join(rng.sample(WORDS, rng.randint(1, 7)))
    b = " ".join(rng.sample(WORDS, rng.randint(1, 7)))
    pairs.append((a, b))

worst, bad = 0.0, []
for q, c in pairs:
    mine, theirs = fuzzy.token_set_ratio(q, c), rf.token_set_ratio(q, c)
    d = abs(mine - theirs)
    worst = max(worst, d)
    if d > 0.5:
        bad.append((q[:40], c[:30], round(mine, 2), round(theirs, 2)))

print(f"compared {len(pairs)} pairs")
print(f"max absolute difference: {worst:.4f}")
if bad:
    print(f"\n{len(bad)} disagreements over 0.5:")
    for q, c, m, t in bad[:12]:
        print(f"  '{q}' vs '{c}': mine={m} rapidfuzz={t}")
    sys.exit(1)

# What actually matters is not the score but the title it resolves to.
mismatch = []
for q, _ in pairs[:900]:
    a = catalog.match(q)
    r = fuzzy.extract(catalog.strip_noise(q), catalog._CHOICES,
                      scorer=fuzzy.token_set_ratio, limit=1)
    rr = rfp.extract(catalog.strip_noise(q), catalog._CHOICES,
                            scorer=rf.token_set_ratio, limit=1)
    if r and rr and r[0][0] != rr[0][0] and abs(r[0][1] - rr[0][1]) > 0.5:
        mismatch.append((q[:44], r[0][0], rr[0][0]))

print(f"top-choice disagreements: {len(mismatch)}")
for q, a, b in mismatch[:8]:
    print(f"  '{q}' -> mine '{a}' vs rapidfuzz '{b}'")
sys.exit(1 if mismatch else 0)
