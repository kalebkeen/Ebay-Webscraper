"""
pricecache.py — precomputed resale estimates, so the phone never waits on
a live comp API.

The problem this solves: the real sources (SoldComps, PriceCharting) are slow
per call and quota-limited -- SoldComps' free tier is 100 requests/MONTH.
Calling them live on every scan would be laggy in the field and would burn
the month's quota in a day.

So the expensive half is precomputed on the DESKTOP. `precompute.py` walks
the catalog, runs each SKU through comps.value_sku against the real layered
source, and stores the finished resale estimate here. The desktop keystore
serves this cache; the phone pulls a copy on sync and reads it FIRST -- an
instant, offline-capable answer -- falling back to a live source only for a
SKU the cache has never seen.

What is cached is the resale ESTIMATE (expected / conservative / p25 / p75 /
confidence / velocity), keyed exactly like comps.Valuation.sku:

    title|region|variant|completeness

NOT the final max_bid. Economics (fees, shipping, local pickup, the ask)
depend on per-request inputs and stay live in core.value; only the networked
comp lookup is precomputed. The stored dict is the exact shape core.value
builds from a Valuation, so a cache hit drops straight in.

Stdlib only (sqlite3 + json) -- no Pillow/torch, so unlike the photo vault
this file is safe to bundle into the APK and read on-device.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS valuations (
    sku         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source_name TEXT,
    computed_at TEXT NOT NULL,     -- UTC ISO8601
    payload     TEXT NOT NULL      -- JSON: the core.value-shaped estimate dict
);
CREATE INDEX IF NOT EXISTS idx_pc_title ON valuations(title);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PriceCache:
    """A keyed store of precomputed resale estimates. Never raises on a read;
    a miss or a corrupt row is simply "no cached estimate" and the caller
    falls back to computing live."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- read (the phone's hot path) --------------------------------------

    def get(self, sku: str, max_age_days: float | None = None) -> dict | None:
        """The cached estimate dict for this sku, or None.

        `max_age_days` lets a caller refuse a stale estimate (e.g. price the
        SKU live instead). None means any age is acceptable -- a month-old
        estimate still beats no estimate for a slow-moving retro title.
        """
        try:
            row = self.db.execute(
                "SELECT payload, computed_at FROM valuations WHERE sku=?",
                (sku,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        if max_age_days is not None and _age_days(row["computed_at"]) > max_age_days:
            return None
        try:
            data = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        data["cached_at"] = row["computed_at"]
        return data

    def stats(self) -> dict:
        def one(sql):
            r = self.db.execute(sql).fetchone()
            return r[0] if r else 0
        return {
            "skus": one("SELECT COUNT(*) FROM valuations"),
            # payload is compact JSON (no space after the colon).
            "quotable": one("SELECT COUNT(*) FROM valuations "
                            "WHERE payload LIKE '%\"quotable\":true%'"),
            "titles": one("SELECT COUNT(DISTINCT title) FROM valuations"),
            "newest": one("SELECT MAX(computed_at) FROM valuations") or None,
            "oldest": one("SELECT MIN(computed_at) FROM valuations") or None,
        }

    # -- write (the desktop harvester) ------------------------------------

    def put(self, sku: str, title: str, estimate: dict, *,
            source_name: str = "", computed_at: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO valuations (sku,title,source_name,computed_at,payload) "
            "VALUES (?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET "
            "title=excluded.title, source_name=excluded.source_name, "
            "computed_at=excluded.computed_at, payload=excluded.payload",
            (sku, title, source_name, computed_at or _now(),
             json.dumps(estimate, separators=(",", ":"))))
        self.db.commit()

    def put_valuation(self, val, *, source_name: str = "") -> None:
        """Store a comps.Valuation as the core.value-shaped estimate dict.

        Mirrors exactly what core.value builds so a cache hit is drop-in.
        """
        if getattr(val, "quotable", False):
            estimate = {
                "quotable": True, "sku": val.sku,
                "expected_resale": val.expected_resale,
                "conservative_resale": val.conservative_resale,
                "p25": val.p25, "p75": val.p75,
                "confidence": val.confidence.value,
                "n_effective": val.n_effective,
                "days_to_sell": val.est_days_to_sell,
                "needs_verify": getattr(val, "needs_verify", False),
                "adjustments": list(val.adjustments[:4]),
                "warnings": list(val.warnings[:3]),
            }
        else:
            estimate = {"quotable": False,
                        "warnings": list(val.warnings)
                        or ["not enough comparable sales"]}
        self.put(val.sku, _title_of(val.sku), estimate, source_name=source_name)

    # -- sync (desktop <-> phone) -----------------------------------------

    def export_rows(self) -> list:
        """Every row as a plain dict, for the keystore to serve."""
        out = []
        for r in self.db.execute(
                "SELECT sku,title,source_name,computed_at,payload FROM valuations"):
            out.append({"sku": r["sku"], "title": r["title"],
                        "source_name": r["source_name"],
                        "computed_at": r["computed_at"], "payload": r["payload"]})
        return out

    def import_rows(self, rows) -> int:
        """Merge synced rows, keeping the NEWER estimate per sku. Returns the
        number of rows that were new or refreshed."""
        changed = 0
        for row in rows or []:
            sku = row.get("sku")
            payload = row.get("payload")
            computed_at = row.get("computed_at") or _now()
            if not sku or not payload:
                continue
            existing = self.db.execute(
                "SELECT computed_at FROM valuations WHERE sku=?", (sku,)).fetchone()
            if existing is not None and existing["computed_at"] >= computed_at:
                continue                       # ours is same-age or newer
            # payload may arrive as a JSON string (from the wire) or a dict.
            if not isinstance(payload, str):
                payload = json.dumps(payload, separators=(",", ":"))
            self.db.execute(
                "INSERT INTO valuations (sku,title,source_name,computed_at,payload) "
                "VALUES (?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET "
                "title=excluded.title, source_name=excluded.source_name, "
                "computed_at=excluded.computed_at, payload=excluded.payload",
                (sku, row.get("title", _title_of(sku)),
                 row.get("source_name", ""), computed_at, payload))
            changed += 1
        self.db.commit()
        return changed


def _title_of(sku: str) -> str:
    return sku.split("|", 1)[0] if sku else ""


def _age_days(stamp: str) -> float:
    try:
        then = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
