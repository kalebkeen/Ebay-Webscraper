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


# --------------------------------------------------------------------------
# thecoverproject source
# --------------------------------------------------------------------------

CAT_HTML = """
<html><body>
  <a href="view.php?game_id=101">Ico</a>
  <a href="view.php?game_id=102">Okami</a>
  <a href="view.php?game_id=103">Katamari Damacy</a>
  <a href="about.php">About</a>
</body></html>
"""

GAME_HTML = """
<html><body>
  <img src="/images/thumbs/ico_front_thumb.jpg">
  <a href="/covers/ps2/ico_front.jpg">Front Cover</a>
  <a href="/covers/ps2/ico_back.jpg">Back Cover</a>
  <a href="/covers/ps2/ico_manual.pdf">Manual</a>
</body></html>
"""


class FakeLimiter:
    """Records that the limiter was consulted without actually sleeping."""
    def __init__(self):
        self.calls = 0
    def wait(self, *a, **k):
        self.calls += 1


class CPRouter:
    def __init__(self, status=200):
        self.status = status
        self.urls = []
    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "blocked", {}, None)
        if "cat_id=" in url:
            return _Resp(CAT_HTML.encode())
        if "game_id=" in url:
            return _Resp(GAME_HTML.encode())
        return _Resp(PNG)


def _cp(router, face="front"):
    lim = FakeLimiter()
    c = seed_covers.CoverProjectCovers(opener=router, face=face, limiter=lim)
    return c, lim


def test_coverproject_index():
    router = CPRouter()
    c, lim = _cp(router)
    n = c.load()
    check("cp indexes only game links", n == 3)
    check("cp index keyed by normalised title", "ico" in c.by_key)
    check("cp ignores non-game links",
          all("game_id=" in u for u in c.by_key.values()))
    check("cp scopes the index request to the PS2 category",
          f"cat_id={seed_covers.CP_PS2_CAT}" in router.urls[0])
    check("cp went through the rate limiter", lim.calls == 1)


def test_coverproject_face_selection():
    c, _ = _cp(CPRouter(), face="front")
    c.load()
    front = c.best_filename("Ico")
    check("cp picks the front cover", front.endswith("ico_front.jpg"))
    check("cp skips thumbnails", "thumb" not in front)
    check("cp returns an absolute url", front.startswith("https://"))

    c2, _ = _cp(CPRouter(), face="back")
    c2.load()
    check("cp honours --face back",
          c2.best_filename("Ico").endswith("ico_back.jpg"))

    c3, _ = _cp(CPRouter())
    c3.load()
    check("cp fuzzy-matches a near-miss title",
          (c3.best_filename("Katamari Damacyy") or "").endswith(".jpg"))
    check("cp returns None for an unknown title",
          c3.best_filename("Some Game That Does Not Exist") is None)


def test_coverproject_limits():
    # The interval is a floor, enforced with an injected clock so the test
    # asserts the sleep rather than serving it.
    slept, t = [], [0.0]
    seed_covers._RateLimiter._last = None
    seed_covers._RateLimiter.wait(sleep=slept.append, clock=lambda: t[0])
    check("first request is not delayed", slept == [])
    t[0] = 0.5
    seed_covers._RateLimiter.wait(sleep=slept.append, clock=lambda: t[0])
    check("limiter sleeps out the remainder of the interval",
          slept and abs(slept[0] - 1.5) < 1e-6)
    # A clock reading exactly 0.0 must still gate the second request.
    check("a zero-valued clock does not disable the gate", len(slept) == 1)
    seed_covers._RateLimiter._last = None

    check("interval floor is their stated 2s", seed_covers._CP_MIN_INTERVAL >= 2.0)
    check("offpeak window is 02:00-06:00 UTC",
          seed_covers._CP_WINDOW_UTC == (2, 6))
    import datetime as _dt
    at3 = _dt.datetime(2026, 1, 1, 3, 0, tzinfo=_dt.timezone.utc)
    at9 = _dt.datetime(2026, 1, 1, 9, 0, tzinfo=_dt.timezone.utc)
    check("03:00 UTC is inside the window", seed_covers.in_offpeak_window(at3))
    check("09:00 UTC is outside the window",
          not seed_covers.in_offpeak_window(at9))
    check("cp identifies itself rather than spoofing a browser",
          "spine-ps2arb" in seed_covers._CP_UA
          and "Mozilla" not in seed_covers._CP_UA)


