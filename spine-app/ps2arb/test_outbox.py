"""
test_outbox.py — the on-phone pending-photo queue. Stdlib only, no vault.

Verifies the durability contract: a confirmed photo is written to disk before
it goes anywhere, survives a fresh process (a new PhotoOutbox over the same
dir), comes back oldest-first, and is removed only when told.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import photo_outbox

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        box = photo_outbox.PhotoOutbox(Path(d) / "photo_outbox")

        check("starts empty", box.count() == 0 and box.pending() == [])

        a = box.enqueue({"image": "AAA", "title": "Ico",
                         "variant": "black_label", "queued_at": 1})
        b = box.enqueue({"image": "BBB", "title": "Okami", "queued_at": 2})
        check("enqueue returns ids", bool(a) and bool(b) and a != b)
        check("count reflects two", box.count() == 2)

        rows = box.pending()
        check("pending returns both", len(rows) == 2)
        check("oldest first", rows[0]["title"] == "Ico"
              and rows[1]["title"] == "Okami")
        check("payload preserved", rows[0]["image"] == "AAA"
              and rows[0]["variant"] == "black_label")
        check("id stamped on record", rows[0]["id"] == a)

        # Durability: a brand-new instance over the same dir sees the queue.
        box2 = photo_outbox.PhotoOutbox(Path(d) / "photo_outbox")
        check("survives a fresh instance", box2.count() == 2)

        box2.remove(a)
        check("remove drops one", box2.count() == 1)
        left = box2.pending()
        check("the right one remains", len(left) == 1
              and left[0]["title"] == "Okami")

        box2.remove("does-not-exist")          # must not raise
        check("removing a missing id is a no-op", box2.count() == 1)

        # queued_at is auto-stamped when the caller omits it.
        box2.enqueue({"image": "CCC", "title": "Shadow of the Colossus"})
        stamped = [r for r in box2.pending() if r["title"].startswith("Shadow")]
        check("auto-stamps queued_at", stamped and stamped[0].get("queued_at", 0) > 0)

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
