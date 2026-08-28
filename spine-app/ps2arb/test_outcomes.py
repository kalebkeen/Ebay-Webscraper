"""
test_outcomes.py — the realized-flip log.

Verifies the calibration contract: a buy freezes the prediction, a sale
computes realized profit / days / fees, stats expose predicted-vs-realized,
and the log survives a fresh process and merges by id (newest wins).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import outcomes

CHECKS = []
def check(name, cond):
    CHECKS.append((name, bool(cond)))


PRED = {"expected_resale": 30.0, "conservative_resale": 26.0,
        "max_bid": 12.5, "confidence": "medium", "days_to_sell": 40}


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        box = outcomes.OutcomeLog(Path(d) / "outcomes.db")

        check("starts empty", box.stats()["total"] == 0)

        fid = box.record_buy(sku="Ico|ntsc_u|unknown|loose", title="Ico",
                             paid=11.0, ask=15.0, ship_in=0.0,
                             region="ntsc_u", variant="unknown",
                             completeness="loose", prediction=PRED)
        check("buy returns an id", bool(fid))
        row = box.get(fid)
        check("buy is open", row["status"] == "open")
        check("prediction frozen at buy time",
              row["pred_conservative"] == 26.0 and row["pred_max_bid"] == 12.5
              and row["pred_confidence"] == "medium")
        check("paid recorded", row["paid"] == 11.0)
        check("open_flips lists it", len(box.open_flips()) == 1)

        # Sale with explicit fees: realized = sold+ship - fees - (paid+ship_in).
        ok = box.record_sale(fid, sold_price=28.0, sold_shipping=0.0,
                             fees=4.10, sold_on="2026-09-15")
        check("sale on a known id succeeds", ok is True)
        row = box.get(fid)
        check("status becomes sold", row["status"] == "sold")
        check("realized profit computed",
              abs(row["realized_profit"] - (28.0 - 4.10 - 11.0)) < 1e-6)
        check("days held computed from the dates",
              row["days_held"] is not None and row["days_held"] > 0)
        check("no longer open", box.open_flips() == [])

        # Sale without fees -> a fee is estimated, never treated as zero.
        f2 = box.record_buy(sku="Okami|ntsc_u|unknown|loose", title="Okami",
                            paid=20.0, prediction=PRED)
        box.record_sale(f2, sold_price=40.0)
        r2 = box.get(f2)
        check("missing fees are estimated, not zero",
              r2["fees"] is not None and r2["fees"] > 0
              and r2["realized_profit"] < (40.0 - 20.0))

        check("unknown id sale returns False",
              box.record_sale("nope", sold_price=1.0) is False)

        f3 = box.record_buy(sku="X|ntsc_u|unknown|loose", title="X", paid=5.0)
        check("abandon marks the flip", box.mark_abandoned(f3) is True
              and box.get(f3)["status"] == "abandoned")

        st = box.stats()
        check("stats count sold", st["sold"] == 2)
        check("stats sum realized profit",
              abs(st["realized_profit"]
                  - (r2["realized_profit"] + (28.0 - 4.10 - 11.0))) < 1e-6)
        check("stats expose predicted vs realized",
              st["avg_predicted_conservative"] == 26.0
              and st["avg_realized_sale"] == 34.0)

        # Durability + sync merge.
        box2 = outcomes.OutcomeLog(Path(d) / "outcomes.db")
        check("survives a fresh instance", box2.stats()["total"] == 3)
        box2.close()

        other = outcomes.OutcomeLog(Path(d) / "other.db")
        moved = other.import_rows(box.export_rows())
        check("export/import carries rows", moved == 3)
        # An older incoming row must not clobber a newer local one.
        stale = box.get(fid); stale["realized_profit"] = -999.0
        stale["updated_at"] = "2000-01-01T00:00:00+00:00"
        other.import_rows([stale])
        check("stale incoming row is ignored",
              other.get(fid)["realized_profit"] != -999.0)
        other.close()
        box.close()

    failures = [n for n, ok in CHECKS if not ok]
    print("-" * 72)
    for n, ok in CHECKS:
        print(f"  {'ok ' if ok else 'FAIL'}  {n}")
    print("-" * 72)
    print(f"{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
