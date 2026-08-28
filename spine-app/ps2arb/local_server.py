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
import time
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
_OUTBOX = None
_PRICE_CACHE = None
_OUTCOMES = None
_PHASH = None
_STATIC = Path(__file__).parent / "static"

# How long to wait on the desktop's CLIP match before giving up and trying the
# next thing. Deliberately short: the desktop is either on the tailnet and
# answers in well under a second, or it is asleep and every extra second is
# dead time in front of the shelf. This used to inherit _vault_call's 12s
# default, which made an unreachable desktop stall the whole identify.
_PHOTO_MATCH_TIMEOUT = 3.5

# Once the desktop has failed to answer, stop asking for a while. Without this
# every scan pays the timeout again, which is worst exactly when it hurts most:
# working down a shelf with no signal, where the desktop is not coming back
# this minute and the offline table could have answered instantly. Any
# successful sync clears it, so a desktop that wakes up is used again at once.
_VAULT_DOWN_BACKOFF = 60.0
_vault_down_until = 0.0


def _vault_probably_down() -> bool:
    return time.monotonic() < _vault_down_until


def _note_vault_result(reachable: bool) -> None:
    global _vault_down_until
    _vault_down_until = 0.0 if reachable else (time.monotonic()
                                               + _VAULT_DOWN_BACKOFF)


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
    if not url:                                        # token optional (Tailscale)
        return {"ok": False, "detail": "keystore not configured"}

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url + "/v1/keys", headers=headers)
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
    if not url:                                        # token optional (Tailscale)
        return {"ok": False, "detail": "keystore not configured"}
    auth = {"Authorization": "Bearer " + token} if token else {}

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
        # Reachable: let identify try the desktop again immediately.
        _note_vault_result(True)
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

    # Confirmed photos queued while offline — flush the on-phone outbox to the
    # vault now. Best-effort; anything still unreachable stays queued.
    try:
        pf = _flush_photo_outbox()
        out["photos_synced"] = pf["synced"]
        out["photos_pending"] = pf["pending"]
    except Exception as exc:                            # noqa: BLE001
        out["photo_error"] = str(exc)

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

    # Precomputed resale estimates — PULL ONLY. The desktop is the only place
    # valuations are computed from real sources, so the phone never pushes;
    # it just imports the newest rows and reads them on the next scan.
    try:
        if _PRICE_CACHE is not None:
            rows = _get("/v1/vault/valuations").get("rows") or []
            out["prices_synced"] = _PRICE_CACHE.import_rows(rows)
            out["prices_total"] = _PRICE_CACHE.stats().get("skus", 0)
    except Exception as exc:                            # noqa: BLE001
        out["price_cache_error"] = str(exc)

    # Offline cover hashes — PULL ONLY, same shape as the price cache. This is
    # what lets a cover resolve with no network; keeping it fresh matters most
    # right after a seeding run added a few hundred covers.
    try:
        if _PHASH is not None:
            rows = _get("/v1/vault/phashes").get("rows") or []
            out["covers_synced"] = _PHASH.replace(rows)
            out["covers_total"] = _PHASH.count()
    except Exception as exc:                            # noqa: BLE001
        out["phash_error"] = str(exc)

    # Realized flips — push then pull, merged by id (newest wins), so the log
    # converges across devices and the desktop backtest can read every sale.
    try:
        if _OUTCOMES is not None:
            _post("/v1/vault/outcomes", {"rows": _OUTCOMES.export_rows()})
            pulled = _get("/v1/vault/outcomes").get("rows") or []
            out["outcomes_synced"] = _OUTCOMES.import_rows(pulled)
            out["outcomes_total"] = _OUTCOMES.stats().get("total", 0)
    except Exception as exc:                            # noqa: BLE001
        out["outcome_error"] = str(exc)

    return out


def _vault_call(method: str, path: str, payload=None, timeout: float = 12.0):
    """Authed request to the desktop vault (keystore). Returns parsed JSON, or
    None if the keystore isn't configured or is unreachable — callers fall back."""
    import json as _json
    import urllib.request
    if _SETTINGS is None:
        return None
    url = (_SETTINGS.get("keystore_url") or "").rstrip("/")
    token = _SETTINGS.get("keystore_token") or ""
    if not url:                                        # token optional (Tailscale)
        return None
    data = _json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode())
    except Exception:                                  # noqa: BLE001
        return None


