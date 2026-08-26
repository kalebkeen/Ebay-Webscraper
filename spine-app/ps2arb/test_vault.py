"""
test_vault.py — the data vault merge rule, the server routes, and the
end-to-end phone backup/restore through local_server.sync_vault.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="spine-vault-"))
TOKEN = "vault-bearer-xyz"
os.environ["SPINE_KEYSTORE_STORE"] = str(TMP / "keystore.json")
os.environ["SPINE_VAULT_DB"] = str(TMP / "vault.db")
os.environ["SCANDEX_CACHE"] = str(TMP / "scandex_cache.json")
os.environ["SPINE_CATALOG_OVERRIDE"] = str(TMP / "catalog_override.json")

import keystore        # noqa: E402
import local_server    # noqa: E402
import settings        # noqa: E402
import upc             # noqa: E402
import vault           # noqa: E402
import scandex         # noqa: E402
import catalog         # noqa: E402

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), keystore.Handler)
    httpd.token = TOKEN
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    # --- unit: the merge rule ---
    confirmed = {"upc": "1", "title": "Ico", "variant": "black_label",
                 "confirmed_by": "user", "first_seen": "2026-01-02",
                 "times_scanned": 5}
    unknownish = {"upc": "1", "title": "Ico", "variant": "unknown",
                  "confirmed_by": "user", "first_seen": "2026-01-01",
                  "times_scanned": 2}
    m = upc.merge_two(confirmed, unknownish)
    check("confirmed variant beats unknown", m["variant"] == "black_label")
    check("times_scanned is max not sum", m["times_scanned"] == 5)
    check("first_seen keeps earliest", m["first_seen"] == "2026-01-01")

    # --- server: push, pull, and no-downgrade ---
    httpd, port = _serve()
    base = f"http://127.0.0.1:{port}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Authorization": "Bearer " + TOKEN,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    call("POST", "/v1/vault/upc", {"entries": [confirmed]})
    got = call("GET", "/v1/vault/upc")["entries"]
    check("vault stored the entry", len(got) == 1 and got[0]["upc"] == "1")
    # Push a weaker record for the same barcode — must not clobber.
    call("POST", "/v1/vault/upc", {"entries": [unknownish]})
    got = call("GET", "/v1/vault/upc")["entries"]
    check("vault did not downgrade confirmed variant",
          got[0]["variant"] == "black_label")

    # --- end-to-end: phone A backs up, phone B (reset) restores ---
    phoneA = upc.UpcIndex(TMP / "phoneA.json")
    phoneA.teach("0711719577966", "Gran Turismo 4", variant="greatest_hits")
    app = settings.Settings(TMP / "appA.json")
    app.set("keystore_url", base)
    app.set("keystore_token", TOKEN)
    local_server.configure(object(), False, phoneA, None, settings_store=app)
    res = local_server.sync_vault()
    check("phone A backup ok", res.get("ok") is True and res.get("backed_up") >= 1)

    phoneB = upc.UpcIndex(TMP / "phoneB.json")          # a fresh/reset device
    check("phone B starts empty", phoneB.lookup("0711719577966") is None)
    local_server.configure(object(), False, phoneB, None, settings_store=app)
    local_server.sync_vault()
    restored = phoneB.lookup("0711719577966")
    check("phone B restored the spine", restored is not None
          and restored.title == "Gran Turismo 4")
    check("restored variant preserved", restored is not None
          and restored.variant == "greatest_hits")

    # --- ScanDex cache vaulting (matched must not be downgraded by a miss) ---
    matched = {"barcode": "012345678905", "status": "matched", "fetched": 100,
               "payload": {"status": "matched", "fetched": 100,
                           "igdb_metadata": {"name": "X"}}}
    miss = {"barcode": "012345678905", "status": "absent", "fetched": 200,
            "payload": {"status": "absent", "fetched": 200}}
    call("POST", "/v1/vault/scandex", {"entries": [matched]})
    call("POST", "/v1/vault/scandex", {"entries": [miss]})   # newer, but a miss
    sx = {e["barcode"]: e for e in call("GET", "/v1/vault/scandex")["entries"]}
    check("scandex matched not downgraded by newer miss",
          sx["012345678905"]["status"] == "matched")

    # end-to-end: a local ScanDex cache backs up through sync_vault
    Path(os.environ["SCANDEX_CACHE"]).write_text(json.dumps({
        "099887766554": {"status": "matched", "fetched": 300,
                         "igdb_metadata": {"name": "Y"}}}))
    r3 = local_server.sync_vault()
    check("sync backed up scandex", r3.get("scandex_backed_up", 0) >= 1)
    codes = {e["barcode"] for e in call("GET", "/v1/vault/scandex")["entries"]}
    check("scandex reached the vault", "099887766554" in codes)

    # --- catalog vaulting + override consumption ---
    vault.replace_catalog([
        {"canonical": "Test Title A", "regions": ["NA"], "aliases": ["tta"],
         "liquidity": "high"},
        {"canonical": "Test Title B", "regions": ["JP"], "aliases": [],
         "liquidity": "thin"}])
    check("vault serves catalog snapshot",
          len(call("GET", "/v1/vault/catalog")["entries"]) == 2)

    ov = Path(os.environ["SPINE_CATALOG_OVERRIDE"])
    ov.write_text(json.dumps([{"canonical": "Override Only Game",
                               "regions": ["NA"], "aliases": [],
                               "liquidity": "low"}]))
    src = catalog._bulk_source()
    check("catalog override consumed",
          len(src) == 1 and src[0][0] == "Override Only Game")
    ov.unlink()
    check("catalog falls back to bundled when no override",
          len(catalog._bulk_source()) > 1000)

    local_server.sync_vault()   # pulls the vault catalog into the override file
    written = json.loads(ov.read_text())
    check("sync wrote catalog override from vault",
          {d["canonical"] for d in written} == {"Test Title A", "Test Title B"})

    # --- best-effort: unreachable leaves local intact ---
    app.set("keystore_url", "http://127.0.0.1:1")
    before = len(phoneB.all_entries())
    r2 = local_server.sync_vault()
    check("unreachable reported not-ok", r2.get("ok") is False)
    check("local index untouched when unreachable",
          len(phoneB.all_entries()) == before)

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
