"""
test_identify.py — photo identification, with a fake transport so no network
or API key is touched. Verifies the model's reply is parsed, run through the
catalog matcher, and degraded to a status (never an exception) on every path.
"""
from __future__ import annotations

import io
import json
import time

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
    # 429 on the free tier is a DAILY cap. Telling the user to "try again in a
    # moment" would send them retrying something that cannot work until
    # tomorrow, so it must read differently from a 503.
    _q = identify._status_note(429, {}).lower()
    check("429 note explains the daily limit",
          "reset" in _q and "moment" not in _q)
    check("429 is not retried (each retry spends scarce daily quota)",
          429 not in identify._RETRY_STATUS)
    check("503 is still retried", 503 in identify._RETRY_STATUS)

    calls429 = {"n": 0}
    def _quota(req, timeout=None):
        calls429["n"] += 1
        raise identify.urllib.error.HTTPError(
            "http://x", 429, "quota", {},
            io.BytesIO(b'{"error":{"message":"exceeded your current quota"}}'))
    real0 = identify.urllib.request.urlopen
    identify.urllib.request.urlopen = _quota
    try:
        st, _ = identify._http("POST", "http://x", {}, b"{}",
                               attempts=3, backoff=0)
    finally:
        identify.urllib.request.urlopen = real0
    check("a 429 gives up immediately, spending one request not three",
          st == 429 and calls429["n"] == 1)
    check("401 note keeps status",
          "401" in identify._status_note(401, {"error": {"message": "nope"}}))

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

    # 11. A stalled service (no reply at all) is the SAME transient condition as
    # a 503 -- under load the free tier grinds instead of refusing. It must be
    # retried and reported plainly, not surfaced as a raw socket error.
    check("timeout note is friendly",
          "slow" in identify._status_note(identify._TIMED_OUT, {}).lower())
    check("timeout is recognised as a timeout",
          identify._is_timeout(TimeoutError("timed out")))
    check("a refused connection is NOT a timeout",
          not identify._is_timeout(OSError("connection refused")))
    check("URLError wrapping a timeout counts",
          identify._is_timeout(
              identify.urllib.error.URLError(TimeoutError("timed out"))))

    calls3 = {"n": 0}
    def _stall(req, timeout=None):
        calls3["n"] += 1
        raise TimeoutError("The read operation timed out")
    identify.urllib.request.urlopen = _stall
    try:
        status, payload = identify._http("POST", "http://x", {}, b"{}",
                                         attempts=3, backoff=0)
    finally:
        identify.urllib.request.urlopen = real
    check("a stalled service retries and reports as transient",
          status == identify._TIMED_OUT and calls3["n"] == 3)
    check("stall does not escape as an exception",
          "slow" in identify._status_note(status, payload).lower())

    # A genuinely dead network must still say so -- "busy" would send the user
    # looking for the wrong problem.
    calls4 = {"n": 0}
    def _dead(req, timeout=None):
        calls4["n"] += 1
        raise identify.urllib.error.URLError("no route to host")
    identify.urllib.request.urlopen = _dead
    raised = False
    try:
        identify._http("POST", "http://x", {}, b"{}", attempts=2, backoff=0)
    except OSError:
        raised = True
    finally:
        identify.urllib.request.urlopen = real
    check("no network still raises rather than reporting 'busy'", raised)

    # The wall-clock budget must stop the loop even with attempts left.
    calls5 = {"n": 0}
    def _slow503(req, timeout=None):
        calls5["n"] += 1
        time.sleep(0.05)
        raise _err503()
    identify.urllib.request.urlopen = _slow503
    try:
        identify._http("POST", "http://x", {}, b"{}",
                       attempts=50, backoff=0, deadline=0.12)
    finally:
        identify.urllib.request.urlopen = real
    check("the time budget stops retrying before the attempt cap",
          0 < calls5["n"] < 50)

    # 12. The CLIP shortlist. A local model's errors are overwhelmingly naming
    # errors -- it reads the right cover and writes "Black: PS2 Game" or the
    # Japanese title -- so being handed the canonical strings to choose from is
    # what turns it from 50% to 89% correct. The prompt must therefore carry
    # the candidates, and must still permit "none of these", or a shortlist
    # would convert an unknown cover into a confident wrong pick.
    seen = {}
    def spy(status=200, payload=None):
        def t(method, url, headers, body):
            seen["body"] = json.loads(body.decode())
            return status, (payload if payload is not None else {
                "choices": [{"message": {"content": json.dumps(
                    {"title": "Ico", "variant": "unknown",
                     "confidence": "high"})}}]})
        return t

    r = identify.identify_cover(
        "Zm9v", provider="gemini", api_key=KEY, transport=spy(),
        candidates=[{"title": "Ico"}, {"title": "Okami"}])
    sent = seen["body"]["messages"][0]["content"][0]["text"]
    check("shortlist prompt lists the candidates",
          "- Ico" in sent and "- Okami" in sent)
    check("shortlist prompt still allows 'none of these'",
          "none of them" in sent.lower())
    check("shortlist prompt warns against guessing a lookalike",
          "do not guess" in sent.lower())
    check("shortlist result still resolves through the catalog",
          r.status == "matched" and r.title == "Ico")

    identify.identify_cover("Zm9v", provider="gemini", api_key=KEY,
                            transport=spy(), candidates=None)
    plain = seen["body"]["messages"][0]["content"][0]["text"]
    check("no candidates -> the plain open-ended prompt", "- " not in plain)
    identify.identify_cover("Zm9v", provider="gemini", api_key=KEY,
                            transport=spy(), candidates=[])
    check("empty candidate list -> plain prompt too",
          seen["body"]["messages"][0]["content"][0]["text"] == plain)
    identify.identify_cover("Zm9v", provider="gemini", api_key=KEY,
                            transport=spy(), candidates=["Ico", "Okami"])
    check("plain strings work as candidates, not just dicts",
          "- Ico" in seen["body"]["messages"][0]["content"][0]["text"])

    # `extra` exists for reasoning models: qwen3.5 spends its whole budget in a
    # `reasoning` field and returns empty content unless told not to think.
    identify.identify_cover("Zm9v", provider="gemini", api_key=KEY,
                            transport=spy(), extra={"reasoning_effort": "none"})
    check("extra fields reach the request body",
          seen["body"].get("reasoning_effort") == "none")
    identify.identify_cover("Zm9v", provider="gemini", api_key=KEY,
                            transport=spy())
    check("no extra -> body stays clean",
          "reasoning_effort" not in seen["body"])

    # And if a reasoning model DOES strand the answer in `reasoning`, salvage
    # it rather than reporting an empty response.
    r = identify.identify_cover(
        "Zm9v", provider="gemini", api_key=KEY,
        transport=spy(payload={"choices": [{"message": {
            "content": "",
            "reasoning": 'I can read the cover. {"title": "Okami", '
                         '"variant": "unknown", "confidence": "medium"}'}}]}))
    check("answer stranded in `reasoning` is recovered",
          r.status == "matched" and r.title == "Okami")

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
