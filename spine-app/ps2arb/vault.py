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

import os
import sqlite3
from pathlib import Path

import upc

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SPINE_VAULT_DB", HERE / "spine_vault.db"))

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
"""

_UPC_COLS = ("upc", "title", "variant", "region", "confirmed_by",
             "first_seen", "times_scanned")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
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


def stats() -> dict:
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM upc_index").fetchone()[0]
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM upc_index WHERE confirmed_by='user' "
            "AND variant NOT IN ('', 'unknown')").fetchone()[0]
        scandex = conn.execute("SELECT COUNT(*) FROM scandex_cache").fetchone()[0]
        cat = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
    finally:
        conn.close()
    return {"upc_total": n, "upc_confirmed": confirmed,
            "scandex_cached": scandex, "catalog_titles": cat,
            "db": str(DB_PATH)}
