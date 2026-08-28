"""
keystore.py — the desktop key server. DESKTOP-ONLY (never bundled into the APK).

One source of truth for durable service credentials (eBay client id/secret,
ScanDex token, PriceCharting token). The phone fetches the current values from
here instead of having them pasted in per-device. Rotate a key once, on this
desktop, and every device picks it up on its next sync.

Why this is safe to serve over the network: it is reached only over Tailscale,
whose WireGuard transport is already encrypted, and every request must carry a
bearer token (`hmac.compare_digest`). It binds all interfaces by default so the
Tailscale IP works, but nothing answers without the token.

Standard library only, same as the rest of the pipeline.

USAGE
    python keystore.py init                 # generate the bearer token (once)
    python keystore.py set ebay_client_id ...      # store a credential
    python keystore.py set ebay_client_secret ...
    python keystore.py set scandex_token ...
    python keystore.py list                 # masked view of what's stored
    python keystore.py serve                # run the server (foreground)

    # phone side: enter keystore_url = http://<this-desktop>.tailXXXX.ts.net:8787
    # and keystore_token = <the token from `init`> in the app's settings panel.

FILES (all in this directory, all chmod 0600)
    keystore.json          the stored credentials (reuses settings.Settings)
    keystore_token.txt     the bearer token

ENV OVERRIDES
    SPINE_KEYSTORE_STORE   path to keystore.json
    SPINE_KEYSTORE_TOKEN   bearer token (else read from keystore_token.txt)
    KEYSTORE_HOST          bind host   (default 0.0.0.0)
    KEYSTORE_PORT          bind port   (default 8787)
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import settings as _settings
import vault as _vault

HERE = Path(__file__).resolve().parent
STORE_PATH = Path(os.environ.get("SPINE_KEYSTORE_STORE", HERE / "keystore.json"))
TOKEN_PATH = HERE / "keystore_token.txt"
# Precomputed resale estimates that precompute.py fills on this desktop; the
# phone pulls them read-only. Same DB file precompute.py writes to.
PRICECACHE_PATH = Path(os.environ.get("PS2ARB_PRICECACHE", HERE / "pricecache.db"))


def _all_valuations() -> dict:
    """Every precomputed estimate row, for the phone to pull. Opens the cache
    per request so it always reflects the latest precompute run and never
    holds a lock; a missing cache is simply an empty list."""
    try:
        import pricecache
        pc = pricecache.PriceCache(PRICECACHE_PATH)
        try:
            return {"rows": pc.export_rows()}
        finally:
            pc.close()
    except Exception as exc:                            # noqa: BLE001
        return {"rows": [], "error": str(exc)}


# The realized-flip log the phone pushes up and the desktop backtest reads.
# Same OutcomeLog store, opened per request. Merged by id (newest wins).
OUTCOMES_STORE_PATH = Path(os.environ.get("PS2ARB_OUTCOMES", HERE / "outcomes.db"))


def _all_outcomes() -> dict:
    try:
        import outcomes
        log = outcomes.OutcomeLog(OUTCOMES_STORE_PATH)
        try:
            return {"rows": log.export_rows()}
        finally:
            log.close()
    except Exception as exc:                            # noqa: BLE001
        return {"rows": [], "error": str(exc)}


def _merge_outcomes(rows) -> dict:
    try:
        import outcomes
        log = outcomes.OutcomeLog(OUTCOMES_STORE_PATH)
        try:
            n = log.import_rows(rows or [])
            return {"ok": True, "merged": n, "total": log.stats().get("total", 0)}
        finally:
            log.close()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "detail": str(exc)}
def _local_vision(image_b64: str) -> dict:
    """CLIP first; if it is not sure, let a desktop vision model choose from
    CLIP's shortlist.

    This runs on the DESKTOP for three reasons: the CLIP embeddings are here,
    Ollama is here on loopback where it cannot be reached by anything else, and
    the phone already talks to this server over the tailnet. Nothing new is
    exposed and the phone needs no extra configuration.

    Returns the same {"matched": ...} shape as /v1/vault/photo/match, plus
    `via` saying which tier answered, so the caller can label it honestly
    instead of passing a model's guess off as a library hit."""
    hit = _vault.match_photo(image_b64)
    if hit.get("matched"):
        return {**hit, "via": "photo-index"}

    store = _store()
    url = store.get("local_vision_url")
    model = store.get("local_vision_model")
    cands = _vault.candidates(image_b64, k=5)
    if not url or not model:
        return {"matched": None, "via": None, "candidates": cands,
                "detail": "no local vision model configured"}
    if not cands:
        # Without a shortlist this is just a slow open-ended guess, and a local
        # small model is measurably poor at that (50%). Let the caller fall
        # through to the cloud model instead of pretending.
        return {"matched": None, "via": None, "candidates": [],
                "detail": "no CLIP candidates (index empty or CLIP unavailable)"}
    try:
        import identify
        res = identify.identify_cover(
            image_b64, "image/jpeg", provider="openai",
            api_key=store.get("vision_api_key") or "local", model=model,
            base_url=url, candidates=cands,
            # Local reasoning models otherwise burn the whole budget thinking
            # and return empty content.
            extra={"reasoning_effort": "none"})
    except Exception as exc:                                # noqa: BLE001
        return {"matched": None, "via": None, "candidates": cands,
                "detail": f"local vision failed: {exc}"}
    if not res.usable:
        return {"matched": None, "via": None, "candidates": cands,
                "detail": res.note or res.status}
    return {"matched": {"title": res.title,
                        "variant": res.variant or "unknown", "barcode": ""},
            "via": "local-vision", "confidence": res.confidence,
            "candidates": cands}


