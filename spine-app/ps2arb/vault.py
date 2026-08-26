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


def stats() -> dict:
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM upc_index").fetchone()[0]
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM upc_index WHERE confirmed_by='user' "
            "AND variant NOT IN ('', 'unknown')").fetchone()[0]
    finally:
        conn.close()
    return {"upc_total": n, "upc_confirmed": confirmed, "db": str(DB_PATH)}
