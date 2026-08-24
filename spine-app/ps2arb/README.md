# PS2 Field Desk

A four-stage valuation pipeline for PS2 game arbitrage, plus **Spine** — a
phone client that answers one question in a shop aisle: *what's the most I
should pay for this disc?*

Requires **Python 3.10 or newer**.

---

## 1. Lay the files out

Everything downloads flat. Four files must go into a `static/` subfolder or
the app will not serve:

```
ps2arb/
├── requirements.txt
│
├── decide.py            ← run this for the offline pipeline
├── api.py               ← run this for the phone app
├── upc_store.py
│
├── listing_parser.py    Stage 1 — identification
├── catalog.py
├── sequel.py
├── pipeline.py
├── comps.py             Stage 2 — valuation
├── economics.py         Stage 3 — economics
├── backtest.py          Stage 4 — backtest
├── timeline.py
├── mock_sources.py
│
├── test_corpus.py       test_adversarial.py   test_comps.py
├── test_economics.py    test_backtest.py
│
└── static/              ← create this folder
    ├── index.html
    ├── sw.js
    ├── manifest.json
    └── icon.svg
```

```bash
mkdir -p ps2arb/static
cd ps2arb
mv index.html sw.js manifest.json icon.svg static/
```

---

## 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Verify the pipeline before worrying about the app:

```bash
python decide.py
for f in test_*.py; do python "$f"; done
```

You should see 15/15, 13/13, 9/9, 14/14, 8/8. `test_backtest.py` takes a few
minutes — it builds several full market timelines.

---

## 3. Run the server

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000> on the same machine. Everything works here,
including the camera, because browsers treat `localhost` as a secure origin.

---

## 4. Get it on your phone

The camera and the service worker both require a **secure origin**. Plain
`http://192.168.x.x` is not one, so the scan button will fail there even
though the rest of the app works. Pick whichever fits.

### A. LAN, no camera — one minute

Find your machine's address (`ipconfig getifaddr en0` on macOS,
`hostname -I` on Linux) and open `http://192.168.x.x:8000` on your phone.
Title search, pricing, and the pickup toggle all work; barcode scanning does
not. Fine for a first look.

### B. LAN with camera, via a Chrome flag — testing only

On Android Chrome open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`,
add `http://192.168.x.x:8000`, and relaunch. Scanning now works. Do not
leave this flag set on a phone you use for anything else.

### C. HTTPS with Cloudflare Tunnel — the real answer

Free, no port forwarding, no router configuration.

```bash
brew install cloudflared          # or: apt install cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://something-random.trycloudflare.com` URL. Open that on
your phone. Camera works, service worker installs, and Chrome will offer
**Add to Home Screen** — which gives you a standalone app icon with no Play
Store involvement.

The quick-tunnel URL changes on every restart. For a stable address, run
`cloudflared tunnel login` and create a named tunnel.

### D. Tailscale — if you already use it

```bash
tailscale serve --bg 8000
```

Gives you an HTTPS address on your tailnet, reachable only from your own
devices. The best option if you don't want the endpoint on the open internet.

---

## 5. Teaching it barcodes

`upc_map.json` **starts empty, deliberately.** There is no free, reliable UPC
database for PS2 games, and a wrong mapping is worse than a missing one — it
silently prices the disc in your hand as a different game.

So it learns as you go: scan an unknown barcode, pick the title once, tap
**Remember this barcode**. Every later scan of that code resolves instantly,
including the variant — black label and Greatest Hits carry different UPCs,
so a scan gives you a complete SKU.

A mapping seen once is served with a warning. Seen twice, it's trusted.

Store location is configurable:

```bash
PS2ARB_UPC_STORE=/var/lib/spine/upc_map.json uvicorn api:app --host 0.0.0.0
```

---

## 6. Before you trust a number

**The prices are synthetic.** `SOURCE_IS_MOCK = True` at the top of `api.py`
drives a standing banner in the client for exactly this reason. Three things
stand between here and a tool you'd act on:

1. **Real comps.** Replace `mock_sources.CombinedSource` in `api.py` with a
   PriceCharting or eBay adapter. It needs three methods — `sold_records`,
   `active_listing_count`, `quote` — and everything downstream is
   source-agnostic. Then set `SOURCE_IS_MOCK = False`.

2. **A real catalog.** `catalog.py` has 30 titles; the NTSC-U library is
   about 1,800.

3. **Auth**, if the endpoint is reachable from the internet. `POST /api/upc`
   currently lets anyone rewrite your barcode map.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | status, catalog size, whether the source is mock |
| `GET` | `/api/titles?q=` | incremental search — substring first, subsequence as fallback |
| `GET` | `/api/upc/{code}` | barcode → title and remembered variant |
| `POST` | `/api/upc/{code}` | teach a barcode |
| `POST` | `/api/value` | the one call the client makes |

```bash
curl -X POST localhost:8000/api/value -H 'Content-Type: application/json' \
  -d '{"title":"God Hand","variant":"black_label","completeness":"cib",
       "ask":25,"local_pickup":true}'
```

---

## Why local pickup is a toggle and not a checkbox you ignore

Postage is $5.75 out and roughly the same in. Removing both drops the
structural floor — the delivered price below which a *free* copy still loses
money — from **$7.72 to about $0.99**.

Katamari Damacy CIB, same disc:

| | max bid | floor |
|---|---|---|
| Collected in person | **$21.82** | $0.99 |
| Bought and shipped | **$11.98** | $7.72 |

That gap is the whole reason this is a phone tool rather than a scraper. The
backtest found that 88% of a shipped feed sits above the floor but below the
workable band. In-person sourcing is where the arithmetic changes.
