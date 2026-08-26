"""
vault.py — the desktop data vault. DESKTOP-ONLY (never bundled into the APK).

Phase 2, first slice: own your learned barcode index. Every spine you confirm
on the phone is durable knowledge; today it lives only in that phone's
upc_index.json and a factory reset would lose it. The vault is a central
SQLite on this desktop that the phone backs its index up to (and restores
from) over the same Tailscale connection and bearer token the keystore uses.

Deliberately reuses `upc.merge_two`, so the server and the phone apply the
identical merge rule: a user-confirmed variant never gets downgraded, scans
are max'd not summed, and the earliest first_seen wins. That makes push/pull
order-independent and safe to run automatically.

Still stdlib only. Later slices (not here yet): the ScanDex cache, a catalog
snapshot, and — once eBay keys exist — the store.py sold-price harvest, which
is the real payoff and the path off synthetic pricing.

    SPINE_VAULT_DB   path to the sqlite file (default spine_vault.db here)
"""
from __future__ import annotations

import base64
import io
import os
import sqlite3
from pathlib import Path

import upc

# Pillow is desktop-only and used just for the photo index (decode + hash).
# Guarded so the keystore keeps serving keys and the rest of the vault even if
# it is not installed.
try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:                                    # pragma: no cover
    _HAVE_PIL = False

# CLIP embeddings (desktop-only, optional). When present, the photo index
# matches by angle-robust image embedding instead of the perceptual hash.
# Availability is detected WITHOUT importing torch — that stays lazy so the
# keystore's key-serving never depends on (or waits for) torch loading, and a
# torch/CUDA hiccup can never take down key delivery.
import importlib.util as _ilu

_HAVE_CLIP = (_ilu.find_spec("sentence_transformers") is not None
             and _ilu.find_spec("numpy") is not None)

