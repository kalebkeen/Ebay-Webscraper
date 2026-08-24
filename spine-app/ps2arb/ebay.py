"""
ebay.py — eBay Browse API client. Standard library only.

No `requests`, no third-party HTTP. The whole pipeline is now C-extension
free so it can run inside an APK under Chaquopy, and this keeps it that way.

WHAT THIS CAN AND CANNOT SEE
----------------------------
Browse API returns ACTIVE listings. It does not return sold prices, and the
distinction is the single most important fact about building on eBay:

    active listings  -> what sellers HOPE to get. Aspirational, often 2-3x
                        reality, and useless as a comp.
    sold listings    -> what buyers ACTUALLY paid. This is what Stage 2
                        needs, and eBay gates it behind the Marketplace
                        Insights API, which requires a separate application
                        and is not granted to most developers.

So this module deliberately does two different jobs and never confuses them:

    search()        -> candidate listings to evaluate and possibly buy
    sold_records()  -> comps, from Marketplace Insights IF you have been
                       granted access, otherwise from your own harvested
                       observations (see store.py)

If you price against active listings because sold data was inconvenient to
obtain, every number downstream is inflated and the system will tell you
that overpriced listings are bargains.

CREDENTIALS
-----------
Set these; never hardcode them.

    export EBAY_CLIENT_ID=...
    export EBAY_CLIENT_SECRET=...
    export EBAY_ENV=production        # or: sandbox

Application tokens use the client-credentials grant, which reaches public
data only. That is all this needs -- no user consent flow, no refresh token.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator

# eBay's own category for video games. Narrowing by category before the
# keyword query removes an enormous amount of noise -- guides, posters,
# empty cases, and console bundles all live elsewhere.
CATEGORY_VIDEO_GAMES = "139973"

ENDPOINTS = {
    "production": {
        "oauth": "https://api.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.ebay.com/buy/browse/v1",
        "insights": "https://api.ebay.com/buy/marketplace_insights/v1_beta",
    },
    "sandbox": {
        "oauth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.sandbox.ebay.com/buy/browse/v1",
        "insights": "https://api.sandbox.ebay.com/buy/marketplace_insights/v1_beta",
    },
}

SCOPE_PUBLIC = "https://api.ebay.com/oauth/api_scope"


class EbayError(RuntimeError):
    pass


class NotEntitled(EbayError):
    """Raised when an endpoint exists but this key is not approved for it."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _http(method: str, url: str, headers: dict, body: bytes | None = None,
          timeout: float = 20.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:400]}
        return e.code, payload
    except urllib.error.URLError as e:
        raise EbayError(f"network error: {e.reason}") from e


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class EbayAuth:
    """Client-credentials token with in-memory caching.

    Application tokens last about two hours. Fetching one per request would
    burn call quota and add latency to every lookup, so it is cached and
    refreshed a minute early to avoid a race against expiry.
    """

    def __init__(self, client_id: str | None = None,
                 client_secret: str | None = None,
                 env: str | None = None,
                 transport: Callable = _http):
        self.client_id = client_id or os.environ.get("EBAY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("EBAY_CLIENT_SECRET", "")
        self.env = env or os.environ.get("EBAY_ENV", "production")
        if self.env not in ENDPOINTS:
            raise EbayError(f"EBAY_ENV must be one of {list(ENDPOINTS)}")
        self._transport = transport
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def token(self) -> str:
        if not self.configured:
            raise EbayError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not set. "
                "Create an app key at developer.ebay.com and export them.")
        if self._token and time.time() < self._expires_at:
            return self._token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "scope": SCOPE_PUBLIC}).encode()
        status, payload = self._transport(
            "POST", ENDPOINTS[self.env]["oauth"],
            {"Authorization": f"Basic {basic}",
             "Content-Type": "application/x-www-form-urlencoded"}, body)

        if status != 200:
            # Never echo the payload verbatim: it can contain the credential.
            raise EbayError(
                f"token request failed ({status}). "
                f"{payload.get('error_description', 'Check your keys and EBAY_ENV.')}")
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 7200)) - 60
        return self._token


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class RateLimit:
    """Browse API allows roughly 5,000 calls a day on a default key.

    A scan that walks the feed can exhaust that in minutes, so calls are
    spaced and counted. Hitting the ceiling mid-scan is worse than scanning
    slowly: you lose the rest of the day.
    """
    min_interval_s: float = 0.12
    daily_budget: int = 4500
    _last_call: float = 0.0
    _count: int = 0
    _day: str = ""

    def wait(self) -> None:
        today = date.today().isoformat()
        if today != self._day:
            self._day, self._count = today, 0
        if self._count >= self.daily_budget:
            raise EbayError(
                f"daily call budget ({self.daily_budget}) exhausted. "
                "Raise it, narrow your searches, or wait for the reset.")
        gap = time.time() - self._last_call
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_call = time.time()
        self._count += 1

    @property
    def used(self) -> int:
        return self._count