def _phash_match(hex_hash: str) -> dict | None:
    """Offline cover lookup against the synced hash table.

    Thresholds come from settings so they are retunable from the desktop
    keystore without an APK rebuild; a bad or missing value falls back to the
    measured defaults rather than disabling matching."""
    if _PHASH is None or not hex_hash:
        return None
    import phash_index

    def _num(field, default):
        raw = (_SETTINGS.get(field) if _SETTINGS else "") or ""
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    res = _PHASH.match(hex_hash,
                       cutoff=_num("phash_cutoff", phash_index.CUTOFF),
                       margin=_num("phash_margin", phash_index.MARGIN))
    return res if res.get("matched") else None


def _flush_photo_outbox(limit: int = 50) -> dict:
    """Push queued photos to the vault, oldest first; drop each on success.

    Stops at the first UNREACHABLE result so the rest wait for the next sync
    (we don't want to churn while offline). A reachable-but-rejected photo
    (a genuinely bad image) is dropped so it can't wedge the queue; a vault
    that's up but not ready (no Pillow yet) is left to retry. Returns
    {synced, dropped, pending}."""
    if _OUTBOX is None:
        return {"synced": 0, "dropped": 0, "pending": 0}
    synced = dropped = 0
    for item in _OUTBOX.pending()[:limit]:
        r = _vault_call("POST", "/v1/vault/photo", {
            "image": item.get("image", ""), "title": item.get("title", ""),
            "variant": item.get("variant") or "unknown",
            "barcode": item.get("barcode") or ""})
        if r is None:
            break                                  # unreachable — retry later
        if r.get("ok"):
            _OUTBOX.remove(item.get("id", "")); synced += 1
        elif "Pillow" in (r.get("detail") or ""):
            break                                  # up but not ready; retry
        else:
            _OUTBOX.remove(item.get("id", "")); dropped += 1   # bad image
    return {"synced": synced, "dropped": dropped, "pending": _OUTBOX.count()}