VERSION = "1.0"

# Tailscale address ranges. In "open" mode the keystore serves token-free but
# ONLY to clients whose source IP is on the tailnet (or loopback) — so it is
# never exposed to the LAN even though it binds all interfaces. Tailscale itself
# is the auth layer: only your own devices can reach these addresses.
_TAILNET = [ipaddress.ip_network("100.64.0.0/10"),        # CGNAT (Tailscale v4)
            ipaddress.ip_network("fd7a:115c:a1e0::/48")]  # Tailscale ULA (v6)


def _is_trusted_client(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_loopback or any(ip in net for net in _TAILNET)

# The keystore holds and serves only durable service credentials — never the
# phone's keystore_url / keystore_token, which are how the phone reaches it.
SERVED = _settings.KEYSTORE_SERVED_FIELDS
# Settable from the CLI but never handed to a phone: desktop-local config such
# as the loopback URL of the vision model. See settings.KEYSTORE_LOCAL_FIELDS.
SETTABLE = SERVED | _settings.KEYSTORE_LOCAL_FIELDS


def _store() -> _settings.Settings:
    return _settings.Settings(STORE_PATH)


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------

def load_token() -> str:
    tok = os.environ.get("SPINE_KEYSTORE_TOKEN", "")
    if tok:
        return tok
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def init_token(force: bool = False) -> str:
    if TOKEN_PATH.exists() and not force:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(tok, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SpineKeystore/1.0"

    def log_message(self, fmt, *args):  # keep secrets/paths out of stdout noise
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        # A valid bearer token always works (defense in depth, other devices).
        want = getattr(self.server, "token", "")       # type: ignore[attr-defined]
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        got = header[len(prefix):] if header.startswith(prefix) else header
        if want and hmac.compare_digest(got, want):
            return True
        # Open mode: token-free, but only for tailnet/loopback source IPs.
        if getattr(self.server, "open_mode", False) \
                and _is_trusted_client(self.client_address[0]):
            return True
        return False

    def do_GET(self):
        if self.path == "/v1/health":
            return self._send(200, {"ok": True, "service": "spine-keystore",
                                    "version": VERSION})
        if self.path == "/v1/keys":
            if not self._authed():
                return self._send(401, {"detail": "missing or bad bearer token"})
            store = _store()
            keys = {f: store.get(f) for f in SERVED if store.get(f)}
            return self._send(200, {"keys": keys})
        vault_gets = {
            "/v1/vault/upc": lambda: {"entries": _vault.all_upc()},
            "/v1/vault/scandex": lambda: {"entries": _vault.all_scandex()},
            "/v1/vault/catalog": lambda: {"entries": _vault.all_catalog()},
            "/v1/vault/valuations": _all_valuations,
            "/v1/vault/outcomes": _all_outcomes,
            "/v1/vault/phashes": lambda: {"rows": _vault.all_phashes()},
            "/v1/vault/photo/coverage": _vault.photo_coverage,
            "/v1/vault/stats": _vault.stats,
        }
        if self.path in vault_gets:
            if not self._authed():
                return self._send(401, {"detail": "missing or bad bearer token"})
            return self._send(200, vault_gets[self.path]())
        return self._send(404, {"detail": "no such route"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"detail": "missing or bad bearer token"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"detail": "body must be JSON"})

        # Merge endpoints back the phone's data up to the vault.
        posters = {
            "/v1/vault/upc": _vault.merge_upc,
            "/v1/vault/scandex": _vault.merge_scandex,
        }
        if self.path in posters:
            return self._send(200, posters[self.path](body.get("entries") or []))

        # Realized flips push (uses "rows", merged by id).
        if self.path == "/v1/vault/outcomes":
            return self._send(200, _merge_outcomes(body.get("rows") or []))

        # Photo index: match a query photo, or store a confirmed one.
        if self.path == "/v1/vault/photo/match":
            return self._send(200, _vault.match_photo(body.get("image") or ""))
        # CLIP, then the desktop vision model on CLIP's shortlist. Separate
        # route so an older phone build keeps the plain match semantics.
        if self.path == "/v1/vault/photo/identify":
            return self._send(200, _local_vision(body.get("image") or ""))
        if self.path == "/v1/vault/photo":
            return self._send(200, _vault.add_photo(
                body.get("image") or "", body.get("title") or "",
                body.get("variant") or "unknown", body.get("barcode") or "",
                source=body.get("source") or "user",
                face=body.get("face") or "front"))
        return self._send(404, {"detail": "no such route"})

    def do_PUT(self):
        # PUT /v1/keys/<field>  body: {"value": "..."} — set/rotate remotely.
        if not self._authed():
            return self._send(401, {"detail": "missing or bad bearer token"})
        prefix = "/v1/keys/"
        if not self.path.startswith(prefix):
            return self._send(404, {"detail": "no such route"})
        field = self.path[len(prefix):]
        if field not in SERVED:
            return self._send(400, {"detail": f"unknown field '{field}'"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"detail": "body must be JSON"})
        store = _store()
        store.set(field, str(body.get("value", "")))
        return self._send(200, {"ok": True, "field": field})


def serve(host: str | None = None, port: int | None = None) -> None:
    open_mode = os.environ.get("SPINE_KEYSTORE_OPEN") == "1"
    token = load_token()
    if not token and not open_mode:
        sys.exit("No keystore token. Run:  python keystore.py init  "
                 "(or 'serve --open' for token-free Tailscale-only access)")
    host = host or os.environ.get("KEYSTORE_HOST", "0.0.0.0")
    port = int(port or os.environ.get("KEYSTORE_PORT", "8787"))

    # Snapshot this desktop's bulk catalog into the vault so phones can pull
    # title-list updates without a rebuild. Cheap and idempotent (full replace).
    try:
        import catalog_data
        res = _vault.replace_catalog([
            {"canonical": r[0], "regions": list(r[1]), "aliases": list(r[2]),
             "liquidity": r[3] if len(r) > 3 else "low"}
            for r in catalog_data.BULK])
        print(f"  catalog snapshot -> vault: {res.get('stored', 0)} titles")
    except Exception as exc:                            # noqa: BLE001
        print(f"  (catalog snapshot skipped: {exc})")

    # Fill in box hashes for any covers stored before that column existed, so
    # the phone's offline table is complete. Idempotent and a no-op once caught
    # up, so it is safe on every start.
    try:
        bf = _vault.backfill_bhash()
        if bf.get("filled"):
            print(f"  offline cover hashes: +{bf['filled']} "
                  f"({bf.get('remaining', 0)} still missing)")
    except Exception as exc:                            # noqa: BLE001
        print(f"  (cover hash backfill skipped: {exc})")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.token = token                               # type: ignore[attr-defined]
    httpd.open_mode = open_mode                       # type: ignore[attr-defined]
    shown = _store()
    have = [f for f in SERVED if shown.get(f)]
    auth = ("Tailscale/loopback source IPs — token-free (open mode)"
            if open_mode else "bearer token required")
    print(f"spine-keystore {VERSION} on http://{host}:{port}  "
          f"(serving {len(have)} credential(s): {', '.join(have) or 'none yet'})")
    print(f"  auth: {auth}")
    print("reach it from the phone at http://<this-desktop>.<tailnet>.ts.net:"
          f"{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "init":
        tok = init_token(force="--force" in rest)
        print("keystore token (enter this once in the app as keystore_token):\n")
        print("   " + tok + "\n")
        print(f"stored in {TOKEN_PATH}")
        return 0

    if cmd == "serve":
        if "--open" in rest:
            os.environ["SPINE_KEYSTORE_OPEN"] = "1"
        serve()
        return 0

    if cmd == "list":
        store = _store()
        for f in sorted(SETTABLE):
            m = store.masked().get(f, {})
            state = m.get("hint") if m.get("set") else "—"
            print(f"  {f:22} {state}")
        return 0

    if cmd == "set":
        if len(rest) < 2:
            print("usage: keystore.py set <field> <value>")
            return 2
        field, value = rest[0], rest[1]
        if field not in SETTABLE:
            print(f"unknown field '{field}'. one of: "
                  f"{', '.join(sorted(SETTABLE))}")
            return 2
        _store().set(field, value)
        print(f"set {field}")
        return 0

    if cmd == "get":
        if not rest:
            print("usage: keystore.py get <field>")
            return 2
        val = _store().get(rest[0])
        print(val or "(unset)")
        return 0

    print(f"unknown command '{cmd}'. try: init | set | get | list | serve")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
