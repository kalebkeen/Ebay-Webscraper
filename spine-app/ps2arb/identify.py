"""
identify.py — identify a PS2 game from a photo of its cover or spine.

Same shape as every other resolver in this project: it turns an input (here a
photo) into a *candidate title string*, which then goes through the existing
catalog matcher and is always confirmed before it sets a price. The photo is
read by a vision model over the Messages API, but that is an implementation
detail — nothing above this module knows or cares how the title was produced,
exactly as it doesn't care whether a title came from a barcode or ScanDex.

Standard library only (no SDK), like ebay.py and scandex.py, so it stays
Chaquopy-safe inside the APK. Credentials come from settings/env; the desktop
keystore serves the key so it is never pasted on the phone.

    VISION_PROVIDER   anthropic (default) | gemini | openai
    VISION_API_KEY    the key (a secret; falls back to ANTHROPIC_API_KEY)
    VISION_MODEL      model id (default per provider)
    VISION_BASE_URL   endpoint for provider=openai (e.g. a local Ollama)
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import catalog

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Google's OpenAI-compatible endpoint — lets the same code path serve Gemini
# (free tier), and later a local Ollama or any other OpenAI-compatible host.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
# Use Gemini's rolling "-latest" alias, not a pinned version: Google retires
# dated flash models (gemini-2.0-flash was retired by mid-2026, which 404'd
# every scan). The alias keeps identify working across model turnover without an
# APK rebuild; pin a specific model with `keystore.py set vision_model ...`.
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "gemini": "gemini-flash-latest"}

_VARIANT_NOTE = (
    '"variant" is one of "black_label", "greatest_hits", or "unknown" — '
    "Greatest Hits / Platinum / The Best budget reprints have a distinctly "
    'coloured spine or logo; use "unknown" if you cannot tell. '
    '"confidence" is one of "high", "medium", "low".'
)
_PROMPT_SINGLE = (
    "You are identifying a PlayStation 2 (PS2) game from a photo of its cover "
    "or spine. Respond with ONLY a JSON object and no other text with keys "
    '"title" (the title exactly as printed, or null if you cannot read a PS2 '
    'game title), "variant", "confidence". ' + _VARIANT_NOTE +
    " If the image is not a PS2 game, set title to null."
)
_PROMPT_MULTI = (
    "You are identifying PlayStation 2 (PS2) games from a photo that shows one "
    "or more game spines or covers (e.g. a shelf or a stack). Respond with "
    "ONLY a JSON array and no other text. Each element is an object with keys "
    '"title" (the title exactly as printed), "variant", "confidence". '
    + _VARIANT_NOTE +
    " Include every distinct PS2 game you can read; use an empty array [] if "
    "you cannot read any."
)


@dataclass
class IdentifyResult:
    raw_title: str | None = None      # what the model read off the cover
    title: str | None = None          # resolved canonical catalog title
    variant: str = "unknown"
    confidence: str = "low"
    match_score: float = 0.0
    status: str = "unknown"           # matched | unmatched | no_game | error
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "matched" and self.title is not None


# Statuses worth another try: the free tier's transient "overloaded" errors.
# A single scan shouldn't fail because of a momentary blip.
#
# 429 is deliberately NOT here. On Gemini's free tier it means the DAILY quota
# is gone (measured 2026-08-28: GenerateRequestsPerDayPerProjectPerModel-
# FreeTier, limit 20 per day PER MODEL), not "slow down for a second". Retrying
# cannot succeed, and with only twenty requests a day to spend, burning three
# of them on a refusal that was never going to change is the expensive mistake.
_RETRY_STATUS = frozenset({500, 502, 503, 504})


# Timing, measured against the live free tier 2026-08-28 while it was
# congested. Latency is strongly BIMODAL: a call either comes back in 1-4s or
# it grinds for 30-50s, and the slow ones are as likely to end in 503 as in an
# answer. Seven models sampled, every one showing the same shape.
#
# That shape decides the strategy. A long per-attempt timeout mostly buys
# waiting: it turns a fast retry into a slow one without improving the odds,
# and 40s x 3 was a 123-second wait to be told it failed. A SHORT timeout with
# MORE attempts plays the fast path repeatedly, which is where the answers are.
#
# So: cut each attempt off at 12s (fast successes land well inside it), and
# govern the whole thing with a wall-clock budget instead of a fixed count, so
# quick 503s buy extra tries while slow ones do not blow the budget.
#
# Attempts stay at THREE despite the bimodal odds arguing for more. The free
# tier allows only twenty requests per day per model, so every retry is a
# meaningful fraction of a day's scanning: six attempts would let three failed
# scans consume the entire daily allowance. Quota, not latency, is the binding
# constraint here.
_TIMEOUT = 12.0
_DEADLINE = 45.0
_ATTEMPTS = 3

# Our own synthetic status for "the server never answered in time". Not a real
# HTTP code from the service -- it exists so a timeout reports as the transient
# it almost always is, instead of surfacing a raw socket error.
_TIMED_OUT = 408


def _is_timeout(exc: BaseException) -> bool:
    """A read/connect timeout, as opposed to a genuinely dead network.

    Worth separating: a timeout means the service is slow (retry, and tell the
    user it is busy), while a refused or unroutable connection means there is
    no network at all (say so -- that is actionable, "busy" is not)."""
    return isinstance(exc, TimeoutError) or isinstance(
        getattr(exc, "reason", None), TimeoutError)


def _http(method: str, url: str, headers: dict, body: bytes,
          timeout: float = _TIMEOUT, *, attempts: int = _ATTEMPTS,
          backoff: float = 0.5, deadline: float = _DEADLINE):
    started = time.monotonic()
    last = (0, {})
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(
                    resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"raw": raw[:300]}
            last = (exc.code, payload)
            if exc.code not in _RETRY_STATUS:
                return exc.code, payload
        except OSError as exc:
            if not _is_timeout(exc):          # no network — do not dress it up
                if i == attempts - 1:
                    raise
            else:
                last = (_TIMED_OUT, {"error": {"message": (
                    f"no response within {timeout:.0f}s")}})
        # Stop on the attempt cap or the time budget, whichever comes first.
        if i == attempts - 1 or (time.monotonic() - started) >= deadline:
            break
        time.sleep(min(backoff * (2 ** i), 2.0))
    return last


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the model's reply, tolerantly."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def _extract_json_array(text: str) -> list:
    """Pull the JSON array out of the model's reply, tolerantly."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _err_detail(payload) -> str:
    if isinstance(payload, dict):
        return (payload.get("error") or {}).get("message", "") or ""
    return ""


