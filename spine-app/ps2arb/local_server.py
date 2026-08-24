"""
local_server.py — the API on http.server, for running inside the APK.

Same routes as service.py, same responses, no framework. Both are thin
transports over core.py, so there is exactly one copy of the pricing logic
and the phone cannot quietly disagree with the desktop about what a disc is
worth.

Chaquopy can install pure-Python wheels but not compiled ones, which rules
out pydantic and therefore FastAPI. The standard library's ThreadingHTTPServer
is entirely adequate here: one user, a handful of requests, all of them
local. It binds to 127.0.0.1 so nothing on the network can reach it.

Called from Kotlin as:

    from local_server import start
    port = start()          # returns the bound port; runs in a daemon thread
"""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core

# Populated by configure(); kept module-level so the handler class can reach
# them without threading state through BaseHTTPRequestHandler's constructor.
_SOURCE = None
_SOURCE_IS_REAL = False
_UPC = None
_STATIC = Path(__file__).parent / "static"


def configure(source, source_is_real: bool = False, upc_index=None,
              static_dir: Path | None = None) -> None:
    global _SOURCE, _SOURCE_IS_REAL, _UPC, _STATIC
    _SOURCE = source
    _SOURCE_IS_REAL = source_is_real
    _UPC = upc_index
    if static_dir:
        _STATIC = Path(static_dir)


class Handler(BaseHTTPRequestHandler):
    server_version = "Spine/1.0"

    # ------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):
        """Silence. stderr on Android goes to logcat and this is noise."""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._send(404, {"detail": "not found"})
            return
        # Refuse anything that escapes the static root, even though this
        # only listens on loopback. A path-traversal hole is not worth
        # leaving open on the argument that nobody can reach it.
        try:
            path.resolve().relative_to(_STATIC.resolve())
        except ValueError:
            self._send(403, {"detail": "forbidden"})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------------------- routes

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/api/health":
                stats = _UPC.stats() if _UPC else {}
                return self._send(200, core.health(_SOURCE_IS_REAL, stats))

            if route == "/api/titles":
                q = (query.get("q") or [""])[0]
                limit = int((query.get("limit") or ["12"])[0])
                return self._send(200, core.titles(q, limit))

            if route.startswith("/api/upc/"):
                code = urllib.parse.unquote(route[len("/api/upc/"):])
                if _UPC is None:
                    return self._send(503, {"detail": "no upc index"})
                found = _UPC.lookup(code)
                if not found:
                    return self._send(200, {"upc": code, "known": False})
                entry = core.entry_for(found.title)
                return self._send(200, {
                    "upc": code, "known": True, "title": found.title,
                    "variant": getattr(found, "variant", "unknown"),
                    "has_greatest_hits": entry.has_greatest_hits if entry else None,
                    "liquidity": entry.liquidity if entry else None,
                    "repro_risk": entry.repro_risk if entry else None})

            if route == "/" or route == "":
                return self._file(_STATIC / "index.html")
            if route == "/sw.js":
                return self._file(_STATIC / "sw.js")
            if route == "/manifest.json":
                return self._file(_STATIC / "manifest.json")
            if route.startswith("/static/"):
                return self._file(_STATIC / route[len("/static/"):])

            return self._send(404, {"detail": "no such route"})

        except core.ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception as exc:                      # noqa: BLE001
            self._send(500, {"detail": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if route == "/api/value":
                if not body.get("title"):
                    raise core.ApiError(400, "title is required")
                return self._send(200, core.value(
                    _SOURCE, _SOURCE_IS_REAL,
                    title=body["title"],
                    variant=body.get("variant", "unknown"),
                    completeness=body.get("completeness", "loose"),
                    region=body.get("region", "ntsc_u"),
                    ask=body.get("ask"),
                    ship_in=float(body.get("ship_in") or 0.0),
                    local_pickup=bool(body.get("local_pickup", False))))

            if route == "/api/assess":
                return self._send(200, core.assess(
                    _SOURCE,
                    raw_title=body.get("raw_title", ""),
                    description=body.get("description", ""),
                    ask=float(body.get("ask") or 0.0),
                    ship_in=float(body.get("ship_in") or 0.0)))

            if route.startswith("/api/upc/"):
                code = urllib.parse.unquote(route[len("/api/upc/"):])
                if _UPC is None:
                    return self._send(503, {"detail": "no upc index"})
                title = body.get("title", "")
                if core.entry_for(title) is None:
                    raise core.ApiError(404, f"'{title}' is not in the catalog")
                saved = _UPC.teach(code, title,
                                   body.get("variant", "unknown"))
                return self._send(200, {"saved": True, "upc": code,
                                        "title": title,
                                        "observations": getattr(saved, "observations", 1)})

            return self._send(404, {"detail": "no such route"})

        except core.ApiError as exc:
            self._send(exc.status, {"detail": exc.detail})
        except Exception as exc:                      # noqa: BLE001
            self._send(500, {"detail": f"{type(exc).__name__}: {exc}"})


def start(port: int = 0, source=None, source_is_real: bool = False,
          upc_index=None, static_dir: str | None = None) -> int:
    """
    Start the server on a daemon thread and return the bound port.

    Port 0 lets the OS pick a free one, which matters on a phone: a fixed
    port can already be taken by another app, and the failure would be a
    blank WebView with nothing in the log to explain it.
    """
    if source is None:
        import mock_sources as ms
        from datetime import date
        today = date(2026, 8, 22)
        source = ms.CombinedSource(ms.MockMarketplace(seed=7, today=today),
                                   ms.MockReference(today))
        source_is_real = False

    if upc_index is None:
        try:
            import upc
            upc_index = upc.UpcIndex()
        except Exception:                              # noqa: BLE001
            upc_index = None

    configure(source, source_is_real, upc_index,
              Path(static_dir) if static_dir else None)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    bound = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="spine-http")
    thread.start()
    return bound


if __name__ == "__main__":
    p = start(8765)
    print(f"serving on http://127.0.0.1:{p}  (ctrl-c to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