def _push_outcomes_bg() -> None:
    """Best-effort background push of the flip log to the vault, so a buy/sale
    is backed up without blocking the request. Failures ride the next sync."""
    if _OUTCOMES is None:
        return
    def go():
        try:
            _vault_call("POST", "/v1/vault/outcomes",
                        {"rows": _OUTCOMES.export_rows()})
        except Exception:                              # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True, name="spine-outcomes-push").start()


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
    global _OUTBOX, _PRICE_CACHE, _OUTCOMES, _PHASH
    _SOURCE = source
    _SOURCE_IS_REAL = source_is_real
    _UPC = upc_index
    _SCANDEX = scandex_client
    _SETTINGS = settings_store
    if static_dir:
        _STATIC = Path(static_dir)
    # The photo outbox and the price cache live beside the barcode index — the
    # same app-private, writable directory — so both survive across launches.
    base = (Path(getattr(_UPC, "path", "")).parent if _UPC is not None
            else Path(__file__).parent)
    try:
        import photo_outbox
        _OUTBOX = photo_outbox.PhotoOutbox(base / "photo_outbox")
    except Exception:                                  # noqa: BLE001
        _OUTBOX = None
    # Precomputed resale estimates pulled from the desktop (see pricecache.py).
    # A missing/unbuilt cache just means every price is computed live.
    try:
        import pricecache
        _PRICE_CACHE = pricecache.PriceCache(base / "pricecache.db")
    except Exception:                                  # noqa: BLE001
        _PRICE_CACHE = None
    # The realized-flip log: what you paid and later sold for. Feeds the
    # eventual quantile recalibration; syncs to the desktop vault.
    try:
        import outcomes
        _OUTCOMES = outcomes.OutcomeLog(base / "outcomes.db")
    except Exception:                                  # noqa: BLE001
        _OUTCOMES = None
    # Cover hashes pulled from the desktop, for identifying a cover with no
    # network at all. An empty table just means offline identify abstains.
    try:
        import phash_index
        _PHASH = phash_index.PhashIndex(base / "phash_index.json")
    except Exception:                                  # noqa: BLE001
        _PHASH = None
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

            if route == "/api/outcomes":
                if _OUTCOMES is None:
                    return self._send(200, {"open": [], "stats": {}})
                return self._send(200, {"open": _OUTCOMES.open_flips(),
                                        "stats": _OUTCOMES.stats()})

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
                    local_pickup=bool(body.get("local_pickup", False)),
                    price_cache=_PRICE_CACHE))

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

            if route == "/api/outcome/buy":
                if _OUTCOMES is None:
                    return self._send(503, {"detail": "no outcome log"})
                if not body.get("title") or body.get("paid") in (None, ""):
                    raise core.ApiError(400, "title and paid are required")
                region = body.get("region") or "ntsc_u"
                variant = body.get("variant") or "unknown"
                completeness = body.get("completeness") or "loose"
                sku = (body.get("sku")
                       or f"{body['title']}|{region}|{variant}|{completeness}")
                fid = _OUTCOMES.record_buy(
                    sku=sku, title=body.get("title", ""),
                    paid=float(body["paid"]), ask=float(body.get("ask") or 0.0),
                    ship_in=float(body.get("ship_in") or 0.0),
                    region=region, variant=variant, completeness=completeness,
                    prediction=body.get("prediction") or {},
                    note=body.get("note", ""))
                _push_outcomes_bg()
                return self._send(200, {"ok": True, "id": fid,
                                        "stats": _OUTCOMES.stats()})

            if route == "/api/outcome/sell":
                if _OUTCOMES is None:
                    return self._send(503, {"detail": "no outcome log"})
                fid = body.get("id")
                if not fid or body.get("sold_price") in (None, ""):
                    raise core.ApiError(400, "id and sold_price are required")
                ok = _OUTCOMES.record_sale(
                    fid, sold_price=float(body["sold_price"]),
                    sold_shipping=float(body.get("sold_shipping") or 0.0),
                    fees=(float(body["fees"]) if body.get("fees") not in (None, "")
                          else None),
                    sold_on=body.get("sold_on") or None,
                    note=body.get("note"))
                if not ok:
                    raise core.ApiError(404, "no such flip")
                _push_outcomes_bg()
                return self._send(200, {"ok": True, "flip": _OUTCOMES.get(fid),
                                        "stats": _OUTCOMES.stats()})

            if route == "/api/identify":
                import identify
                img = body.get("image") or ""
                if img.startswith("data:") and "," in img:
                    img = img.split(",", 1)[1]        # strip the data: URL prefix
                media = body.get("media_type") or "image/jpeg"

                def _priced(out, title, variant):
                    try:
                        out["price"] = core.value(_SOURCE, _SOURCE_IS_REAL,
                                                  title=title, variant=variant,
                                                  price_cache=_PRICE_CACHE)
                    except core.ApiError as exc:
                        out["price_error"] = exc.detail
                    return out

                # 1. Your own photo library first — free and instant on a
                # repeat cover, no vision-API call at all. Short timeout: an
                # asleep desktop must not stall the paths below it, and after
                # one failure we stop asking for a minute (see the backoff).
                vm = None
                if not _vault_probably_down():
                    vm = _vault_call("POST", "/v1/vault/photo/match",
                                     {"image": img},
                                     timeout=_PHOTO_MATCH_TIMEOUT)
                    _note_vault_result(vm is not None)
                if vm and vm.get("matched"):
                    hit = vm["matched"]
                    title = hit.get("title")
                    return self._send(200, _priced({
                        "status": "matched", "title": title, "raw_title": title,
                        "variant": hit.get("variant") or "unknown",
                        "confidence": "high", "source": "photo-index",
                        "note": "recognized from your photo library"},
                        title, hit.get("variant") or "unknown"))

                # 2. The desktop did not answer (asleep, or no signal). Try the
                # offline table using the hash the browser computed on-device.
                # Less accurate than CLIP, so it abstains rather than guesses —
                # but it is the ONLY thing that works with no network at all.
                pm = _phash_match(body.get("phash") or "")
                if pm:
                    hit = pm["matched"]
                    title = hit.get("title")
                    return self._send(200, _priced({
                        "status": "matched", "title": title, "raw_title": title,
                        "variant": hit.get("variant") or "unknown",
                        "confidence": "medium", "source": "offline-covers",
                        "note": "matched offline against your cover library — "
                                "check the spine before you pay"},
                        title, hit.get("variant") or "unknown"))

                # 3. Fall back to the vision model.
                s = _SETTINGS
                provider = (s.get("vision_provider") if s else "") or None
                key = (s.get("vision_api_key") if s else "") \
                    or (s.get("anthropic_api_key") if s else "") or ""
                model = (s.get("vision_model") if s else "") or None
                base = (s.get("vision_base_url") if s else "") or None
                res = identify.identify_cover(
                    img, media, provider=provider, api_key=key,
                    model=model, base_url=base)
                out = {"status": res.status, "raw_title": res.raw_title,
                       "title": res.title, "variant": res.variant,
                       "confidence": res.confidence, "note": res.note,
                       "match_score": round(res.match_score, 1),
                       "source": "vision"}
                if res.usable:
                    _priced(out, res.title, res.variant)
                elif res.status == "error" and _PHASH is not None:
                    # Everything failed, which usually means no network at all.
                    # Say why the offline table did not save it, so the miss is
                    # actionable ("get closer / hold it square") rather than a
                    # blank wall.
                    off = _PHASH.match(body.get("phash") or "")
                    why = {"empty": "no covers synced yet — sync while you have "
                                    "signal to enable offline matching",
                           "no_hash": "this build could not hash the photo",
                           "far": "no close enough cover offline — try filling "
                                  "the frame with the front cover, square on",
                           "ambiguous": "offline match was too close to call "
                                        "between similar covers"}
                    out["offline_note"] = why.get(off.get("reason"), "")
                    out["offline_covers"] = _PHASH.count()
                return self._send(200, out)

            if route == "/api/identify/remember":
                # Store a confirmed photo + label so the photo index grows and
                # future scans of it are free. Write to the on-phone outbox
                # FIRST (so an offline capture is never lost), then flush to the
                # vault in the background; whatever can't send now is retried on
                # the next sync.
                img = body.get("image") or ""
                if img.startswith("data:") and "," in img:
                    img = img.split(",", 1)[1]
                title = body.get("title") or ""
                if not img or not title:
                    return self._send(400, {"detail": "image and title required"})
                item = {"image": img, "title": title,
                        "variant": body.get("variant") or "unknown",
                        "barcode": body.get("barcode") or ""}
                if _OUTBOX is not None:
                    _OUTBOX.enqueue(item)
                    threading.Thread(target=_flush_photo_outbox, daemon=True,
                                     name="spine-photo-flush").start()
                    return self._send(200, {"ok": True, "queued": True,
                                            "pending": _OUTBOX.count()})
                # No local outbox (e.g. desktop): best-effort direct push.
                r = _vault_call("POST", "/v1/vault/photo", item)
                return self._send(200, r or {"ok": False,
                                             "detail": "vault unreachable"})

            if route == "/api/identify/shelf":
                # One photo of several spines/covers -> a list of titles+prices.
                import identify
                img = body.get("image") or ""
                if img.startswith("data:") and "," in img:
                    img = img.split(",", 1)[1]
                media = body.get("media_type") or "image/jpeg"
                s = _SETTINGS
                provider = (s.get("vision_provider") if s else "") or None
                key = (s.get("vision_api_key") if s else "") \
                    or (s.get("anthropic_api_key") if s else "") or ""
                model = (s.get("vision_model") if s else "") or None
                base = (s.get("vision_base_url") if s else "") or None
                results = identify.identify_shelf(
                    img, media, provider=provider, api_key=key,
                    model=model, base_url=base)
                items = []
                for r in results:
                    item = {"status": r.status, "raw_title": r.raw_title,
                            "title": r.title, "variant": r.variant,
                            "confidence": r.confidence, "note": r.note}
                    if r.usable:
                        try:
                            item["price"] = core.value(
                                _SOURCE, _SOURCE_IS_REAL, title=r.title,
                                variant=r.variant, price_cache=_PRICE_CACHE)
                        except core.ApiError as exc:
                            item["price_error"] = exc.detail
                    items.append(item)
                return self._send(200, {"results": items})

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
                # Autosync this one scan to the vault in the background. If it
                # fails it's already saved in the local index and rides the
                # next full sync (launch or the manual button), so we never
                # block the scan on the network.
                try:
                    from dataclasses import asdict as _asdict
                    entry = _asdict(saved)
                    threading.Thread(
                        target=lambda: _vault_call(
                            "POST", "/v1/vault/upc", {"entries": [entry]}),
                        daemon=True, name="spine-upc-push").start()
                except Exception:                          # noqa: BLE001
                    pass
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
        from datetime import date
        today = date(2026, 8, 22)
        try:
            import sources
            source, source_is_real = sources.build_source(today=today)
        except Exception:                              # noqa: BLE001
            import mock_sources as ms
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