def _status_note(status: int, payload) -> str:
    """A human-readable note for a non-200 vision response. Keeps the numeric
    status (useful for debugging) but explains the transient ones plainly."""
    if status == _TIMED_OUT:
        # We gave up waiting rather than being refused. Under load the service
        # stalls instead of saying no, so this is the same condition as a 503
        # and deserves the same plain explanation, not a socket error.
        return ("the photo service is slow right now — it didn't answer in "
                "time; try again in a moment")
    if status == 429:
        # A daily cap, not a momentary one — "try again in a moment" would be
        # actively misleading. Say what ran out and what to do about it.
        return ("today's free photo scans for this model are used up (429) — "
                "they reset tomorrow, or pick another model on the keystore "
                "(each model has its own daily allowance)")
    if status == 503:
        return ("the photo service is busy right now (503) — "
                "try again in a moment")
    if status in (500, 502, 504):
        return f"the photo service had a temporary error ({status}) — try again"
    d = _err_detail(payload)
    return f"identify service returned {status}" + (f": {d[:120]}" if d else "")


def _call_anthropic(image_b64, media_type, prompt, api_key, model, transport):
    """Anthropic Messages API. Returns (model_text, error_note)."""
    body = json.dumps({
        "model": model, "max_tokens": 2048,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type,
                                         "data": image_b64}},
            {"type": "text", "text": prompt}]}],
    }).encode()
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
               "content-type": "application/json"}
    status, payload = transport("POST", ANTHROPIC_URL, headers, body)
    if status != 200:
        return None, _status_note(status, payload)
    if payload.get("stop_reason") == "refusal":
        return None, "request was declined"
    text = "".join(b.get("text", "") for b in (payload.get("content") or [])
                   if b.get("type") == "text")
    return text, None


