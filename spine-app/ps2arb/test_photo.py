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

    # Force dHash for the core checks so they're deterministic whether or not
    # torch/CLIP is installed; the CLIP path gets its own section at the end.
    orig_clip = vault._HAVE_CLIP
    vault._HAVE_CLIP = False

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

    # ---- CLIP path (only if torch/sentence-transformers is installed) ----
    vault._HAVE_CLIP = orig_clip
    if vault.clip_available():
        import numpy as np
        va = vault._embed(base64.b64decode(imgA))
        vb = vault._embed(base64.b64decode(imgB))
        check("clip embedding dim 512", len(va) == 512)
        check("clip embedding deterministic",
              max(abs(a - b) for a, b in zip(va, vault._embed(base64.b64decode(imgA)))) < 1e-4)
        check("clip: identical is most similar",
              float(np.asarray(va) @ np.asarray(va)) > float(np.asarray(va) @ np.asarray(vb)))
        vault.add_photo(noise_b64(4), "God of War")     # stored WITH embedding
        m = vault.match_photo(noise_b64(4))
        check("clip match uses embeddings",
              m.get("method") == "clip" and m["matched"]["title"] == "God of War")
    else:
        print("(sentence-transformers/torch not installed — CLIP checks skipped)")

    # --- CLIP shortlist + the desktop vision tier -------------------------
    # candidates() answers a deliberately softer question than match_photo():
    # "what might this be", with no threshold. It is what lets a local model
    # pick from canonical strings instead of inventing one.
    cands = vault.candidates(noise_b64(1), k=3)
    if vault.clip_available():
        check("candidates returns a ranked shortlist",
              isinstance(cands, list) and len(cands) <= 3)
        check("candidates are distinct titles",
              len({c["title"] for c in cands}) == len(cands))
        check("candidates carry a similarity score",
              all("similarity" in c for c in cands) if cands else True)
        check("candidates are ordered best-first",
              all(cands[i]["similarity"] >= cands[i + 1]["similarity"]
                  for i in range(len(cands) - 1)))
    else:
        # dHash cannot rank near-misses usefully, and a bad shortlist is worse
        # than none — it would steer the model toward a confident wrong pick.
        check("no CLIP -> no shortlist offered", cands == [])

    # The keystore's /identify tier must degrade honestly: with no local model
    # configured it reports no match rather than inventing one, and it must
    # never pass a model's guess off as an index hit.
    settings.Settings(Path(os.environ["SPINE_KEYSTORE_STORE"])).set(
        "local_vision_url", "")
    # A flat grey frame, not more noise: random-noise images sit close together
    # in CLIP space and would match each other, which would test nothing.
    from PIL import Image as _Im
    _b = io.BytesIO()
    _Im.new("RGB", (64, 64), (128, 128, 128)).save(_b, format="JPEG", quality=90)
    blank = base64.b64encode(_b.getvalue()).decode()
    check("the probe genuinely matches nothing in the index",
          vault.match_photo(blank).get("matched") is None)
    res = keystore._local_vision(blank)
    check("no local model configured -> no match, no pretending",
          res.get("matched") is None and res.get("via") is None)
    check("a miss still explains itself", bool(res.get("detail")))

    # A stored cover must still come back as an index hit, labelled as one.
    vault.add_photo(noise_b64(4242), "Okami", "black_label")
    hit = keystore._local_vision(noise_b64(4242))
    check("an indexed cover is labelled photo-index, not local-vision",
          hit.get("matched") and hit.get("via") == "photo-index")

    # --- barcode-labelled capture: source / face / barcode ----------------
    # A photo taken right after a barcode scan is the best-labelled image this
    # app can collect, and the only kind that shows a REAL case. It must be
    # distinguishable from seeded catalogue art, or we cannot tell broad
    # coverage from useful coverage.
    vault.add_photo(noise_b64(7001), "Kuon", "black_label",
                    barcode="012345678905", source="user", face="spine")
    cov = vault.photo_coverage("Kuon")
    check("a user photo is recorded as a user photo",
          cov["have_user_photo"] and cov["user_faces"] == ["spine"])
    vault.add_photo(noise_b64(7002), "Kuon", "black_label",
                    barcode="012345678905", source="user", face="front")
    cov = vault.photo_coverage("Kuon")
    check("faces accumulate per title",
          sorted(cov["user_faces"]) == ["front", "spine"])

    # A front and a spine of the same game are both wanted; dedup must not
    # treat them as the same shot just because the title matches.
    n_before = vault.stats()["photos"]
    vault.add_photo(noise_b64(7003), "Kuon", face="back", source="user")
    check("a different face of the same title is kept",
          vault.stats()["photos"] == n_before + 1)
    # ...but a near-identical repeat of the SAME face is still skipped.
    again = vault.add_photo(noise_b64(7003), "Kuon", face="back", source="user")
    check("a duplicate of the same face is still skipped",
          again.get("stored") is False)

    check("the barcode link is stored",
          any(r for r in [vault.photo_coverage("Kuon")] if r["total"] >= 3))

    # Seeded art must not be counted as evidence about real cases.
    # A title used nowhere else in this suite, so the only row is the seeded
    # one — otherwise an earlier user photo would mask the distinction.
    vault.add_photo(noise_b64(7100), "Rule of Rose", source="seed", face="front")
    seeded = vault.photo_coverage("Rule of Rose")
    check("seeded art is not mistaken for a user photo",
          seeded["seeded"] and not seeded["have_user_photo"])
    whole = vault.photo_coverage()
    check("library-wide coverage separates the two sources",
          whole["user_titles"] >= 1 and whole["seed_titles"] >= 1)

    # Desktop-only config must never be handed to a phone.
    check("local_vision_* is settable from the CLI",
          "local_vision_model" in keystore.SETTABLE)
    check("local_vision_* is NOT served to the phone",
          "local_vision_model" not in keystore.SERVED
          and "local_vision_url" not in keystore.SERVED)

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
