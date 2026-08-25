"""
android_main.py — the single entry point Kotlin calls.

Kept deliberately small. Everything it touches is already tested on the
desktop; this file exists only to bridge two conventions and to make the
device-specific decisions that the desktop never has to make.

Two of those decisions matter:

  WRITABLE PATHS. An APK's assets are read-only. The UPC map and the harvest
  database must live under the app's private files directory or the first
  write throws and the app looks broken. Resolved here, once.

  WHICH SOURCE. If eBay credentials are present the real adapter is used;
  otherwise it falls back to the mock and the client raises its banner. The
  app should never silently serve synthetic prices while looking live.
"""

import os
import sys
from pathlib import Path

_PORT = None


def _data_dir() -> Path:
    """App-private storage. Writable; cleared only on uninstall."""
    try:
        from com.chaquo.python import Python
        ctx = Python.getPlatform().getApplication()
        return Path(str(ctx.getFilesDir()))
    except Exception:
        return Path(os.environ.get("SPINE_DATA", "."))


def _static_dir() -> Path:
    """The web client, shipped alongside the Python in src/main/python."""
    return Path(__file__).parent / "static"


def _build_source(data: Path):
    """Real comps if configured, mock otherwise. Never silently mixed."""
    from datetime import date
    today = date(2026, 8, 22)

    client_id = os.environ.get("EBAY_CLIENT_ID", "")
    if client_id:
        try:
            import store
            harvest = store.HarvestStore(data / "harvest.db")
            # Only trust the harvest once it has enough observed sales to
            # price anything; before that it would return empty results that
            # read as "no comps" rather than "not ready yet".
            if harvest.stats().get("sold", 0) >= 20:
                return harvest, True
        except Exception as exc:                      # noqa: BLE001
            print(f"harvest unavailable: {exc}", file=sys.stderr)

    import mock_sources as ms
    return (ms.CombinedSource(ms.MockMarketplace(seed=7, today=today),
                              ms.MockReference(today)), False)


def start() -> int:
    """Start the loopback server and return its port. Idempotent."""
    global _PORT
    if _PORT is not None:
        return _PORT

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)

    # Modules read these at import time, so they must be set first.
    os.environ.setdefault("PS2ARB_UPC", str(data / "upc_map.json"))
    os.environ.setdefault("PS2ARB_STORE", str(data / "harvest.db"))
    os.environ.setdefault("EBAY_TOKEN_CACHE", str(data / "ebay_token.json"))
    os.environ.setdefault("SCANDEX_CACHE", str(data / "scandex_cache.json"))

    import local_server

    # Optional bulk barcode coverage. With no token this stays None and the
    # resolver falls back to the local index and manual entry -- the intended
    # default, not a degraded mode.
    scandex_client = None
    try:
        import scandex
        candidate = scandex.ScanDexClient()
        scandex_client = candidate if candidate.configured else None
    except Exception as exc:          # never block startup on a barcode extra
        print(f"spine: scandex unavailable ({exc})")

    source, is_real = _build_source(data)
    _PORT = local_server.start(
        port=0, source=source, source_is_real=is_real,
        static_dir=str(_static_dir()),
        scandex_client=scandex_client)
    print(f"spine: serving on 127.0.0.1:{_PORT} (real_source={is_real})")
    return _PORT


def port() -> int:
    return _PORT or 0