def _call_openai(image_b64, media_type, prompt, api_key, model, base_url, transport):
    """OpenAI-compatible chat/completions (Gemini free tier, Ollama, etc.).
    Returns (model_text, error_note)."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model, "max_tokens": 2048,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{image_b64}"}}]}],
    }).encode()
    headers = {"Authorization": "Bearer " + api_key,
               "content-type": "application/json"}
    status, payload = transport("POST", url, headers, body)
    if status != 200:
        return None, _status_note(status, payload)
    choices = payload.get("choices") or []
    if not choices:
        return None, "identify service returned no content"
    content = (choices[0].get("message") or {}).get("content", "")
    # Some hosts return content as a list of parts; join text parts.
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict))
    return content, None


def _model_text(image_b64, media_type, prompt, *, provider, api_key, model,
                base_url, transport):
    """Resolve provider/key/model, call the vision model, return (text, err)."""
    provider = (provider or os.environ.get("VISION_PROVIDER")
                or "anthropic").lower()
    api_key = api_key or os.environ.get("VISION_API_KEY") \
        or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "no photo-identify key set"
    model = model or os.environ.get("VISION_MODEL") \
        or DEFAULT_MODELS.get(provider, "")
    if not model:
        return None, "no vision model configured"
    try:
        if provider == "anthropic":
            return _call_anthropic(image_b64, media_type, prompt, api_key,
                                   model, transport)
        base = base_url or os.environ.get("VISION_BASE_URL") \
            or (GEMINI_BASE if provider == "gemini" else "")
        if not base:
            return None, "no vision base URL configured"
        return _call_openai(image_b64, media_type, prompt, api_key, model,
                            base, transport)
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except Exception as exc:                            # noqa: BLE001
        return None, f"request failed: {exc}"


def _resolve_title(raw, variant, confidence) -> IdentifyResult:
    """Turn a title the model read into a catalog-resolved result."""
    raw = (raw or "").strip()
    variant = variant or "unknown"
    confidence = confidence or "low"
    if not raw:
        return IdentifyResult(status="no_game", variant=variant,
                              confidence=confidence,
                              note="couldn't read a PS2 game in the photo")
    m = catalog.match(raw)
    if m.title is not None and m.confident:
        return IdentifyResult(raw_title=raw, title=m.title.canonical,
                              variant=variant, confidence=confidence,
                              match_score=m.score, status="matched")
    return IdentifyResult(
        raw_title=raw, variant=variant, confidence=confidence,
        match_score=m.score, status="unmatched",
        note=f"read “{raw}” but no confident catalog match (best {m.score:.0f})")


def identify_cover(image_b64: str, media_type: str = "image/jpeg", *,
                   provider: str | None = None, api_key: str | None = None,
                   model: str | None = None, base_url: str | None = None,
                   transport: Callable = _http) -> IdentifyResult:
    """Identify one game from one photo. Never raises — failures become a status.

    provider: "anthropic" (default) or "gemini" / "openai" (OpenAI-compatible).
    """
    text, err = _model_text(image_b64, media_type, _PROMPT_SINGLE,
                            provider=provider, api_key=api_key, model=model,
                            base_url=base_url, transport=transport)
    if err:
        return IdentifyResult(status="error", note=err)
    data = _extract_json(text or "")
    return _resolve_title(data.get("title"), data.get("variant"),
                          data.get("confidence"))


def identify_shelf(image_b64: str, media_type: str = "image/jpeg", *,
                   provider: str | None = None, api_key: str | None = None,
                   model: str | None = None, base_url: str | None = None,
                   transport: Callable = _http) -> list[IdentifyResult]:
    """Identify every PS2 game in one photo of multiple spines/covers.

    Returns a list of IdentifyResult (matched or unmatched per game). On any
    request/parse error, returns a single-element list with status='error'.
    """
    text, err = _model_text(image_b64, media_type, _PROMPT_MULTI,
                            provider=provider, api_key=api_key, model=model,
                            base_url=base_url, transport=transport)
    if err:
        return [IdentifyResult(status="error", note=err)]
    rows = _extract_json_array(text or "")
    results = [_resolve_title(r.get("title"), r.get("variant"),
                              r.get("confidence"))
               for r in rows if isinstance(r, dict) and r.get("title")]
    if not results:
        return [IdentifyResult(status="no_game",
                               note="couldn't read any PS2 games in the photo")]
    return results
