"""
httpjson.py — one small stdlib HTTP-GET-JSON helper for the comp adapters.

The real comp sources (PriceCharting, SoldComps, and any future REST comp
source) all speak the same shape: an authenticated GET that returns JSON, on
a flaky public endpoint that occasionally 429s or 503s. Rather than copy the
retry loop into each adapter -- and drift them -- it lives here once.

Stdlib only (urllib), because these adapters are bundled into the APK where
every third-party wheel is a build risk. Same constraint as ebay.py and
identify.py.

Contract, chosen so an adapter NEVER has to raise on a network blip:

    status, data = get_json(url, headers=...)

  * status is the HTTP status, or 0 if the request never completed (DNS
    failure, timeout, connection refused -- the offline case).
  * data is the parsed JSON on 2xx, else None.

A caller that gets (0, None) or a non-2xx status returns "no comps", which
the valuation layer reads as thin data and prices pessimistically -- exactly
the safe direction. Nothing here decides pricing; it only fetches.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# Transient statuses worth a retry. A 401/403/404 is a configuration or
# lookup miss and will not fix itself, so those return immediately.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

_UA = "spine-ps2arb/1.0 (+https://github.com/kalebkeen/Ebay-Webscraper)"


def get_json(url: str, *, headers: dict | None = None, timeout: float = 30.0,
             attempts: int = 3, backoff: float = 1.0,
             opener=urllib.request.urlopen):
    """GET `url` and parse JSON. Returns (status, data-or-None).

    `opener` is injectable so tests can drive the adapters without a network:
    pass a callable with urlopen's signature. Retries transient statuses and
    network errors with exponential backoff; never raises for a network or
    HTTP condition (a JSON that will not decode surfaces as (status, None)).
    """
    hdrs = {"Accept": "application/json", "User-Agent": _UA}
    if headers:
        hdrs.update(headers)

    last_status = 0
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, method="GET")
            for k, v in hdrs.items():
                req.add_header(k, v)
            with opener(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                raw = resp.read().decode("utf-8", "replace")
                return status, _loads(raw)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            # Body is drained so a caller/logger could inspect it, but a
            # reference/marketplace GET has nothing actionable in an error
            # body, so it is discarded.
            try:
                exc.read()
            except Exception:
                pass
            if exc.code not in _RETRY_STATUS or i == attempts - 1:
                return exc.code, None
        except (urllib.error.URLError, OSError, ValueError):
            # Offline, DNS failure, TLS error, timeout. Retry, then give up
            # as status 0 rather than raising into the valuation path.
            if i == attempts - 1:
                return 0, None
        time.sleep(backoff * (2 ** i))
    return last_status, None


def _loads(raw: str):
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
