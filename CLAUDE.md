# CLAUDE.md — Spine (PS2 Arbitrage Pipeline)

Identify PS2 game listings/covers, value them, output the most you should pay
(`max_bid`). Phone app (Chaquopy WebView) + a desktop back-end (keystore + data
vault) over Tailscale. Repo: github.com/kalebkeen/Ebay-Webscraper — project at
`spine-app/ps2arb/`.

## North star — read before adding features
The goal is **real valuation. Prices are still synthetic — wiring a real comp
source is priority #1.** Weigh every new feature against that; don't let the app
sprawl faster than it gets real. If a request grows scope, say so and offer the
smaller version first.

**Priorities (high → low):**
1. **Real comp source** — replace `mock_sources.CombinedSource`. Real adapters
   now exist (`sources.build_source` layers PriceCharting + SoldComps +
   harvest); going live is gated only on **setting the tokens** on the desktop
   keystore (`keystore.py set pricecharting_token …` / `soldcomps_token …`).
   eBay dev-account approval and the `store.py` harvest are now optional extra
   sources, no longer the sole path.
2. Recalibrate `CONSERVATIVE_QUANTILE` against real data.
3. Real `liquidity` / `repro_risk` (both are priors now).

**Deferred / not now:** fine-tuning a vision model; more catalog scraping;
anything that doesn't move toward real prices.

## Hard constraints (violating these = bugs or rework)
- **Phone/APK is stdlib-only** (Chaquopy). No third-party imports in bundled
  modules; `sync_android.sh` enforces it.
- **Edit ROOT files** in `spine-app/ps2arb/`. CI runs `sync_android.sh` to
  regenerate `android/app/src/main/python/` — the committed bundle is **not
  authoritative**; never hand-edit it.
- **Desktop-only modules** (never add to `sync_android.sh` MODULES):
  `keystore.py`, `vault.py`, and anything importing Pillow / torch / numpy /
  sentence-transformers.
- **The desktop keystore runs under Python 3.12**
  (`C:\Users\kaleb\AppData\Local\Programs\Python\Python312\`), NOT 3.14 — torch
  has no 3.14 wheels. 3.14 still runs the keystore but falls back to dHash.
- **Tests** are plain scripts: `python test_x.py` (exit code), no pytest.
  Fifteen suites: corpus, adversarial, comps, economics, backtest, keystore,
  vault, identify, photo, outbox, phash, sources, pricecache, seed, outcomes.
  Run all + the stdlib guard before pushing.
- Windows `python3` is a Store stub; use `/c/Python314/python` or the 3.12 path.
- **Secrets:** Claude never enters API keys. The user sets them on the desktop
  keystore (`python keystore.py set <field> <value>`); the phone syncs them.

## Design philosophy (enforce it)
- **Never silently feed a wrong price into a buy decision.** Pessimistic
  defaults; unconfirmed variant/completeness prices as the cheap/loose option;
  confirm before pricing.
- **Don't dress guesses as data.** Priors are marked as priors (`curated` flag,
  "uncurated" reasons); measured data replaces them.
- **Identification is "just another resolver":** any input (barcode, ScanDex,
  photo) produces a candidate TITLE → `catalog.match()` → confirm → the same
  valuation path.
- Photo identify keeps the vision model internal (product choice) but keeps
  confidence/confirm visible.
- **The offline cover matcher abstains rather than guesses.** It runs when
  nothing else can, so nothing is left to catch it being wrong. `phash_index`
  refuses on distance (cutoff) and on ambiguity (a different title within
  `margin` bits — sequels share box art). Falling through to the normal path is
  nearly free; a confident wrong title is not. Measured: 0% wrong answers.
- **`vault._boxhash_rgba` and `coverHash()` in `static/index.html` are one
  algorithm in two languages.** A change to either without the other silently
  disables offline matching. `test_phash.py` pins the bit layout.

## Workflow
- After changes: run all suites + the stdlib guard; commit + push to `main`
  (CI builds the APK). Document back-end changes in `docs/KEYSTORE-DESIGN.md`.
- Desktop deploy after a push: `git -C C:\Users\kaleb\spine pull`, stop the
  running keystore python (CommandLine matches `keystore.py`), relaunch the
  3.12 `pythonw` with **`keystore.py serve --open`** — or reboot (the Startup
  shortcut already runs 3.12 + `--open`). `--open` = Tailscale-only, token-free
  for tailnet/loopback IPs; omit it and the phone (which syncs token-free) 401s.

## Module map (`spine-app/ps2arb/`)
- **Identify:** `listing_parser`, `catalog` (+`catalog_data`), `fuzzy`,
  `sequel`, `pipeline`, `upc`, `scandex`, `identify` (photo/vision),
  `ebay` (GTIN), `seed_covers` (desktop-only CLI: seeds the vault photo index
  with libretro box art so known covers skip the vision call).
- **Value:** `comps`, `sources` (layers real sources + `build_source`),
  `pricecharting`, `soldcomps`, `httpjson` (shared stdlib GET), `pricecache`
  (bundled; precomputed estimates the phone reads for instant/offline prices),
  `precompute` (desktop-only harvester CLI that fills the cache), `store`
  (harvest), `economics`, `decide`, `backtest`, `timeline`, `mock_sources`,
  `outcomes` (bundled realized-flip log — paid vs sold — the ground truth for
  recalibrating `CONSERVATIVE_QUANTILE`; syncs to the vault).
- **Serve:** `core`, `service` (FastAPI, desktop), `local_server` (on-device),
  `settings`, `snapshot`, `static/index.html`.
- **Desktop back-end (NOT bundled):** `keystore`, `vault` (Pillow + CLIP).
- **Deep reference:** `docs/KEYSTORE-DESIGN.md`. **Session history:**
  `~/Downloads/HANDOFF-*.md`.
