"""
core.py — the API logic, with no web framework attached.

service.py runs on FastAPI, which cannot go inside the APK: FastAPI needs
pydantic v2, pydantic v2 is compiled Rust, and Chaquopy installs pure-Python
wheels only. The device therefore needs a stdlib http.server.

The wrong fix is to write the handlers twice. Two copies of pricing logic
drift, and they drift silently — the phone quietly disagreeing with the
desktop about what a disc is worth is the exact bug class this project has
spent four stages hunting.

So the logic lives here as plain functions taking and returning dicts.
service.py and local_server.py are both thin transport layers over it.

Handlers raise ApiError with an HTTP status; each transport translates that
into whatever its framework expects.
"""

from __future__ import annotations

import time
from datetime import date

import catalog
import comps
import decide
import economics as ec
from listing_parser import Completeness as C, Region as R, Variant as V

TODAY = date(2026, 8, 22)
CACHE_TTL = 6 * 3600
_CACHE: dict[str, tuple[float, dict]] = {}


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value: dict) -> dict:
    _CACHE[key] = (time.time(), value)
    return value


def _enum(cls, value, fallback):
    try:
        return cls(value)
    except (ValueError, TypeError):
        return fallback


def entry_for(title: str):
    return next((t for t in catalog.CATALOG if t.canonical == title), None)


# ---------------------------------------------------------------- handlers

def health(source_is_real: bool, upc_stats: dict) -> dict:
    return {"ok": True, "catalog": len(catalog.CATALOG),
            "upc": upc_stats, "source_is_mock": not source_is_real}


def titles(q: str = "", limit: int = 12) -> dict:
    """Substring first; subsequence only as a fallback.

    Ranking them as peers is worse than useless on a phone: 'kata' matched
    Kingdom Hearts and Tony Hawk as subsequences and buried the two real
    hits. Someone picking one-handed needs a short correct list.
    """
    needle = (q or "").strip().lower()
    if not needle:
        picked = sorted(catalog.CATALOG, key=lambda t: t.canonical)[:limit]
        return {"results": [_row(t) for t in picked]}

    subs = []
    for t in catalog.CATALOG:
        hay = " ".join([t.canonical] + list(t.aliases)).lower()
        if needle in hay:
            subs.append((hay.index(needle), len(t.canonical), t))
    if subs:
        subs.sort(key=lambda x: (x[0], x[1]))
        return {"results": [_row(t) for _, _, t in subs[:limit]]}

    seq = []
    for t in catalog.CATALOG:
        hay = " ".join([t.canonical] + list(t.aliases)).lower()
        i, start, end = 0, None, None
        for pos, ch in enumerate(hay):
            if i < len(needle) and ch == needle[i]:
                if start is None:
                    start = pos
                end = pos
                i += 1
        if i < len(needle):
            continue
        span = (end - start) if start is not None else len(hay)
        if span > 4 * len(needle) + 8:
            continue
        seq.append((span, len(t.canonical), t))
    seq.sort(key=lambda x: (x[0], x[1]))
    return {"results": [_row(t) for _, _, t in seq[:limit]]}


def _row(t) -> dict:
    return {"title": t.canonical, "liquidity": t.liquidity,
            "repro_risk": t.repro_risk,
            "has_greatest_hits": t.has_greatest_hits,
            "regions": t.regions, "curated": t.curated}


