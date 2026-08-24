"""
service.py — HTTP surface over the pipeline.

The split, and why it is this way:

  SERVER (this file)  holds the API credentials, runs the batch scan, caches
                      comps, and does the arithmetic. Keys cannot live in a
                      mobile bundle -- they are extractable from any APK, and
                      eBay bans the key rather than the app.

  CLIENT (static/)    answers one question in a shop aisle: what is the most
                      I should pay for the disc in my hand. It never sees a
                      credential and holds no pricing logic of its own.

Endpoints deliberately return the REASONS alongside the number. A bare max
bid with no explanation is untrustworthy in the field, where the whole
decision is whether to override it.

Run:  uvicorn service:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import catalog
import comps
import decide
import economics as ec
import mock_sources as ms
import pipeline
import upc as upc_mod
from listing_parser import Completeness as C, Region as R, Variant as V

STATIC = Path(__file__).parent / "static"
TODAY = date(2026, 8, 22)

app = FastAPI(title="PS2 field valuation", version="0.1")
# The client is served from the same origin in production, but a phone on
# the same LAN hitting a laptop dev server is a different origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

UPC = upc_mod.UpcIndex()

# ---------------------------------------------------------------------------
# Comp source
# ---------------------------------------------------------------------------
# SWAP THIS for a real adapter. It needs three methods -- sold_records,
# active_listing_count, quote -- and everything downstream is source-agnostic.
# Until then every number this service returns is synthetic.
SOURCE = ms.CombinedSource(ms.MockMarketplace(seed=7, today=TODAY),
                           ms.MockReference(TODAY))
SOURCE_IS_REAL = False


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class _Cached:
    value: dict
    at: float


_CACHE: dict[str, _Cached] = {}
CACHE_TTL = 60 * 60 * 6      # comps move slowly; rate limits do not


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit.at < CACHE_TTL:
        return hit.value
    return None


def _store(key: str, value: dict) -> dict:
    _CACHE[key] = _Cached(value, time.time())
    return value


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ValueRequest(BaseModel):
    title: str
    variant: str = V.UNKNOWN.value
    completeness: str = C.LOOSE.value
    region: str = R.NTSC_U.value
    ask: float | None = Field(default=None, description="Seller's price, if known")
    ship_in: float = 0.0
    local_pickup: bool = True


class TeachRequest(BaseModel):
    title: str
    variant: str = V.UNKNOWN.value
    region: str = R.NTSC_U.value


class AssessRequest(BaseModel):
    raw_title: str
    description: str = ""
    ask: float
    ship_in: float = 0.0


def _enum(cls, value, fallback):
    try:
        return cls(value)
    except ValueError:
        return fallback


def _entry(title: str):
    return next((t for t in catalog.CATALOG if t.canonical == title), None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "catalog_titles": len(catalog.CATALOG),
        "comp_source": "MOCK — every price below is synthetic"
                       if not SOURCE_IS_REAL else "live",
        "upc": UPC.stats(),
    }


@app.get("/api/titles")
def titles(q: str = "", limit: int = 12):
    """Type-ahead over the catalog. Empty query returns everything."""
    if not q.strip():
        rows = catalog.CATALOG[:limit]
        return {"results": [{"title": t.canonical, "score": 100,
                             "liquidity": t.liquidity,
                             "has_greatest_hits": t.has_greatest_hits}
                            for t in rows]}
    match = catalog.match(q)
    rivals = catalog.ambiguity_check(q)
    out = []
    if match.title:
        out.append({"title": match.title.canonical, "score": round(match.score),
                    "liquidity": match.title.liquidity,
                    "has_greatest_hits": match.title.has_greatest_hits})
    for name, sc in rivals:
        if not any(o["title"] == name for o in out):
            e = _entry(name)
            out.append({"title": name, "score": round(sc),
                        "liquidity": e.liquidity if e else "unknown",
                        "has_greatest_hits": bool(e and e.has_greatest_hits)})
    # Fall back to a plain substring sweep so a partial title still offers
    # something to tap rather than an empty list.
    if len(out) < 3:
        needle = q.lower()
        for t in catalog.CATALOG:
            if needle in t.canonical.lower() and not any(
                    o["title"] == t.canonical for o in out):
                out.append({"title": t.canonical, "score": 0,
                            "liquidity": t.liquidity,
                            "has_greatest_hits": t.has_greatest_hits})
            if len(out) >= limit:
                break
    return {"results": out[:limit], "ambiguous": bool(rivals)}


@app.get("/api/upc/{code}")
def upc_lookup(code: str):
    clean = upc_mod.normalise(code)
    entry = UPC.lookup(clean)
    if entry is None:
        return {"known": False, "upc": clean,
                "checksum_ok": upc_mod.check_digit_ok(clean),
                "hint": "Unknown code. Pick the title once and it is "
                        "remembered for next time."}
    return {"known": True, "upc": clean, "title": entry.title,
            "variant": entry.variant, "region": entry.region,
            "times_scanned": entry.times_scanned}


@app.post("/api/upc/{code}")
def upc_teach(code: str, body: TeachRequest):
    if not _entry(body.title):
        raise HTTPException(404, f"'{body.title}' is not in the catalog")
    e = UPC.teach(code, body.title, body.variant, body.region)
    return {"saved": True, "upc": e.upc, "title": e.title,
            "variant": e.variant}


@app.post("/api/value")
def value(req: ValueRequest):
    """
    The field endpoint. Returns a max bid plus the reasoning behind it.

    `local_pickup` is not a cosmetic toggle. Removing both shipping legs
    drops the structural floor from about $7.72 to near $1, which is the
    single biggest lever in the whole model and the reason in-person
    sourcing works where mail-order arbitrage does not.
    """
    entry = _entry(req.title)
    if entry is None:
        raise HTTPException(404, f"'{req.title}' is not in the catalog")

    variant = _enum(V, req.variant, V.UNKNOWN)
    completeness = _enum(C, req.completeness, C.LOOSE)
    region = _enum(R, req.region, R.NTSC_U)

    key = (f"{req.title}|{region.value}|{variant.value}|{completeness.value}"
           f"|{req.local_pickup}")
    cached = _cached(key)

    if cached is None:
        val = comps.value_sku(
            title=req.title, region=region, variant=variant,
            completeness=completeness, source=SOURCE,
            has_budget_reprint=entry.has_greatest_hits, today=TODAY)
        if not val.quotable:
            cached = {"quotable": False,
                      "warnings": val.warnings or ["not enough comparable sales"]}
        else:
            cached = {
                "quotable": True,
                "sku": val.sku,
                "expected_resale": val.expected_resale,
                "conservative_resale": val.conservative_resale,
                "p25": val.p25, "p75": val.p75,
                "confidence": val.confidence.value,
                "n_effective": val.n_effective,
                "days_to_sell": val.est_days_to_sell,
                "adjustments": val.adjustments[:4],
                "warnings": val.warnings[:3],
            }
        _store(key, cached)

    out = dict(cached)
    out["title"] = req.title
    out["liquidity"] = entry.liquidity
    out["repro_risk"] = entry.repro_risk
    out["has_greatest_hits"] = entry.has_greatest_hits
    out["priced_as_variant"] = variant.value
    out["source_is_mock"] = not SOURCE_IS_REAL

    if not out.get("quotable"):
        return out

    ops = ec.OpsModel(postage_out=0.0 if req.local_pickup else 5.75,
                      supplies=0.0 if req.local_pickup else 0.45)
    fees = ec.FeeModel()
    risk = ec.RiskModel().scaled(entry.repro_risk, out["confidence"],
                                 entry.liquidity)
    hurdle = ec.Hurdle()
    days = out["days_to_sell"] or 90.0

    out["max_bid"] = ec.max_bid_for(out["conservative_resale"], req.ship_in,
                                    fees, ops, risk, hurdle, days)
    out["structural_floor"] = round(ec.breakeven_delivered(fees, ops), 2)

    if req.ask is not None:
        deal = ec.evaluate(sku=out["sku"], ask=req.ask, ship_in=req.ship_in,
                           resale=out["conservative_resale"],
                           days_to_sell=days, fees=fees, ops=ops,
                           risk=risk, hurdle=hurdle)
        out["ask"] = req.ask
        out["take"] = deal.take
        out["expected_profit"] = deal.expected_profit
        out["roi"] = round(deal.roi, 3)
        out["reasons"] = deal.reasons
    return out


@app.post("/api/assess")
def assess(req: AssessRequest):
    """Full pipeline on raw listing text — for pasting an online listing in."""
    cand = decide.assess(req.raw_title, req.description, req.ask,
                         req.ship_in, SOURCE)
    if cand.blocked_at:
        return {"blocked_at": cand.blocked_at, "verdict": "reject",
                "reasons": cand.target.reasons[:4]}
    d = cand.deal
    return {
        "sku": d.sku, "verdict": "take" if d.take else "pass",
        "ask": d.ask, "resale": d.resale, "max_bid": d.max_bid,
        "expected_profit": d.expected_profit,
        "expected_days": d.expected_days,
        "profit_per_day": d.profit_per_day,
        "reasons": d.reasons[:4],
        "stage1_verdict": cand.target.verdict.value,
        "source_is_mock": not SOURCE_IS_REAL,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/manifest.json")
    def manifest():
        return FileResponse(STATIC / "manifest.json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript")