def test_coverproject_failures():
    router = CPRouter(status=403)
    c, _ = _cp(router)
    check("a 403 index load reports zero, not a crash", c.load() == 0)
    check("the 403 is retained for the diagnostic", c.last_status == 403)
    # Whitespace-normalised: the message is hard-wrapped for the terminal.
    explain = " ".join(seed_covers._cp_explain_403().split())
    check("the 403 explanation refuses UA spoofing as the fix",
          "NOT to send a browser user-agent" in explain
          and "allowlist this user-agent" in explain)

    # End-to-end through run(), with the same injected add_photo as libretro.
    captured = []
    c2, _ = _cp(CPRouter())
    summary = seed_covers.run(
        c2, ["Ico", "Okami"],
        add_photo=lambda b64, t, v: (captured.append(t), {"ok": True,
                                                          "stored": True})[1],
        delay=0, log=lambda *_: None)
    check("cp seeds through the shared run loop", summary["seeded"] == 2)
    check("cp stored both titles", set(captured) == {"Ico", "Okami"})

    try:
        seed_covers.build_source("typo")
        ok = False
    except ValueError:
        ok = True
    check("an unknown --source fails loudly", ok)
    check("build_source returns the right classes",
          isinstance(seed_covers.build_source("libretro"),
                     seed_covers.LibretroCovers)
          and isinstance(seed_covers.build_source("coverproject"),
                         seed_covers.CoverProjectCovers))


# --------------------------------------------------------------------------
# TheGamesDB source
# --------------------------------------------------------------------------

TGDB_GAMES = [
    {"id": 101, "game_title": "Ico"},
    {"id": 102, "game_title": "Okami"},
    {"id": 103, "game_title": "Katamari Damacy"},
]

TGDB_BASE = {"original": "https://cdn.thegamesdb.net/images/original/",
             "large": "https://cdn.thegamesdb.net/images/large/"}


class TGDBRouter:
    """Serves the v1 ByPlatformID / Images shapes, plus the CDN image."""

    def __init__(self, allowance=500, pages=1, status=200):
        self.allowance = allowance
        self.pages = pages
        self.status = status
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "err", {}, None)
        if "ByPlatformID" in url:
            return _Resp({"code": 200,
                          "data": {"count": len(TGDB_GAMES),
                                   "games": TGDB_GAMES},
                          "remaining_monthly_allowance": self.allowance})
        if "Games/Images" in url:
            gid = urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query)["games_id"][0]
            return _Resp({"code": 200,
                          "data": {"base_url": TGDB_BASE,
                                   "images": {gid: [
                                       {"id": 1, "type": "boxart",
                                        "side": "front",
                                        "filename": f"boxart/front/{gid}-1.jpg"},
                                       {"id": 2, "type": "boxart",
                                        "side": "back",
                                        "filename": f"boxart/back/{gid}-1.jpg"},
                                       {"id": 3, "type": "screenshot",
                                        "side": None,
                                        "filename": f"screenshots/{gid}.jpg"},
                                   ]}},
                          "remaining_monthly_allowance": self.allowance})
        return _Resp(PNG)


def _tgdb(router, face="front", key="testkey"):
    return seed_covers.TheGamesDBCovers(api_key=key, opener=router, face=face)


