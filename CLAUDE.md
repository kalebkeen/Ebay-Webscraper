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
1. **Real comp source** — replace `mock_sources.CombinedSource`. The only true
   blocker. Gated on eBay dev-account approval (reminder ~28 Aug) or the
   `store.py` harvest.
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
- **Tests** are plain scripts: `python test_x.py` (exit code), no pytest. Nine
  suites: corpus, adversarial, comps, economics, backtest, keystore, vault,
  identify, photo. Run all + the stdlib guard before pushing.
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

## Workflow
- After changes: run all suites + the stdlib guard; commit + push to `main`
  (CI builds the APK). Document back-end changes in `docs/KEYSTORE-DESIGN.md`.
- Desktop deploy after a push: `git -C C:\Users\kaleb\spine pull`, stop the
  running keystore python (CommandLine matches `keystore.py`), relaunch the
  3.12 `pythonw` — or reboot (the Startup shortcut points at 3.12).

## Module map (`spine-app/ps2arb/`)
- **Identify:** `listing_parser`, `catalog` (+`catalog_data`), `fuzzy`,
  `sequel`, `pipeline`, `upc`, `scandex`, `identify` (photo/vision),
  `ebay` (GTIN).
- **Value:** `comps`, `store` (harvest), `economics`, `decide`, `backtest`,
  `timeline`, `mock_sources`.
- **Serve:** `core`, `service` (FastAPI, desktop), `local_server` (on-device),
  `settings`, `snapshot`, `static/index.html`.
- **Desktop back-end (NOT bundled):** `keystore`, `vault` (Pillow + CLIP).
- **Deep reference:** `docs/KEYSTORE-DESIGN.md`. **Session history:**
  `~/Downloads/HANDOFF-*.md`.
