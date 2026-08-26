"""
test_keystore.py — the desktop keystore + the phone-side sync path.

Covers: health needs no auth, /v1/keys refuses without/with a wrong bearer and
serves the stored creds with the right one, and local_server.sync_from_keystore
pulls those creds through end-to-end and leaves cached settings untouched when
the keystore is unreachable.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="spine-ks-"))
TOKEN = "test-bearer-abc123"
os.environ["SPINE_KEYSTORE_STORE"] = str(TMP / "keystore.json")

import keystore          # noqa: E402  (after env so STORE_PATH points at TMP)
import local_server      # noqa: E402
import settings          # noqa: E402

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), keystore.Handler)
    httpd.token = TOKEN
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _get(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    # Seed two credentials into the keystore store.
    store = keystore._store()
    store.set("scandex_token", "SCANDEX-XYZ")
    store.set("ebay_client_id", "EBAY-ID-1")
    httpd, port = _serve()
    base = f"http://127.0.0.1:{port}"

    # health: no auth
    st, body = _get(base + "/v1/health")
    check("health ok, no auth", st == 200 and body.get("ok") is True)

    # keys: refused without / with wrong token
    st, _ = _get(base + "/v1/keys")
    check("keys 401 without token", st == 401)
    st, _ = _get(base + "/v1/keys", token="wrong")
    check("keys 401 with wrong token", st == 401)

    # keys: served with the right token, only set fields present
    st, body = _get(base + "/v1/keys", token=TOKEN)
    keys = body.get("keys", {})
    check("keys 200 with token", st == 200)
    check("serves stored scandex_token", keys.get("scandex_token") == "SCANDEX-XYZ")
    check("serves stored ebay_client_id", keys.get("ebay_client_id") == "EBAY-ID-1")
    check("omits unset fields", "pricecharting_token" not in keys)

    # End-to-end: local_server.sync_from_keystore pulls them into app settings.
    app_settings = settings.Settings(TMP / "app_settings.json")
    app_settings.set("keystore_url", base)
    app_settings.set("keystore_token", TOKEN)
    local_server.configure(object(), False, None, None,
                           settings_store=app_settings)
    res = local_server.sync_from_keystore()
    check("sync ok", res.get("ok") is True)
    check("sync applied scandex", "scandex_token" in res.get("synced", []))
    check("app has scandex now", app_settings.get("scandex_token") == "SCANDEX-XYZ")
    check("scandex_ready true after sync", res.get("scandex_ready") is True)

    # Sync never overwrites the local keystore_url/token.
    check("keystore_url preserved", app_settings.get("keystore_url") == base)

    # Unreachable keystore leaves cached creds intact (best-effort contract).
    app_settings.set("keystore_url", "http://127.0.0.1:1")   # nothing listening
    before = app_settings.get("scandex_token")
    res2 = local_server.sync_from_keystore()
    check("unreachable reported not-ok", res2.get("ok") is False)
    check("cached creds untouched when unreachable",
          app_settings.get("scandex_token") == before)

    httpd.shutdown()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
