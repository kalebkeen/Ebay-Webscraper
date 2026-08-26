"""
test_photo.py — the photo index (perceptual-hash store + match) and its
keystore routes and phone-side reach. Needs Pillow (desktop-only feature);
skips cleanly if it isn't installed.
"""
from __future__ import annotations

import base64
import io
import json
import os
import random
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="spine-photo-"))
TOKEN = "photo-bearer-1"
os.environ["SPINE_KEYSTORE_STORE"] = str(TMP / "keystore.json")
os.environ["SPINE_VAULT_DB"] = str(TMP / "vault.db")
os.environ["SPINE_VAULT_PHOTOS"] = str(TMP / "photos")

import keystore        # noqa: E402
import local_server    # noqa: E402
import settings        # noqa: E402
import vault           # noqa: E402

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


def noise_b64(seed: int, w: int = 64, h: int = 64) -> str:
    from PIL import Image
    r = random.Random(seed)
    img = Image.new("L", (w, h))
    img.putdata([r.randint(0, 255) for _ in range(w * h)])
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    if not vault.photo_available():
        print("Pillow not installed — photo index unavailable; skipping.")
        return 0

    imgA = noise_b64(1)          # reused bytes -> identical hash on match
    imgB = noise_b64(2)          # a clearly different "cover"

    # Hash sanity.
    ha = vault._dhash(base64.b64decode(imgA))
    check("dhash is 16 hex chars", len(ha) == 16)
    check("same bytes -> same hash", ha == vault._dhash(base64.b64decode(imgA)))
    check("different images -> distant hashes",
          vault._hamming(ha, vault._dhash(base64.b64decode(imgB))) > vault._MATCH_DISTANCE)

    # Store, dedupe, match.
    r1 = vault.add_photo(imgA, "Ico", variant="black_label")
    check("first store ok", r1.get("ok") and r1.get("stored") is True)
    r2 = vault.add_photo(imgA, "Ico", variant="black_label")
    check("near-duplicate skipped", r2.get("stored") is False)
    check("total is one", r2.get("total") == 1)

    m = vault.match_photo(imgA)
    check("query matches the stored cover", m.get("matched") is not None
          and m["matched"]["title"] == "Ico")
    check("match carries the variant", m["matched"]["variant"] == "black_label")
    miss = vault.match_photo(imgB)
    check("unknown cover does not match", miss.get("matched") is None)

    # A real file landed in the photo dir.
    check("image file was written",
          any(Path(os.environ["SPINE_VAULT_PHOTOS"]).glob("*.jpg")))

    # Keystore routes + phone-side reach.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), keystore.Handler)
    httpd.token = TOKEN
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, payload):
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + TOKEN,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    check("route: match hits over HTTP",
          call("/v1/vault/photo/match", {"image": imgA})["matched"]["title"] == "Ico")
    imgC = noise_b64(3)
    call("/v1/vault/photo", {"image": imgC, "title": "Okami", "variant": "unknown"})
    check("route: stored a new cover",
          call("/v1/vault/photo/match", {"image": imgC})["matched"]["title"] == "Okami")

    # Phone-side _vault_call reaches the vault with real settings.
    app = settings.Settings(TMP / "app.json")
    app.set("keystore_url", base)
    app.set("keystore_token", TOKEN)
    local_server.configure(object(), False, None, None, settings_store=app)
    vm = local_server._vault_call("POST", "/v1/vault/photo/match", {"image": imgA})
    check("phone reaches photo index", vm and vm["matched"]["title"] == "Ico")

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
