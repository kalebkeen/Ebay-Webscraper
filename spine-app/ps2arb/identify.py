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

    ANTHROPIC_API_KEY   the vision API key (a secret)
    VISION_MODEL        model id, default claude-opus-5
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import catalog

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"

_PROMPT = (
    "You are identifying a PlayStation 2 (PS2) game from a photo of its cover "
    "or spine. Respond with ONLY a JSON object and no other text. Keys:\n"
    '  "title": the game\'s title exactly as printed, or null if you cannot '
    "read a PS2 game title in the image.\n"
    '  "variant": one of "black_label", "greatest_hits", or "unknown". '
    "Greatest Hits / Platinum / The Best budget reprints have a distinctly "
    "coloured spine or logo; use \"unknown\" if you cannot tell.\n"
    '  "confidence": one of "high", "medium", "low".\n'
    "If the image is not a PS2 game, set title to null."
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


def _http(method: str, url: str, headers: dict, body: bytes,
          timeout: float = 40.0):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:300]}
        return exc.code, payload


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the model's reply, tolerantly."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def identify_cover(image_b64: str, media_type: str = "image/jpeg", *,
                   api_key: str | None = None, model: str | None = None,
                   transport: Callable = _http) -> IdentifyResult:
    """Identify one game from one photo. Never raises into the caller — every
    failure becomes a status, matching scandex.ScanDexClient.lookup."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return IdentifyResult(status="error", note="no photo-identify key set")
    model = model or os.environ.get("VISION_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type,
                                         "data": image_b64}},
            {"type": "text", "text": _PROMPT},
        ]}],
    }).encode()
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION,
               "content-type": "application/json"}

    try:
        status, payload = transport("POST", API_URL, headers, body)
    except urllib.error.URLError as exc:
        return IdentifyResult(status="error", note=f"unreachable: {exc.reason}")
    except Exception as exc:                            # noqa: BLE001
        return IdentifyResult(status="error", note=f"request failed: {exc}")

    if status != 200:
        # Never echo the payload verbatim — it can contain the key on auth errs.
        detail = (payload.get("error") or {}).get("message", "") \
            if isinstance(payload, dict) else ""
        return IdentifyResult(status="error",
                              note=f"identify service returned {status}"
                                   + (f": {detail[:120]}" if detail else ""))
    if payload.get("stop_reason") == "refusal":
        return IdentifyResult(status="error", note="request was declined")

    text = "".join(b.get("text", "") for b in (payload.get("content") or [])
                   if b.get("type") == "text")
    data = _extract_json(text)
    raw = (data.get("title") or "").strip() if data.get("title") else None
    variant = data.get("variant") or "unknown"
    confidence = data.get("confidence") or "low"

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
        note=f"read “{raw}” but no confident catalog match "
             f"(best {m.score:.0f})")