_CLIP_MODEL = None                                     # lazy-loaded once

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SPINE_VAULT_DB", HERE / "spine_vault.db"))
PHOTO_DIR = Path(os.environ.get("SPINE_VAULT_PHOTOS", HERE / "vault_photos"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS upc_index (
    upc           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    variant       TEXT,
    region        TEXT,
    confirmed_by  TEXT,
    first_seen    TEXT,
    times_scanned INTEGER DEFAULT 0,
    updated_at    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS scandex_cache (
    barcode    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,     -- the raw ScanDex response, JSON
    status     TEXT,              -- matched | unmatched | absent | ...
    fetched    REAL DEFAULT 0,    -- epoch seconds of the lookup
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS catalog (
    canonical  TEXT PRIMARY KEY,
    regions    TEXT,              -- JSON array
    aliases    TEXT,              -- JSON array
    liquidity  TEXT
);
CREATE TABLE IF NOT EXISTS photo_index (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phash      TEXT NOT NULL,     -- 64-bit dHash, hex
    title      TEXT NOT NULL,     -- confirmed canonical title
    variant    TEXT,
    barcode    TEXT,              -- linked barcode if known, else ''
    file       TEXT,              -- relative filename under PHOTO_DIR
    embedding  TEXT,              -- CLIP embedding as JSON floats, if available
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_UPC_COLS = ("upc", "title", "variant", "region", "confirmed_by",
             "first_seen", "times_scanned")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Migrate a pre-embedding photo_index (added 2026-08-26).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photo_index)")}
    if "embedding" not in cols:
        conn.execute("ALTER TABLE photo_index ADD COLUMN embedding TEXT")
        conn.commit()
    return conn


def merge_upc(entries: list[dict]) -> dict:
    """Merge pushed barcode records into the vault. Returns a small summary."""
    conn = _conn()
    stored = 0
    try:
        for e in entries or []:
            code = upc.normalise(str(e.get("upc", "")))
            if not code or not e.get("title"):
                continue
            row = conn.execute(
                "SELECT upc, title, variant, region, confirmed_by, first_seen, "
                "times_scanned FROM upc_index WHERE upc=?", (code,)).fetchone()
            incoming = {**e, "upc": code}
            merged = upc.merge_two(dict(row), incoming) if row else incoming
            conn.execute(
                "INSERT INTO upc_index "
                "(upc,title,variant,region,confirmed_by,first_seen,times_scanned,"
                " updated_at) VALUES (?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(upc) DO UPDATE SET "
                "title=excluded.title, variant=excluded.variant, "
                "region=excluded.region, confirmed_by=excluded.confirmed_by, "
                "first_seen=excluded.first_seen, "
                "times_scanned=excluded.times_scanned, "
                "updated_at=datetime('now')",
                (code, merged.get("title", ""),
                 merged.get("variant"), merged.get("region"),
                 merged.get("confirmed_by"), merged.get("first_seen", ""),
                 int(merged.get("times_scanned") or 0)))
            stored += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM upc_index").fetchone()[0]
    finally:
        conn.close()
    return {"received": len(entries or []), "stored": stored, "total": total}


def all_upc() -> list[dict]:
    """Every barcode record in the vault, as plain dicts (for the phone to pull)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT upc, title, variant, region, confirmed_by, first_seen, "
            "times_scanned FROM upc_index").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _scandex_better(a: dict, b: dict) -> bool:
    """Is record a a better cache entry than b? A 'matched' beats a miss;
    otherwise the more recently fetched wins. Keeps useful hits when ScanDex
    later forgets a barcode or a device holds a stale miss."""
    sa = 1 if a.get("status") == "matched" else 0
    sb = 1 if b.get("status") == "matched" else 0
    if sa != sb:
        return sa > sb
    return float(a.get("fetched") or 0) > float(b.get("fetched") or 0)


def merge_scandex(entries: list[dict]) -> dict:
    """Merge pushed ScanDex cache rows in. Each: {barcode, payload, status?, fetched?}."""
    import json as _json
    conn = _conn()
    stored = 0
    try:
        for e in entries or []:
            code = str(e.get("barcode", "")).strip()
            payload = e.get("payload")
            if not code or payload is None:
                continue
            status = e.get("status") or (payload.get("status")
                                         if isinstance(payload, dict) else "")
            fetched = e.get("fetched") or (payload.get("fetched", 0)
                                           if isinstance(payload, dict) else 0)
            row = conn.execute("SELECT status, fetched FROM scandex_cache "
                               "WHERE barcode=?", (code,)).fetchone()
            incoming = {"status": status, "fetched": fetched}
            if row is not None and not _scandex_better(incoming, dict(row)):
                continue
            conn.execute(
                "INSERT INTO scandex_cache (barcode,payload,status,fetched,updated_at) "
                "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(barcode) DO UPDATE SET "
                "payload=excluded.payload, status=excluded.status, "
                "fetched=excluded.fetched, updated_at=datetime('now')",
                (code, _json.dumps(payload), status, float(fetched or 0)))
            stored += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM scandex_cache").fetchone()[0]
    finally:
        conn.close()
    return {"received": len(entries or []), "stored": stored, "total": total}


def all_scandex() -> list[dict]:
    import json as _json
    conn = _conn()
    try:
        rows = conn.execute("SELECT barcode, payload, status, fetched "
                            "FROM scandex_cache").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            payload = _json.loads(r["payload"])
        except (ValueError, TypeError):
            payload = {}
        out.append({"barcode": r["barcode"], "payload": payload,
                    "status": r["status"], "fetched": r["fetched"]})
    return out


def replace_catalog(entries: list[dict]) -> dict:
    """Snapshot the catalog into the vault (full replace — the desktop is the
    single source of truth for the title list). Each: {canonical, regions,
    aliases, liquidity}."""
    import json as _json
    conn = _conn()
    try:
        conn.execute("DELETE FROM catalog")
        n = 0
        for e in entries or []:
            canonical = e.get("canonical")
            if not canonical:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO catalog (canonical,regions,aliases,liquidity) "
                "VALUES (?,?,?,?)",
                (canonical, _json.dumps(e.get("regions") or []),
                 _json.dumps(e.get("aliases") or []), e.get("liquidity") or ""))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return {"stored": n}


def all_catalog() -> list[dict]:
    import json as _json
    conn = _conn()
    try:
        rows = conn.execute("SELECT canonical, regions, aliases, liquidity "
                            "FROM catalog").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            regions = _json.loads(r["regions"] or "[]")
            aliases = _json.loads(r["aliases"] or "[]")
        except (ValueError, TypeError):
            regions, aliases = [], []
        out.append({"canonical": r["canonical"], "regions": regions,
                    "aliases": aliases, "liquidity": r["liquidity"]})
    return out


# ---------------------------------------------------------------------------
# Photo index — a learned image index for covers/spines, the visual analog of
# the barcode learned index. A confirmed photo is stored with a perceptual
# hash; a new photo that hashes close to a stored one resolves for free, no
# vision-API call. dHash is deliberately light (Pillow only); a CLIP-embedding
# backend can replace _signature later for cross-angle robustness.
# ---------------------------------------------------------------------------

_HASH_SIZE = 8            # 8x9 grayscale -> 64-bit dHash
_DEDUP_DISTANCE = 4       # near-identical shot of a title we already have
_MATCH_DISTANCE = 10      # default dHash "same cover" threshold (of 64 bits)
_MATCH_COSINE = 0.85      # default CLIP "same cover" cosine threshold


def photo_available() -> bool:
    return _HAVE_PIL


def clip_available() -> bool:
    return _HAVE_CLIP


def _clip():
    global _CLIP_MODEL
    if _CLIP_MODEL is None:
        from sentence_transformers import SentenceTransformer  # lazy: pulls torch
        _CLIP_MODEL = SentenceTransformer(
            os.environ.get("SPINE_CLIP_MODEL", "clip-ViT-B-32"))
    return _CLIP_MODEL


def _embed(image_bytes: bytes) -> list[float]:
    """A unit-normalized CLIP image embedding (cosine == dot product)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    vec = _clip().encode(img, normalize_embeddings=True)
    return [float(x) for x in vec]


def _dhash(image_bytes: bytes) -> str:
    """64-bit difference hash as 16 hex chars."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize(
        (_HASH_SIZE + 1, _HASH_SIZE))
    px = list(img.getdata())
    w = _HASH_SIZE + 1
    bits = 0
    for row in range(_HASH_SIZE):
        for col in range(_HASH_SIZE):
            i = row * w + col
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return f"{bits:016x}"


def _hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def add_photo(image_b64: str, title: str, variant: str = "unknown",
              barcode: str = "") -> dict:
    """Store a confirmed photo + label. Skips a near-duplicate of a title we
    already hold, so re-scanning the same game doesn't bloat the set."""
    import json as _json
    if not _HAVE_PIL:
        return {"ok": False, "detail": "photo index unavailable (no Pillow)"}
    try:
        raw = base64.b64decode(image_b64)
        phash = _dhash(raw)
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "detail": f"bad image: {exc}"}
    emb = None
    if _HAVE_CLIP:
        try:
            emb = _json.dumps(_embed(raw))
        except Exception:                               # noqa: BLE001
            emb = None                                  # degrade to dHash-only

    conn = _conn()
    try:
        for row in conn.execute(
                "SELECT phash FROM photo_index WHERE title=?", (title,)):
            if _hamming(phash, row["phash"]) <= _DEDUP_DISTANCE:
                total = conn.execute(
                    "SELECT COUNT(*) FROM photo_index").fetchone()[0]
                return {"ok": True, "stored": False, "reason": "duplicate",
                        "total": total}
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        cur = conn.execute(
            "INSERT INTO photo_index (phash,title,variant,barcode,file,embedding) "
            "VALUES (?,?,?,?,?,?)", (phash, title, variant, barcode, "", emb))
        rid = cur.lastrowid
        fname = f"{rid:06d}_{phash[:8]}.jpg"
        (PHOTO_DIR / fname).write_bytes(raw)
        conn.execute("UPDATE photo_index SET file=? WHERE id=?", (fname, rid))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM photo_index").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "stored": True, "id": rid, "total": total}


def match_photo(image_b64: str, max_distance: int = _MATCH_DISTANCE,
                min_cosine: float = _MATCH_COSINE) -> dict:
    """Nearest stored photo. Prefers CLIP cosine (angle-robust) when available,
    falls back to the perceptual hash. Returns the label if within threshold."""
    if not _HAVE_PIL:
        return {"matched": None, "detail": "photo index unavailable (no Pillow)"}
    try:
        raw = base64.b64decode(image_b64)
    except Exception as exc:                            # noqa: BLE001
        return {"matched": None, "detail": f"bad image: {exc}"}

    conn = _conn()
    try:
        rows = conn.execute("SELECT phash, title, variant, barcode, embedding "
                            "FROM photo_index").fetchall()
    finally:
        conn.close()
    if not rows:
        return {"matched": None, "best_distance": None}

    def _hit(r, extra):
        return {"matched": {"title": r["title"], "variant": r["variant"],
                            "barcode": r["barcode"], **extra}}

    # CLIP first, over rows that carry an embedding.
    if _HAVE_CLIP:
        try:
            import json as _json
            import numpy as _np                          # lazy, like torch
            q = _np.array(_embed(raw), dtype=_np.float32)
            best, best_sim = None, -1.0
            for r in rows:
                if not r["embedding"]:
                    continue
                e = _np.asarray(_json.loads(r["embedding"]), dtype=_np.float32)
                sim = float(q @ e)
                if sim > best_sim:
                    best, best_sim = r, sim
            if best is not None and best_sim >= min_cosine:
                return {**_hit(best, {"similarity": round(best_sim, 3)}),
                        "method": "clip"}
        except Exception:                               # noqa: BLE001
            pass                                        # fall through to dHash

    # dHash fallback (also covers any rows without an embedding).
    try:
        phash = _dhash(raw)
    except Exception as exc:                            # noqa: BLE001
        return {"matched": None, "detail": f"bad image: {exc}"}
    best, best_d = None, 65
    for r in rows:
        d = _hamming(phash, r["phash"])
        if d < best_d:
            best, best_d = r, d
    if best is not None and best_d <= max_distance:
        return {**_hit(best, {"distance": best_d}), "method": "dhash"}
    return {"matched": None, "best_distance": (best_d if best else None)}


def stats() -> dict:
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM upc_index").fetchone()[0]
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM upc_index WHERE confirmed_by='user' "
            "AND variant NOT IN ('', 'unknown')").fetchone()[0]
        scandex = conn.execute("SELECT COUNT(*) FROM scandex_cache").fetchone()[0]
        cat = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
        photos = conn.execute("SELECT COUNT(*) FROM photo_index").fetchone()[0]
    finally:
        conn.close()
    return {"upc_total": n, "upc_confirmed": confirmed,
            "scandex_cached": scandex, "catalog_titles": cat,
            "photos": photos, "photo_index": _HAVE_PIL,
            "photo_match": "clip" if _HAVE_CLIP else "dhash",
            "db": str(DB_PATH)}