@dataclass
class Listing:
    """One active listing, normalised. Feeds straight into pipeline.resolve."""
    item_id: str
    title: str
    price: float
    shipping: float
    currency: str = "USD"
    condition: str = ""
    condition_id: int | None = None
    url: str = ""
    image: str = ""
    seller: str = ""
    feedback_pct: float | None = None
    country: str = ""
    buying_options: tuple = ()
    best_offer: bool = False
    description: str = ""
    listed_on: date | None = None

    @property
    def delivered(self) -> float:
        """What the buyer pays. The basis everything downstream uses."""
        return round(self.price + self.shipping, 2)


class EbayClient:
    def __init__(self, auth: EbayAuth | None = None,
                 marketplace: str = "EBAY_US",
                 rate: RateLimit | None = None,
                 transport: Callable = _http):
        self.auth = auth or EbayAuth(transport=transport)
        self.marketplace = marketplace
        self.rate = rate or RateLimit()
        self._transport = transport

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.auth.token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "Content-Type": "application/json",
        }

    def _get(self, url: str, retries: int = 3) -> dict:
        for attempt in range(retries):
            self.rate.wait()
            status, payload = self._transport("GET", url, self._headers())
            if status == 200:
                return payload
            if status in (403,):
                raise NotEntitled(
                    "403 from eBay. This key is not entitled to that endpoint "
                    "— Marketplace Insights in particular requires a separate "
                    "application.")
            if status == 401:
                # Token may have been revoked early; force one refresh.
                self.auth._token = None
                if attempt == 0:
                    continue
                raise EbayError("401 unauthorised after refresh.")
            if status in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 8))
                continue
            errs = payload.get("errors") or []
            msg = errs[0].get("message") if errs else str(payload)[:200]
            raise EbayError(f"eBay returned {status}: {msg}")
        raise EbayError(f"gave up after {retries} attempts: {url}")

    # -------------------------------------------------------------- search

    def search(self, query: str, *, limit: int = 200, category: str | None = CATEGORY_VIDEO_GAMES,
               filters: str | None = None, sort: str | None = "newlyListed",
               page_size: int = 50) -> Iterator[Listing]:
        """Walk active listings for one query.

        `sort='newlyListed'` is the default on purpose. Underpriced buy-it-now
        listings are taken within minutes, so a scan ordered by recency sees
        the only inventory it has any chance of winning. Sorting by price
        surfaces the same stale bargain-priced junk on every run.
        """
        base = f"{ENDPOINTS[self.auth.env]['browse']}/item_summary/search"
        offset, seen = 0, 0
        while seen < limit:
            params: dict[str, Any] = {
                "q": query,
                "limit": str(min(page_size, limit - seen)),
                "offset": str(offset),
            }
            if category:
                params["category_ids"] = category
            if filters:
                params["filter"] = filters
            if sort:
                params["sort"] = sort
            payload = self._get(base + "?" + urllib.parse.urlencode(params))

            items = payload.get("itemSummaries") or []
            if not items:
                return
            for raw in items:
                seen += 1
                yield to_listing(raw)
            total = int(payload.get("total", 0))
            offset += len(items)
            if offset >= total:
                return

    def item_detail(self, item_id: str) -> dict:
        """Full item record, including the description.

        Costs one call each, so this is a SECOND-PASS operation. Search
        summaries carry no description, and the description is where sellers
        bury the defects Stage 1 is built to catch -- so fetch detail only
        for listings that already look worth buying on title alone.
        """
        url = (f"{ENDPOINTS[self.auth.env]['browse']}/item/"
               f"{urllib.parse.quote(item_id, safe='')}")
        return self._get(url)

    def enrich(self, listing: Listing) -> Listing:
        detail = self.item_detail(listing.item_id)
        listing.description = _strip_html(
            detail.get("description") or detail.get("shortDescription") or "")
        return listing

    # ----------------------------------------------------- sold (if granted)

    def sold_search(self, query: str, *, days_back: int = 180,
                    limit: int = 200, category: str | None = CATEGORY_VIDEO_GAMES):
        """Marketplace Insights: real sold prices. Requires approval.

        Raises NotEntitled if this key has not been granted access, which is
        the normal case. store.HarvestStore is the fallback.
        """
        base = f"{ENDPOINTS[self.auth.env]['insights']}/item_sales/search"
        start = _iso_days_ago(days_back)
        params = {
            "q": query,
            "limit": str(min(limit, 200)),
            "filter": f"lastSoldDate:[{start}..]",
        }
        if category:
            params["category_ids"] = category
        payload = self._get(base + "?" + urllib.parse.urlencode(params))
        return payload.get("itemSales") or []


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def _money(node: dict | None) -> float:
    if not node:
        return 0.0
    try:
        return float(node.get("value", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _cheapest_shipping(raw: dict) -> float:
    """Lowest advertised shipping. Free shipping reports as 0.0, correctly.

    eBay charges its fee on item + shipping either way, so bundling postage
    into the price changes nothing about the economics -- which is exactly
    why the pipeline works in delivered cost throughout.
    """
    opts = raw.get("shippingOptions") or []
    costs = [_money(o.get("shippingCost")) for o in opts]
    return min(costs) if costs else 0.0


def _strip_html(text: str) -> str:
    """Descriptions are seller-authored HTML, frequently a wall of template.

    Stage 1 scans plain text, and an unstripped description turns every
    listing into a soup of style attributes that swamps the defect phrases.
    """
    import re
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return re.sub(r"[ \t]+", " ", text).strip()


def to_listing(raw: dict) -> Listing:
    seller = raw.get("seller") or {}
    listed = raw.get("itemCreationDate")
    when = None
    if listed:
        try:
            when = datetime.fromisoformat(listed.replace("Z", "+00:00")).date()
        except ValueError:
            when = None
    options = tuple(raw.get("buyingOptions") or ())
    return Listing(
        item_id=raw.get("itemId", ""),
        title=raw.get("title", ""),
        price=_money(raw.get("price")),
        shipping=_cheapest_shipping(raw),
        currency=(raw.get("price") or {}).get("currency", "USD"),
        condition=raw.get("condition", ""),
        condition_id=_safe_int(raw.get("conditionId")),
        url=raw.get("itemWebUrl", ""),
        image=((raw.get("image") or {}).get("imageUrl", "")),
        seller=seller.get("username", ""),
        feedback_pct=_safe_float(seller.get("feedbackPercentage")),
        country=((raw.get("itemLocation") or {}).get("country", "")),
        buying_options=options,
        best_offer="BEST_OFFER" in options,
        description=_strip_html(raw.get("shortDescription") or ""),
        listed_on=when,
    )


def _safe_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------------------
# Region inference — the gap flagged since Stage 2
# ---------------------------------------------------------------------------

# Stage 1 cannot read region from listing text, because US sellers never
# write "NTSC-U". Guessing costs ~30%: a PAL copy priced as NTSC-U looks
# like a bargain and is not. Seller location plus marketplace is the
# reliable signal, and it arrives free with every search result.
_COUNTRY_REGION = {
    "US": "ntsc_u", "CA": "ntsc_u", "MX": "ntsc_u",
    "GB": "pal", "IE": "pal", "DE": "pal", "FR": "pal", "ES": "pal",
    "IT": "pal", "NL": "pal", "BE": "pal", "AU": "pal", "NZ": "pal",
    "SE": "pal", "NO": "pal", "DK": "pal", "FI": "pal", "PL": "pal",
    "JP": "ntsc_j",
}


def infer_region(listing: Listing, marketplace: str = "EBAY_US") -> str:
    """Region from seller country, falling back to the marketplace."""
    if listing.country in _COUNTRY_REGION:
        return _COUNTRY_REGION[listing.country]
    return {"EBAY_US": "ntsc_u", "EBAY_GB": "pal", "EBAY_DE": "pal",
            "EBAY_AU": "pal", "EBAY_JP": "ntsc_j"}.get(marketplace, "unknown")


# ---------------------------------------------------------------------------
# Search construction
# ---------------------------------------------------------------------------

def build_filter(*, min_price: float | None = None, max_price: float | None = None,
                 buy_it_now: bool = True, auctions: bool = False,
                 conditions: tuple[str, ...] = ("USED",),
                 max_shipping: float | None = None,
                 exclude_countries: tuple[str, ...] = ()) -> str:
    """Compose a Browse API filter string.

    Filtering server-side is not just tidy -- every listing excluded here is
    one that does not consume your daily call budget downstream.
    """
    parts = []
    if min_price is not None or max_price is not None:
        lo = "" if min_price is None else f"{min_price:.2f}"
        hi = "" if max_price is None else f"{max_price:.2f}"
        parts.append(f"price:[{lo}..{hi}]")
        parts.append("priceCurrency:USD")
    options = []
    if buy_it_now:
        options.append("FIXED_PRICE")
    if auctions:
        options.append("AUCTION")
    if options:
        parts.append("buyingOptions:{" + "|".join(options) + "}")
    if conditions:
        parts.append("conditions:{" + "|".join(conditions) + "}")
    if max_shipping is not None:
        parts.append(f"maxDeliveryCost:{max_shipping:.2f}")
    if exclude_countries:
        parts.append("excludeSellers:{" + "|".join(exclude_countries) + "}")
    return ",".join(parts)
