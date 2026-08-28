"""
outcomes.py — the realized-flip log: what you actually paid and sold for.

The valuation layer is a set of PRIORS until it is checked against reality.
CONSERVATIVE_QUANTILE, the fee/risk models, days-to-sell -- all of them are
guesses with a decimal point until there is a record of: the model said resell
~$X and bid <=$Y; you paid $P; it later sold for $S in D days. That record is
what a backtest fits the quantile against; nothing else does.

So this captures it. At the moment of a buy it snapshots the PREDICTION (what
the model claimed at that price) alongside what you paid -- the prediction has
to be frozen then, because re-pricing the SKU weeks later would compare the
sale against a number that has since moved. When the item sells you add the
outcome and it computes realized profit and days held.

Stdlib only (sqlite3 + uuid + json), so it lives on the phone beside the
barcode index and syncs to the desktop vault like everything else. Reads never
raise: a corrupt or missing store is an empty log, not a crash on the hot path.

    box = OutcomeLog(path)
    fid = box.record_buy(sku=..., title=..., paid=12.0, prediction={...})
    box.record_sale(fid, sold_price=28.0, fees=4.10, sold_on="2026-09-15")
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# eBay-ish default when a sale is logged without explicit fees, so realized
# profit is never silently overstated. A prior, like everything else here.
_DEFAULT_FEE_RATE = 0.133
_DEFAULT_FEE_FIXED = 0.40

SCHEMA = """
CREATE TABLE IF NOT EXISTS flips (
    id            TEXT PRIMARY KEY,
    sku           TEXT,
    title         TEXT,
    region        TEXT,
    variant       TEXT,
    completeness  TEXT,
    ask           REAL,
    paid          REAL,
    ship_in       REAL,
    pred_expected     REAL,
    pred_conservative REAL,
    pred_max_bid      REAL,
    pred_confidence   TEXT,
    pred_days         REAL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | sold | abandoned
    sold_price    REAL,
    sold_shipping REAL,
    fees          REAL,
    sold_on       TEXT,
    days_held     INTEGER,
    realized_profit REAL,
    bought_on     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_flips_status ON flips(status);
CREATE INDEX IF NOT EXISTS idx_flips_sku    ON flips(sku);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OutcomeLog:
    """A durable log of buys and their eventual sales."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- record a buy ------------------------------------------------------

    def record_buy(self, *, sku: str, title: str, paid: float,
                   ask: float = 0.0, ship_in: float = 0.0,
                   region: str = "", variant: str = "", completeness: str = "",
                   prediction: dict | None = None, note: str = "") -> str:
        """Log a purchase, freezing the model's prediction at buy time."""
        p = prediction or {}
        fid = uuid.uuid4().hex
        now = _now()
        self.db.execute(
            "INSERT INTO flips (id,sku,title,region,variant,completeness,"
            "ask,paid,ship_in,pred_expected,pred_conservative,pred_max_bid,"
            "pred_confidence,pred_days,status,bought_on,updated_at,note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?)",
            (fid, sku, title, region, variant, completeness,
             _num(ask), _num(paid), _num(ship_in),
             _num(p.get("expected_resale")), _num(p.get("conservative_resale")),
             _num(p.get("max_bid")), p.get("confidence"), _num(p.get("days_to_sell")),
             now, now, note))
        self.db.commit()
        return fid

    # -- record the sale ---------------------------------------------------

    def record_sale(self, flip_id: str, *, sold_price: float,
                    sold_shipping: float = 0.0, fees: float | None = None,
                    sold_on: str | None = None, note: str | None = None) -> bool:
        """Close a flip with its sale. Computes fees (if omitted), realized
        profit, and days held. Returns False if the id is unknown."""
        row = self.db.execute("SELECT * FROM flips WHERE id=?",
                              (flip_id,)).fetchone()
        if row is None:
            return False
        sold_price = _num(sold_price) or 0.0
        sold_shipping = _num(sold_shipping) or 0.0
        if fees is None:
            fees = round(_DEFAULT_FEE_RATE * (sold_price + sold_shipping)
                         + _DEFAULT_FEE_FIXED, 2)
        cost = (row["paid"] or 0.0) + (row["ship_in"] or 0.0)
        realized = round(sold_price + sold_shipping - fees - cost, 2)
        sold_on = sold_on or date.today().isoformat()
        days = _days_between(row["bought_on"], sold_on)
        self.db.execute(
            "UPDATE flips SET status='sold', sold_price=?, sold_shipping=?, "
            "fees=?, sold_on=?, days_held=?, realized_profit=?, updated_at=?, "
            "note=COALESCE(?, note) WHERE id=?",
            (sold_price, sold_shipping, fees, sold_on, days, realized,
             _now(), note, flip_id))
        self.db.commit()
        return True

    def mark_abandoned(self, flip_id: str) -> bool:
        cur = self.db.execute(
            "UPDATE flips SET status='abandoned', updated_at=? WHERE id=?",
            (_now(), flip_id))
        self.db.commit()
        return cur.rowcount > 0

    def remove(self, flip_id: str) -> None:
        self.db.execute("DELETE FROM flips WHERE id=?", (flip_id,))
        self.db.commit()

    # -- read --------------------------------------------------------------

    def get(self, flip_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM flips WHERE id=?",
                              (flip_id,)).fetchone()
        return dict(row) if row else None

    def open_flips(self) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM flips WHERE status='open' ORDER BY bought_on DESC")]

    def all_flips(self) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM flips ORDER BY bought_on DESC")]

    def stats(self) -> dict:
        def one(sql):
            r = self.db.execute(sql).fetchone()
            return r[0] if r and r[0] is not None else 0
        sold = one("SELECT COUNT(*) FROM flips WHERE status='sold'")
        return {
            "total": one("SELECT COUNT(*) FROM flips"),
            "open": one("SELECT COUNT(*) FROM flips WHERE status='open'"),
            "sold": sold,
            "abandoned": one("SELECT COUNT(*) FROM flips WHERE status='abandoned'"),
            "realized_profit": round(
                one("SELECT COALESCE(SUM(realized_profit),0) FROM flips "
                    "WHERE status='sold'"), 2),
            # How the frozen prediction compared to what the sale realised --
            # the raw material for recalibrating the conservative quantile.
            "avg_predicted_conservative": _avg(
                self.db, "pred_conservative", "status='sold'"),
            "avg_realized_sale": _avg(
                self.db, "sold_price", "status='sold'"),
        }

    # -- sync (phone <-> desktop vault), merged by id, newest wins ---------

    def export_rows(self) -> list:
        return self.all_flips()

    def import_rows(self, rows) -> int:
        changed = 0
        for row in rows or []:
            fid = row.get("id")
            updated = row.get("updated_at") or _now()
            if not fid:
                continue
            existing = self.db.execute(
                "SELECT updated_at FROM flips WHERE id=?", (fid,)).fetchone()
            if existing is not None and existing["updated_at"] >= updated:
                continue
            cols = ("id", "sku", "title", "region", "variant", "completeness",
                    "ask", "paid", "ship_in", "pred_expected",
                    "pred_conservative", "pred_max_bid", "pred_confidence",
                    "pred_days", "status", "sold_price", "sold_shipping", "fees",
                    "sold_on", "days_held", "realized_profit", "bought_on",
                    "updated_at", "note")
            vals = [row.get(c) for c in cols]
            if not row.get("bought_on"):
                vals[cols.index("bought_on")] = updated
            placeholders = ",".join("?" * len(cols))
            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
            self.db.execute(
                f"INSERT INTO flips ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", vals)
            changed += 1
        self.db.commit()
        return changed


def _num(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _avg(db, col, where):
    r = db.execute(f"SELECT AVG({col}) FROM flips WHERE {where} "
                   f"AND {col} IS NOT NULL").fetchone()
    return round(r[0], 2) if r and r[0] is not None else None


def _days_between(start: str, end: str) -> int | None:
    try:
        d0 = datetime.fromisoformat(start).date()
        d1 = date.fromisoformat(end[:10])
        return max((d1 - d0).days, 0)
    except (ValueError, TypeError):
        return None
