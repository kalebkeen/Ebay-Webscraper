"""
test_identify.py — photo identification, with a fake transport so no network
or API key is touched. Verifies the model's reply is parsed, run through the
catalog matcher, and degraded to a status (never an exception) on every path.
"""
from __future__ import annotations

import json

import identify

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def fake(reply_text=None, *, status=200, stop_reason="end_turn", payload=None):
    """Build a transport returning a canned Messages-API response."""
    def _t(method, url, headers, body):
        if payload is not None:
            return status, payload
        return status, {"stop_reason": stop_reason,
                        "content": [{"type": "text", "text": reply_text or ""}]}
    return _t


def main() -> int:
    KEY = "sk-test"

    # 1. Clean match: a real catalog title comes back and resolves.
    r = identify.identify_cover(
        "Zm9v", api_key=KEY,
        transport=fake(json.dumps({"title": "Resident Evil 4",
                                   "variant": "black_label",
                                   "confidence": "high"})))
    check("matched status", r.status == "matched")
    check("resolved to catalog title", r.title == "Resident Evil 4")
    check("variant carried through", r.variant == "black_label")
    check("usable flag", r.usable is True)

    # 2. JSON embedded in prose still parses.
    r = identify.identify_cover(
        "Zm9v", api_key=KEY,
        transport=fake('Sure! {"title": "Ico", "variant": "unknown", '
                       '"confidence": "medium"} hope that helps'))
    check("embedded json parsed + matched", r.status == "matched" and r.title == "Ico")

    # 3. A title the catalog doesn't confidently know -> unmatched (not matched).
    r = identify.identify_cover(
        "Zm9v", api_key=KEY,
        transport=fake(json.dumps({"title": "Totally Fake Game 9000",
                                   "variant": "unknown", "confidence": "low"})))
    check("unmatched status", r.status == "unmatched")
    check("unmatched keeps raw title", r.raw_title == "Totally Fake Game 9000")
    check("unmatched has no catalog title", r.title is None)

    # 4. No game in the photo.
    r = identify.identify_cover(
        "Zm9v", api_key=KEY,
        transport=fake(json.dumps({"title": None, "confidence": "low"})))
    check("no_game status", r.status == "no_game")

    # 5. No API key -> error, no call.
    r = identify.identify_cover("Zm9v", api_key="",
                                transport=fake("should not be called"))
    check("missing key is an error", r.status == "error" and "key" in r.note)

    # 6. Model declined.
    r = identify.identify_cover("Zm9v", api_key=KEY,
                                transport=fake(None, stop_reason="refusal",
                                               payload=None))
    check("refusal is an error", r.status == "error")

    # 7. HTTP error (e.g. bad key) -> error, key never echoed.
    r = identify.identify_cover(
        "Zm9v", api_key=KEY,
        transport=fake(status=401,
                       payload={"error": {"message": "invalid x-api-key"}}))
    check("http error surfaced", r.status == "error" and "401" in r.note)
    check("key not echoed in note", KEY not in r.note)

    # 8. Gemini / OpenAI-compatible path (different response shape) parses too.
    def fake_openai(text=None, *, status=200, payload=None):
        def _t(method, url, headers, body):
            if payload is not None:
                return status, payload
            return status, {"choices": [{"message": {"content": text or ""}}]}
        return _t

    r = identify.identify_cover(
        "Zm9v", provider="gemini", api_key=KEY,
        transport=fake_openai(json.dumps({"title": "God of War II",
                                          "variant": "unknown",
                                          "confidence": "high"})))
    check("gemini path matched", r.status == "matched" and r.title == "God of War II")
    r = identify.identify_cover(
        "Zm9v", provider="gemini", api_key=KEY,
        transport=fake_openai(status=429,
                              payload={"error": {"message": "rate limited"}}))
    check("gemini http error surfaced", r.status == "error" and "429" in r.note)

    # 9. Shelf mode: one photo -> a list of titles.
    arr = json.dumps([
        {"title": "Ico", "variant": "unknown", "confidence": "high"},
        {"title": "Okami", "variant": "unknown", "confidence": "medium"},
        {"title": "Nonexistent Game 12345", "variant": "unknown", "confidence": "low"}])
    rows = identify.identify_shelf("Zm9v", api_key=KEY, transport=fake(arr))
    titles = [r.title for r in rows if r.status == "matched"]
    check("shelf found both known titles", "Ico" in titles and "Okami" in titles)
    check("shelf marks unknown as unmatched",
          any(r.status == "unmatched" and r.raw_title == "Nonexistent Game 12345"
              for r in rows))
    rows = identify.identify_shelf("Zm9v", api_key=KEY, transport=fake("[]"))
    check("empty shelf -> no_game", len(rows) == 1 and rows[0].status == "no_game")
    rows = identify.identify_shelf(
        "Zm9v", api_key=KEY,
        transport=fake(status=401, payload={"error": {"message": "bad"}}))
    check("shelf http error -> error", len(rows) == 1 and rows[0].status == "error")

    # 10. Transient errors: friendly note, and _http retries before giving up.
    check("503 note is friendly",
          "503" in identify._status_note(503, {})
          and "busy" in identify._status_note(503, {}).lower())
    check("401 note keeps status",
          "401" in identify._status_note(401, {"error": {"message": "nope"}}))

    import io

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'

    def _err503():
        return identify.urllib.error.HTTPError(
            "http://x", 503, "busy", {},
            io.BytesIO(b'{"error":{"message":"overloaded"}}'))

    real = identify.urllib.request.urlopen
    calls = {"n": 0}
    def _flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _err503()
        return _Resp()
    identify.urllib.request.urlopen = _flaky
    try:
        status, payload = identify._http("POST", "http://x", {}, b"{}",
                                         attempts=3, backoff=0)
    finally:
        identify.urllib.request.urlopen = real
    check("retries transient 503 then succeeds",
          status == 200 and calls["n"] == 3 and payload.get("ok") is True)

    calls2 = {"n": 0}
    def _always(req, timeout=None):
        calls2["n"] += 1
        raise _err503()
    identify.urllib.request.urlopen = _always
    try:
        status, _ = identify._http("POST", "http://x", {}, b"{}",
                                   attempts=3, backoff=0)
    finally:
        identify.urllib.request.urlopen = real
    check("gives up after N tries on persistent 503",
          status == 503 and calls2["n"] == 3)

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
