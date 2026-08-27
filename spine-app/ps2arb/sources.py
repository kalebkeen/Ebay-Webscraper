"""
sources.py — combine every real comp source behind one CompSource.

The valuation layer (comps.value_sku) talks to a single `source` with three
methods: quote / sold_records / active_listing_count. In development that was
`mock_sources.CombinedSource`. This module is its real replacement: it layers
whatever real sources are actually configured, so the app improves as each
key is added rather than flipping from "all mock" to "all real" at once.

  * PriceCharting  (pricecharting.py) -> tier prices        [quote]
  * SoldComps      (soldcomps.py)     -> individual sales    [sold_records]
                                       + active supply       [active_listings]
  * Harvest store  (store.py)         -> sales inferred from
                                         listings disappearing [sold_records]

`build_source()` reads the persisted settings, constructs only the sources
whose credentials are present, and returns (source, is_real). With nothing
configured it returns the mock and is_real=False, so the client keeps raising
its "synthetic prices" banner and the app never silently looks live while
serving guesses.

`is_real` is True as soon as ANY real source is wired. That is deliberate:
one real source (say PriceCharting alone) is a real price, and comps already
degrades gracefully when the other shapes are empty.
"""

from __future__ import annotations

import os
import sys
from datetime import date


class LayeredSource:
    """Several CompSources merged into one.

    quote            : first source that returns any tier wins (reference
                       priority order).
    sold_records     : the UNION across every source, so PriceCharting's
                       aggregate and SoldComps' individual sales and the
                       harvest can all contribute. (Sources may observe the
                       same eBay sale; that overlap can modestly inflate the
                       effective sample. Acceptable for now and noted; true
                       cross-source dedup needs shared item ids we do not
                       have.)
    active_listings  : first source that reports a number wins.
    """

    def __init__(self, sources: list, name: str = "layered"):
        # Keep only things that actually look like a CompSource.
        self.sources = [s for s in sources if s is not None]
        self.name = name

    def quote(self, title, region):
        for s in self.sources:
            try:
                q = s.quote(title, region)
            except Exception:
                q = None
            if q:
                return q
        return {}

    def sold_records(self, title, region, since):
        out: list = []
        for s in self.sources:
            try:
                out.extend(s.sold_records(title, region, since) or [])
            except Exception:
                continue
        return sorted(out, key=lambda r: r.sold_on)

    def active_listing_count(self, title, region):
        for s in self.sources:
            try:
                n = s.active_listing_count(title, region)
            except Exception:
                n = None
            if n is not None:
                return n
        return None


def build_source(data_dir=None, today: date | None = None, *,
                 settings=None) -> tuple[object, bool]:
    """Construct the real layered source, or the mock if nothing is set up.

    Reads credentials from a Settings instance (which itself falls back to
    environment variables), so both desktop runs and the synced phone see the
    same keys. Every source is constructed defensively: a bad token or a
    missing optional module drops that one source, never the whole app.
    """
    today = today or date.today()
    settings = settings or _load_settings()

    def cred(field: str) -> str:
        # An explicitly injected settings object (tests) is authoritative.
        if settings is not None:
            try:
                v = (settings.get(field) or "").strip()
                if v:
                    return v
            except Exception:
                pass
            return (os.environ.get(field.upper(), "") or "").strip()
        # Real run: resolve across the settings store, the desktop keystore
        # store, and the environment, so `keystore.py set <token>` is honoured.
        try:
            import settings as _s
            return (_s.resolve(field) or "").strip()
        except Exception:
            return (os.environ.get(field.upper(), "") or "").strip()

    real: list = []

    # SoldComps first: individual sales + live supply are the richest signal.
    sc_token = cred("soldcomps_token")
    if sc_token:
        try:
            import soldcomps
            real.append(soldcomps.SoldCompsSource(sc_token))
        except Exception as exc:                       # noqa: BLE001
            print(f"sources: soldcomps unavailable ({exc})", file=sys.stderr)

    # PriceCharting: the reference tier prices / cross-check.
    pc_token = cred("pricecharting_token")
    if pc_token:
        try:
            import pricecharting
            real.append(pricecharting.PriceChartingSource(pc_token))
        except Exception as exc:                       # noqa: BLE001
            print(f"sources: pricecharting unavailable ({exc})", file=sys.stderr)

    # Harvest store: only once it holds enough observed sales to price with.
    if os.environ.get("EBAY_CLIENT_ID", "").strip():
        try:
            import store
            path = None
            if data_dir is not None:
                from pathlib import Path
                path = Path(data_dir) / "harvest.db"
            harvest = store.HarvestStore(path)
            if harvest.stats().get("sold", 0) >= 20:
                real.append(harvest)
            else:
                harvest.close()
        except Exception as exc:                       # noqa: BLE001
            print(f"sources: harvest unavailable ({exc})", file=sys.stderr)

    if real:
        return LayeredSource(real), True

    import mock_sources as ms
    return (ms.CombinedSource(ms.MockMarketplace(seed=7, today=today),
                              ms.MockReference(today)), False)


def _load_settings():
    try:
        import settings as _settings
        return _settings.Settings()
    except Exception:
        return None