def value(source, source_is_real: bool, *, title: str,
          variant: str = "unknown", completeness: str = "loose",
          region: str = "ntsc_u", ask: float | None = None,
          ship_in: float = 0.0, local_pickup: bool = False,
          price_cache=None) -> dict:
    """Max bid plus the reasoning behind it.

    `local_pickup` is not cosmetic. Removing both shipping legs drops the
    structural floor from about $7.72 to near $1 — the single biggest lever
    in the model, and why in-person sourcing works where mail-order
    arbitrage does not.

    `price_cache`, when given, is consulted before the live source: a SKU the
    desktop already priced comes back instantly and offline, without a comp
    API call (see pricecache.py). Only the resale estimate is cached; the
    economics below still run per request off the live inputs.
    """
    entry = entry_for(title)
    if entry is None:
        raise ApiError(404, f"'{title}' is not in the catalog")

    variant_e = _enum(V, variant, V.UNKNOWN)
    completeness_e = _enum(C, completeness, C.LOOSE)
    region_e = _enum(R, region, R.NTSC_U)

    sku = f"{title}|{region_e.value}|{variant_e.value}|{completeness_e.value}"
    key = f"{sku}|{local_pickup}"
    cached = _cached(key)

    # Precomputed estimate (desktop-priced from the real source) beats a live
    # call. Its realness/cached-ness is stored INSIDE the dict so repeat hits
    # off the in-memory cache stay consistent.
    if cached is None and price_cache is not None:
        row = price_cache.get(sku)
        if row is not None:
            row = dict(row)
            row["_real"] = True          # came from the real desktop source
            row["_cached"] = True
            cached = row
            _store(key, cached)

    if cached is None:
        val = comps.value_sku(
            title=title, region=region_e, variant=variant_e,
            completeness=completeness_e, source=source,
            has_budget_reprint=entry.has_greatest_hits, today=TODAY)
        if not val.quotable:
            cached = {"quotable": False,
                      "warnings": val.warnings or ["not enough comparable sales"]}
        else:
            cached = {
                "quotable": True, "sku": val.sku,
                "expected_resale": val.expected_resale,
                "conservative_resale": val.conservative_resale,
                "p25": val.p25, "p75": val.p75,
                "confidence": val.confidence.value,
                "n_effective": val.n_effective,
                "days_to_sell": val.est_days_to_sell,
                "adjustments": val.adjustments[:4],
                "warnings": val.warnings[:3],
            }
        cached["_real"] = source_is_real
        cached["_cached"] = False
        _store(key, cached)

    out = dict(cached)
    real = out.pop("_real", source_is_real)
    was_cached = out.pop("_cached", False)
    cached_at = out.pop("cached_at", None)
    out.update(title=title, liquidity=entry.liquidity,
               repro_risk=entry.repro_risk,
               has_greatest_hits=entry.has_greatest_hits,
               priced_as_variant=variant_e.value,
               source_is_mock=not real, cached=was_cached)
    if cached_at:
        out["cached_at"] = cached_at
    if not out.get("quotable"):
        return out

    ops = ec.OpsModel(postage_out=0.0 if local_pickup else 5.75,
                      supplies=0.0 if local_pickup else 0.45)
    fees = ec.FeeModel()
    risk = ec.RiskModel().scaled(entry.repro_risk, out["confidence"],
                                 entry.liquidity)
    hurdle = ec.Hurdle()
    days = out["days_to_sell"] or 90.0

    out["max_bid"] = ec.max_bid_for(out["conservative_resale"], ship_in,
                                    fees, ops, risk, hurdle, days)
    out["structural_floor"] = round(ec.breakeven_delivered(fees, ops), 2)

    if ask is not None:
        deal = ec.evaluate(sku=out["sku"], ask=ask, ship_in=ship_in,
                           resale=out["conservative_resale"],
                           days_to_sell=days, fees=fees, ops=ops,
                           risk=risk, hurdle=hurdle)
        out.update(ask=ask, take=deal.take,
                   expected_profit=deal.expected_profit,
                   roi=round(deal.roi, 3), reasons=deal.reasons)
    return out


def assess(source, *, raw_title: str, description: str = "",
           ask: float = 0.0, ship_in: float = 0.0) -> dict:
    """Full pipeline over raw listing text — for pasting a live listing in."""
    cand = decide.assess(raw_title, description, ask, ship_in, source)
    if cand.blocked_at:
        return {"blocked_at": cand.blocked_at, "verdict": "reject",
                "reasons": cand.target.reasons[:4]}
    d = cand.deal
    t = cand.target
    v = cand.valuation
    entry = entry_for(t.title) if t.title else None

    # A verdict has to say WHAT it is verdicting on. Returning "take" with
    # only an opaque sku string meant the caller could not show the game,
    # the variant it was priced as, or the risk attached to it -- and a
    # confident buy signal on a high-repro title with no warning attached is
    # the single most expensive thing this app could display.
    warnings = list(v.warnings[:2]) if v else []
    if entry and entry.repro_risk == "high":
        warnings.append("Reproductions are common for this title — "
                        "check the disc face before paying.")
    if t.variant.value == "unknown" and entry and entry.has_greatest_hits:
        warnings.append("Variant unconfirmed — priced as Greatest Hits. "
                        "Black label is worth more; check the spine.")

    return {"sku": d.sku, "verdict": "take" if d.take else "pass",
            "title": t.title,
            "variant": t.variant.value,
            "completeness": t.completeness.value,
            "region": t.region.value,
            "confidence": v.confidence.value if v else None,
            "n_effective": round(v.n_effective, 1) if v else None,
            "liquidity": entry.liquidity if entry else None,
            "repro_risk": entry.repro_risk if entry else None,
            "ask": d.ask, "resale": d.resale, "max_bid": d.max_bid,
            "expected_profit": d.expected_profit,
            "expected_days": d.expected_days,
            "profit_per_day": d.profit_per_day,
            "reasons": d.reasons,
            "warnings": warnings[:3]}
