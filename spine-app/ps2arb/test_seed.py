"""
test_seed.py — the cover-art seeder, driven without a network or Pillow.

The GitHub tree and the raw-image fetch both go through an injected opener,
and add_photo is injected too, so the matching rules (normalise, region
preference, demo skip, fuzzy fallback) and the seed loop (dry-run, store,
missing art, fetch failure, refusal) are all exercised against canned data.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse

import seed_covers

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))

PNG = b"\x89PNG\r\n\x1a\nFAKEBOXART"

TREE_FILES = [
    "Ico (USA).png",
    "Ico (Japan).png",
    "Okami (USA).png",
    "Okami (Europe).png",
    "Devil May Cry 3 - Dante's Awakening (USA) (Special Edition).png",
    "Shadow of the Colossus (USA).png",
    "Shadow of the Colossus (USA) (Demo).png",
    "Katamari Damacy (USA).png",
]


class _Resp:
    def __init__(self, body, status=200):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class Router:
    def __init__(self, files=TREE_FILES, png=PNG, missing=()):
        self.files = files
        self.png = png
        self.missing = set(missing)
        self.fetched = []
    def __call__(self, req, timeout=None):
        url = req.full_url
        if "git/trees" in url:
            return _Resp({"tree": [{"path": "Named_Boxarts/" + f, "type": "blob"}
                                   for f in self.files]})
        # a raw image request
        fname = urllib.parse.unquote(url.split("/Named_Boxarts/", 1)[1])
        self.fetched.append(fname)
        if fname in self.missing:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return _Resp(self.png)


def _covers(router):
    c = seed_covers.LibretroCovers(opener=router)
    c.load()
    return c


def test_matching():
    check("norm folds colon/dash/case",
          seed_covers._norm("Devil May Cry 3: Dante's Awakening")
          == seed_covers._norm("Devil May Cry 3 - Dante's Awakening (USA)"))
    check("region rank prefers USA over Japan",
          seed_covers._region_rank("Ico (USA).png")
          < seed_covers._region_rank("Ico (Japan).png"))
    check("demo scans are deprioritised",
          seed_covers._region_rank("Shadow of the Colossus (USA) (Demo).png")
          > seed_covers._region_rank("Shadow of the Colossus (USA).png"))

    c = _covers(Router())
    check("index loaded", c.loaded and len(c.by_key) >= 5)
    check("prefers the USA boxart", c.best_filename("Ico") == "Ico (USA).png")
    check("skips the demo, picks the real cover",
          c.best_filename("Shadow of the Colossus")
          == "Shadow of the Colossus (USA).png")
    check("matches across colon vs dash punctuation",
          c.best_filename("Devil May Cry 3: Dante's Awakening")
          == "Devil May Cry 3 - Dante's Awakening (USA) (Special Edition).png")
    check("fuzzy fallback catches a near-miss title",
          c.best_filename("Katamari Damacyy") == "Katamari Damacy (USA).png")
    check("a title with no art returns None",
          c.best_filename("Some Game That Does Not Exist") is None)


def test_seed_loop():
    captured = []
    def fake_add(b64, title, variant):
        captured.append((b64, title, variant))
        return {"ok": True, "stored": True}

    router = Router()
    c = _covers(router)

    # Dry run: counts a match, fetches nothing, stores nothing.
    summary = seed_covers.run(c, ["Ico", "Okami"], add_photo=fake_add,
                              dry_run=True, delay=0, log=lambda *_: None)
    check("dry-run counts matches", summary["seeded"] == 2)
    check("dry-run fetches nothing", router.fetched == [])
    check("dry-run stores nothing", captured == [])

    # Real seed: fetches art, base64s it, stores under the title.
    summary = seed_covers.run(c, ["Ico", "Okami", "Some Game That Does Not Exist"],
                              add_photo=fake_add, delay=0, log=lambda *_: None)
    check("seeds the two real titles", summary["seeded"] == 2)
    check("reports the missing one", summary["missing"] == 1)
    check("add_photo got both titles",
          {t for _, t, _ in captured} == {"Ico", "Okami"})
    check("seeded bytes are the fetched PNG",
          captured and base64.b64decode(captured[0][0]) == PNG)
    check("seeds under the requested variant",
          all(v == "unknown" for _, _, v in captured))


def test_failures():
    # Fetch failure: the boxart 404s -> counted as failed, not seeded.
    router = Router(missing={"Ico (USA).png"})
    c = _covers(router)
    summary = seed_covers.run(c, ["Ico"], add_photo=lambda *a: {"ok": True},
                              delay=0, log=lambda *_: None)
    check("a 404 boxart is a failure, not a seed",
          summary["failed"] == 1 and summary["seeded"] == 0)

    # add_photo refuses (e.g. no Pillow) -> failure, surfaced not swallowed.
    c2 = _covers(Router())
    summary = seed_covers.run(
        c2, ["Ico"],
        add_photo=lambda *a: {"ok": False, "detail": "no Pillow"},
        delay=0, log=lambda *_: None)
    check("add_photo refusal is a failure", summary["failed"] == 1)

    # A totally unreachable tree -> honest error, no crash.
    def dead(req, timeout=None):
        raise urllib.error.URLError("offline")
    c3 = seed_covers.LibretroCovers(opener=dead)
    summary = seed_covers.run(c3, ["Ico"], add_photo=lambda *a: {"ok": True},
                              delay=0, log=lambda *_: None)
    check("unreachable index reports an error", "error" in summary)


def main() -> int:
    test_matching()
    test_seed_loop()
    test_failures()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