def test_tgdb_index():
    router = TGDBRouter()
    c = _tgdb(router)
    check("tgdb indexes the platform listing", c.load() == 3)
    check("tgdb keys by normalised title", c.by_key.get("ico") == 101)
    check("tgdb scopes to the PS2 platform id",
          f"id={seed_covers.TGDB_PS2_PLATFORM}" in router.urls[0])
    check("tgdb sends the api key", "apikey=testkey" in router.urls[0])
    check("tgdb records the allowance", c.remaining == 500)


def test_tgdb_faces():
    c = _tgdb(TGDBRouter(), face="front")
    c.load()
    front = c.best_filename("Ico")
    check("tgdb builds an absolute cdn url",
          front == "https://cdn.thegamesdb.net/images/original/boxart/front/101-1.jpg")

    c2 = _tgdb(TGDBRouter(), face="back")
    c2.load()
    check("tgdb serves the back cover",
          c2.best_filename("Ico").endswith("boxart/back/101-1.jpg"))

    c3 = _tgdb(TGDBRouter())
    c3.load()
    check("tgdb ignores non-boxart images",
          "screenshot" not in (c3.best_filename("Okami") or ""))
    check("tgdb fuzzy-matches a near-miss title",
          (c3.best_filename("Katamari Damacyy") or "").endswith(".jpg"))
    check("tgdb returns None for an unknown title",
          c3.best_filename("Some Game That Does Not Exist") is None)

    # A face the API does not carry must miss, not silently serve the other
    # side -- a back cover stored as a front one would poison the index.
    class OneSided(TGDBRouter):
        def __call__(self, req, timeout=None):
            resp = super().__call__(req, timeout)
            if "Games/Images" in req.full_url:
                d = json.loads(resp.read())
                for imgs in d["data"]["images"].values():
                    imgs[:] = [i for i in imgs if i.get("side") != "back"]
                return _Resp(d)
            return resp
    c4 = _tgdb(OneSided(), face="back")
    c4.load()
    check("a missing face misses rather than substituting",
          c4.best_filename("Ico") is None)


def test_tgdb_allowance_and_key():
    # Allowance exhausted -> stop resolving rather than spend against it.
    c = _tgdb(TGDBRouter(allowance=0))
    c.load()
    check("tgdb sees an exhausted allowance", c.exhausted())
    check("tgdb stops resolving when exhausted", c.best_filename("Ico") is None)

    # No key -> a clean zero, not a traceback, and no request attempted.
    router = TGDBRouter()
    c2 = seed_covers.TheGamesDBCovers(api_key="", opener=router)
    check("no key loads nothing", c2.load() == 0)
    check("no key makes no request", router.urls == [])

    # Caching: two lookups of one title cost one Images call.
    router2 = TGDBRouter()
    c3 = _tgdb(router2)
    c3.load()
    c3.best_filename("Ico")
    c3.best_filename("Ico")
    check("tgdb caches images per game id",
          sum("Games/Images" in u for u in router2.urls) == 1)


def test_tgdb_seed_loop():
    captured = []
    c = _tgdb(TGDBRouter())
    summary = seed_covers.run(
        c, ["Ico", "Okami"],
        add_photo=lambda b64, t, v: (captured.append(t),
                                     {"ok": True, "stored": True})[1],
        delay=0, log=lambda *_: None)
    check("tgdb seeds through the shared run loop", summary["seeded"] == 2)
    check("tgdb stored both titles", set(captured) == {"Ico", "Okami"})
    check("build_source knows thegamesdb",
          isinstance(seed_covers.build_source("thegamesdb"),
                     seed_covers.TheGamesDBCovers))


def main() -> int:
    test_matching()
    test_seed_loop()
    test_failures()
    test_tgdb_index()
    test_tgdb_faces()
    test_tgdb_allowance_and_key()
    test_tgdb_seed_loop()
    test_coverproject_index()
    test_coverproject_face_selection()
    test_coverproject_limits()
    test_coverproject_failures()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
