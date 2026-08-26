"""
local_server.py — the API on http.server, for running inside the APK.

Same routes as service.py, same responses, no framework. Both are thin
transports over core.py, so there is exactly one copy of the pricing logic
and the phone cannot quietly disagree with the desktop about what a disc is
worth.

Chaquopy can install pure-Python wheels but not compiled ones, which rules
out pydantic and therefore FastAPI. The standard library's ThreadingHTTPServer
is entirely adequate here: one user, a handful of requests, all of them
local. It binds to 127.0.0.1 so nothing on the network can reach it.

Called from Kotlin as:

    from local_server import start
    port = start()          # returns the bound port; runs in a daemon thread
"""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core

# Populated by configure(); kept module-level so the handler class can reach
# them without threading state through BaseHTTPRequestHandler's constructor.
_SOURCE = None
_SOURCE_IS_REAL = False
_UPC = None
_SCANDEX = None
_EBAY = None
_SETTINGS = None
_STATIC = Path(__file__).parent / "static"


def rebuild_clients() -> None:
    """Recreate API clients from current settings.

    Called after credentials change so a saved key takes effect immediately.
    Without this, entering a token appears to do nothing until the app is
    force-quit, which reads as a bug rather than a restart requirement.
    """
    global _SCANDEX, _EBAY
    if _SETTINGS is None:
        return
    _SCANDEX = None
    _EBAY = None
    if _SETTINGS.scandex_ready:
        try:
            import scandex
            c = scandex.ScanDexClient(token=_SETTINGS.get("scandex_token"))
            _SCANDEX = c if c.configured else None
        except Exception as exc:                       # noqa: BLE001
            print(f"spine: scandex init failed ({exc})")
    if _SETTINGS.ebay_ready:
        try:
            import ebay
            _EBAY = ebay.EbayClient()
        except Exception as exc:                       # noqa: BLE001
            print(f"spine: ebay init failed ({exc})")


def sync_from_keystore() -> dict:
    """Pull current service credentials from the desktop keystore and apply them.

    Best-effort by design: if the keystore is unconfigured, unreachable, or
    rejects the token, the cached credentials in settings.json are left exactly
    as they were, so scanning keeps working on the last-synced keys. This is the
    whole point of the keystore — rotate a key once on the desktop and the phone
    picks it up here, but being offline in a shop never breaks anything.
    """
    import json as _json
    import urllib.error
    import urllib.request
    import settings as _settings

    if _SETTINGS is None:
        return {"ok": False, "detail": "settings unavailable"}
    url = (_SETTINGS.get("keystore_url") or "").rstrip("/")
    token = _SETTINGS.get("keystore_token") or ""
    if not url or not token:
        return {"ok": False, "detail": "keystore not configured"}

    req = urllib.request.Request(
        url + "/v1/keys",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = _json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"keystore returned {exc.code}"}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "detail": f"keystore unreachable: {exc}"}

    keys = payload.get("keys") or {}
    # Only durable service fields; never let a sync overwrite the local
    # keystore_url / keystore_token the phone needs to reach the keystore.
    clean = {k: v for k, v in keys.items()
             if k in _settings.KEYSTORE_SERVED_FIELDS and v}
    changed = _SETTINGS.update(clean) if clean else []
    if changed:
        rebuild_clients()
    return {"ok": True, "synced": changed,
            "scandex_ready": _SETTINGS.scandex_ready,
            "ebay_ready": _SETTINGS.ebay_ready}


def sync_vault() -> dict:
    """Back the learned barcode index, the ScanDex cache, and pull the catalog
    override, from the desktop vault. Best-effort: an unreachable vault leaves
    everything local untouched.

    Push then pull, both through the merge rules, so it is safe to run on every
    launch and converges no matter which side is ahead. The catalog is pulled
    into a local override that catalog.py picks up on the NEXT launch, so title
    updates arrive without an APK rebuild."""
    import json as _json
    import urllib.error
    import urllib.request
    from pathlib import Path as _Path

    if _SETTINGS is None:
        return {"ok": False, "detail": "vault unavailable"}
    url = (_SETTINGS.get("keystore_url") or "").rstrip("/")
    token = _SETTINGS.get("keystore_token") or ""
    if not url or not token:
        return {"ok": False, "detail": "keystore not configured"}
    auth = {"Authorization": "Bearer " + token}

    def _post(path, payload):
        req = urllib.request.Request(
            url + path, data=_json.dumps(payload).encode(),
            headers={**auth, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return _json.loads(resp.read().decode())

    def _get(path):
        req = urllib.request.Request(
            url + path, headers={**auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return _json.loads(resp.read().decode())

    out = {"ok": True}
    try:
        # Barcode index — the crown jewel. A failure here (unreachable) aborts
        # the whole sync and reports not-ok.
        if _UPC is not None:
            pushed = _post("/v1/vault/upc", {"entries": _UPC.all_entries()})
            payload = _get("/v1/vault/upc")
            out["backed_up"] = pushed.get("stored", 0)
            out["pulled_into_local"] = _UPC.merge_entries(payload.get("entries") or [])
            out["vault_total"] = pushed.get("total", 0)
        else:
            _get("/v1/vault/stats")   # still prove reachability if no index yet
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"vault returned {exc.code}"}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "detail": f"vault unreachable: {exc}"}

    # ScanDex cache — optional; its own failure doesn't fail the whole sync.
    try:
        import scandex
        sc_push = _post("/v1/vault/scandex", {"entries": scandex.cache_entries()})
        sc_pull = _get("/v1/vault/scandex")
        out["scandex_backed_up"] = sc_push.get("stored", 0)
        out["scandex_restored"] = scandex.merge_cache(sc_pull.get("entries") or [])
    except Exception as exc:                            # noqa: BLE001
        out["scandex_error"] = str(exc)

    # Catalog override — pulled for the NEXT launch to consume. Same path
    # catalog.py reads (SPINE_CATALOG_OVERRIDE keeps them in lockstep).
    try:
        import os as _os
        cat = _get("/v1/vault/catalog").get("entries") or []
        if cat:
            dest = _os.environ.get(
                "SPINE_CATALOG_OVERRIDE",
                str(_Path(__file__).parent / "catalog_override.json"))
            _Path(dest).write_text(_json.dumps(cat), encoding="utf-8")
            out["catalog_titles"] = len(cat)
    except Exception as exc:                            # noqa: BLE001
        out["catalog_error"] = str(exc)

    return out


def _startup_sync() -> None:
    """Keys first, then the barcode index — both best-effort, off the hot path."""
    try:
        sync_from_keystore()
        sync_vault()
    except Exception:                                  # noqa: BLE001
        pass


def configure(source, source_is_real: bool = False, upc_index=None,
              static_dir: Path | None = None, scandex_client=None,
              settings_store=None) -> None:
    global _SOURCE, _SOURCE_IS_REAL, _UPC, _STATIC, _SCANDEX, _SETTINGS
    _SOURCE = source
    _SOURCE_IS_REAL = source_is_real
    _UPC = upc_index
    _SCANDEX = scandex_client
    _SETTINGS = settings_store
    if static_dir:
        _STATIC = Path(static_dir)
    if _SETTINGS is not None and scandex_client is None:
        rebuild_clients()


class Handler(BaseHTTPRequestHandler):
    server_version = "Spine/1.0"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        """Silence. stderr on Android goes to logcat and this is noise."""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._send(404, {"detail": "not found"})
            return
        # Refuse anything that escapes the static root, even though this
        # only listens on loopback. A path-traversal hole is not worth
        # leaving open on the argument that nobody can reach it.
        try:
            path.resolve().relative_to(_STATIC.resolve())
        except ValueError:
            self._send(403, {"detail": "forbidden"})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------------------- routes

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/api/health":
                stats = _UPC.stats() if _UPC else {}
                return self._send(200, core.health(_SOURCE_IS_REAL, stats))

            if route == "/api/settings":
                if _SETTINGS is None:
                    return self._send(200, {"available": False, "fields": {}})
                return self._send(200, {
                    "available": True,
                    "fields": _SETTINGS.masked(),
                    "scandex_ready": _SETTINGS.scandex_ready,
                    "ebay_ready": _SETTINGS.ebay_ready})

            if route == "/api/titles":
                q = (query.get("q") or [""])[0]
                limit = int((query.get("limit") or ["12"])[0])
                return self._send(200, core.titles(q, limit))

            if route.startswith("/api/upc/"):
                code = urllib.parse.unquote(route[len("/api/upc/"):])
                if _UPC is None:
                    return self._send(503, {"detail": "no upc index"})

                # Local index first, ScanDex second. The local entry carries
                # a CONFIRMED variant; a ScanDex hit carries only a title,
                # and preferring the remote source would discard the more
                # valuable answer. See scandex.resolve.
                import scandex as _sd
                res = _sd.resolve(code, _UPC, _SCANDEX, _EBAY)
                if not res.title:
                    return self._send(200, {
                        "upc": res.barcode, "known": False,
                        "suggest": res.suggest,
                        "warnings": res.warnings})

                entry = core.entry_for(res.title)
                return self._send(200, {
                    "upc": res.barcode, "known": True, "title": res.title,
                    "variant": res.variant,
                    "source": res.source,
                    "trusted": res.confident,
                    "needs_variant_check": (res.variant in (None, "", "unknown")
                                            or res.source != "local"),
                    "warnings": res.warnings,
                    "has_greatest_hits": entry.has_greatest_hits if entry else None,
                    "liquidity": entry.liquidity if entry else None,
                    "repro_risk": entry.repro_risk if entry else None})

            if route == "/" or route == "":
                return self._file(_STATIC / "index.html")
            if route == "/sw.js":
                return self._file(_STATIC / "sw.js")
            if route == "/manifest.json":
                return self._file(_STATIC / "manifest.json")
            if route.startswith("/static/"):
                return self._file(_STATIC / route[len("/static/"):])

            return self._send(404, {"detail": "no such route"})

        except core.ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception as exc:                      # noqa: BLE001
            self._send(500, {"detail": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if route == "/api/value":
                if not body.get("title"):
                    raise core.ApiError(400, "title is required")
                return self._send(200, core.value(
                    _SOURCE, _SOURCE_IS_REAL,
                    title=body["title"],
                    variant=body.get("variant", "unknown"),
                    completeness=body.get("completeness", "loose"),
                    region=body.get("region", "ntsc_u"),
                    ask=body.get("ask"),
                    ship_in=float(body.get("ship_in") or 0.0),
                    local_pickup=bool(body.get("local_pickup", False))))

            if route == "/api/assess":
                return self._send(200, core.assess(
                    _SOURCE,
                    raw_title=body.get("raw_title", ""),
                    description=body.get("description", ""),
                    ask=float(body.get("ask") or 0.0),
                    ship_in=float(body.get("ship_in") or 0.0)))

            if route == "/api/keystore/sync":
                return self._send(200, sync_from_keystore())

            if route == "/api/vault/sync":
                return self._send(200, sync_vault())

            if route == "/api/identify":
                import identify
                img = body.get("image") or ""
                if img.startswith("data:") and "," in img:
                    img = img.split(",", 1)[1]        # strip the data: URL prefix
                media = body.get("media_type") or "image/jpeg"
                key = _SETTINGS.get("anthropic_api_key") if _SETTINGS else ""
                model = (_SETTINGS.get("vision_model") if _SETTINGS else "") or None
                res = identify.identify_cover(img, media, api_key=key, model=model)
                out = {"status": res.status, "raw_title": res.raw_title,
                       "title": res.title, "variant": res.variant,
                       "confidence": res.confidence, "note": res.note,
                       "match_score": round(res.match_score, 1)}
                if res.usable:
                    try:
                        out["price"] = core.value(
                            _SOURCE, _SOURCE_IS_REAL, title=res.title,
                            variant=res.variant)
                    except core.ApiError as exc:
                        out["price_error"] = exc.detail
                return self._send(200, out)

            if route == "/api/settings":
                if _SETTINGS is None:
                    return self._send(503, {"detail": "settings unavailable"})
                changed = _SETTINGS.update(body)
                rebuild_clients()
                return self._send(200, {
                    "saved": changed,
                    "fields": _SETTINGS.masked(),
                    "scandex_ready": _SETTINGS.scandex_ready,
                    "ebay_ready": _SETTINGS.ebay_ready})

            if route.startswith("/api/upc/"):
                code = urllib.parse.unquote(route[len("/api/upc/"):])
                if _UPC is None:
                    return self._send(503, {"detail": "no upc index"})
                title = body.get("title", "")
                if core.entry_for(title) is None:
                    raise core.ApiError(404, f"'{title}' is not in the catalog")
                saved = _UPC.teach(code, title,
                                   body.get("variant", "unknown"))
                return self._send(200, {"saved": True, "upc": code,
                                        "title": title,
                                        "observations": getattr(saved, "observations", 1)})

            return self._send(404, {"detail": "no such route"})

        except core.ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception as exc:                      # noqa: BLE001
            self._send(500, {"detail": f"{type(exc).__name__}: {exc}"})


def start(port: int = 0, source=None, source_is_real: bool = False,
          upc_index=None, static_dir: str | None = None,
          scandex_client=None, settings_store=None) -> int:
    """
    Start the server on a daemon thread and return the bound port.

    Port 0 lets the OS pick a free one, which matters on a phone: a fixed
    port can already be taken by another app, and the failure would be a
    blank WebView with nothing in the log to explain it.
    """
    if source is None:
        import mock_sources as ms
        from datetime import date
        today = date(2026, 8, 22)
        source = ms.CombinedSource(ms.MockMarketplace(seed=7, today=today),
                                   ms.MockReference(today))
        source_is_real = False

    if upc_index is None:
        try:
            import upc
            upc_index = upc.UpcIndex()
        except Exception:                              # noqa: BLE001
            upc_index = None

    if settings_store is None:
        try:
            import settings as _settings
            settings_store = _settings.Settings()
        except Exception:                              # noqa: BLE001
            settings_store = None

    configure(source, source_is_real, upc_index,
              Path(static_dir) if static_dir else None,
              scandex_client=scandex_client,
              settings_store=settings_store)

    # Pull the latest keys AND back up / restore the barcode index on launch,
    # off the hot path: best-effort, threaded, never blocks the server from
    # binding (a blocked start is a blank WebView with nothing to explain it).
    if settings_store is not None and settings_store.get("keystore_url"):
        threading.Thread(target=_startup_sync, daemon=True,
                         name="spine-startup-sync").start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    bound = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="spine-http")
    thread.start()
    return bound


if __name__ == "__main__":
    p = start(8765)
    print(f"serving on http://127.0.0.1:{p}  (ctrl-c to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
