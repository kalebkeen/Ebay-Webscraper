# Design — Spine Keystore (and eventual Data Vault)

**Status:** Phase 1 **implemented** (2026-08-25) — reachability = **Tailscale**,
keystore auto-starts (§9). **Phase 2 first slice implemented** (2026-08-25):
the learned barcode index now backs up to a desktop vault (§4a). Remaining
Phase 2 (ScanDex cache, catalog snapshot, eBay sold-price harvest) still design.
**Author:** drafted with Claude, 2026-08-25.
**Problem it solves:** API keys (ScanDex today; eBay/PriceCharting tomorrow)
rotate and expire. Right now each new key must be hand-pasted into the phone's
settings panel. When the ScanDex token expired, scanning silently broke until
re-pasted. The goal is that **tokens refresh themselves** — the user stores
durable login/credentials once, in one place we control (this desktop), and
never touches a key again *unless something actually fails*. The phone fetches
whatever's currently valid. Later: own the data we collect so we survive
ScanDex/eBay disappearing or starting to charge.

> **Clarified requirement (2026-08-25):** rotation must be automatic; manual
> action only on failure. User is fine storing login credentials on the local
> server for the app to use. See §2b — this is achievable *and already how it
> works* for OAuth-style services (eBay); ScanDex is the one exception and the
> reason is documented there.

---

## 1. Goals and non-goals

**Goals**
- One source of truth for service keys, edited in one place (this desktop).
- The phone fetches the current keys itself and caches them; a rotated key
  means updating the desktop once, not re-pasting into every device.
- Reachable from the phone **anywhere** (not just home WiFi) — your stated
  requirement — while staying **native in the app and easy** (no fiddly
  per-scan steps, ideally nothing to babysit on the phone).
- Later: a local vault that owns the barcodes, variants, and sold prices we
  collect, independent of any third-party service.

**Non-goals**
- Not a secrets manager for anyone but you (single-user, personal tool).
- Not military-grade crypto. The bar is "matches the platform's app-private
  guarantee and isn't shipped in plaintext to the world" — the same bar
  `settings.py` already sets.
- The app stays **stdlib-only** (Chaquopy/APK constraint). This rules out
  pulling in an AES library on the phone side, which shapes the transport
  choice in §5.

---

## 2. Architecture at a glance

```
        ┌──────────────────────── this desktop ────────────────────────┐
        │                                                               │
        │   keystore.json (0600)          spine_vault.db  (Phase 2)     │
        │        ▲                              ▲                        │
        │        │                              │                        │
        │   keystore.py  ── stdlib http.server, binds 127.0.0.1 ──┐     │
        │        ▲                                                  │     │
        └────────│──────────────────────────────────────────────────────┘
                 │  (exposed ONLY through the tunnel/VPN below, never
                 │   port-forwarded raw)
        ┌────────┴─────────┐
        │  tunnel / VPN    │   Cloudflare Tunnel  OR  Tailscale  (see §5)
        └────────┬─────────┘
                 │  HTTPS + Bearer <KEYSTORE_TOKEN>
        ┌────────┴─────────┐
        │   Spine app      │   GET /v1/keys  ->  settings.update(...)  ->
        │   (phone)        │   rebuild_clients()   +   cache in settings.json
        └──────────────────┘
```

The phone holds exactly **one** long-lived secret (the keystore token) and the
keystore URL. Everything else (ScanDex/eBay/PriceCharting keys) flows down on
demand and is cached locally, so the phone keeps working offline / away from
the desktop.

---

## 2b. Per-service auth reality (why "automatic" is easy for two of three)

"Automatic rotation" is only as possible as each service's auth model allows.
Investigated the ScanDex docs directly on 2026-08-25; here's the honest split:

| Service | What you store (durable) | Rotation | Notes |
|---|---|---|---|
| **eBay** | `client_id` + `client_secret` (app keys — these do **not** expire) | **Fully automatic already.** `EbayAuth` mints a ~2h access token via the client-credentials grant and refreshes it a minute before expiry. You store the two durable keys once; tokens self-refresh forever. | This is exactly the model you want, and it's built. |
| **PriceCharting** | static API token | None needed | Doesn't expire on a schedule; store once. |
| **ScanDex** | static account token from the dashboard | **No programmatic rotation exists** | Their v2 API (`/lookup`, `/create`) authenticates with one static `Authorization: <token>` header. **There is no login/OAuth/refresh endpoint.** The token comes from the developer dashboard only. |

**Why your ScanDex token "expired":** their changelog shows *"Added API v2"*
on **31 Oct 2025**. That migration almost certainly invalidated old v1 tokens.
This reads as a **one-time** event, not routine rotation — so for ScanDex the
"store once, only touch it if it breaks" model likely means touching it ~never.

**So the design principle is credential-based with per-service providers:** the
keystore stores durable credentials and each service has a small "provider"
that turns them into a currently-valid token. eBay's provider = the existing
client-credentials refresh; PriceCharting/ScanDex providers = pass the stored
token through. The app always asks the keystore for "the current valid token"
and never sees the refresh machinery.

**ScanDex, if it ever DOES rotate regularly** (no evidence it will): the only
way to fully automate is to store your ScanDex email+password on the local
server and have it drive a headless browser to log into the dashboard and
scrape a fresh token. Feasible (you're fine storing creds), but **brittle** —
it breaks whenever their web UI changes — and it adds a browser dependency on
the desktop. Recommendation: **do not build this speculatively.** Store the
token; if ScanDex proves to rotate often and re-pasting on the desktop gets
annoying, add the scraper then.

**Security note on storing passwords:** prefer durable *API credentials*
(eBay's client_id/secret, purpose-built for automation and independently
revocable) over raw account passwords wherever a service offers them. Store
passwords only for a service like ScanDex that offers no API-credential path,
and only if we build the scraper. Everything sits `0600` in app-private
storage, same bar as `settings.py`.

---

## 3. Phase 1 — Keystore broker

### 3.1 New desktop component: `keystore.py`
A standalone stdlib HTTP server (same `http.server` style as
`local_server.py`, but desktop-resident and separate from it). Reads/writes
`keystore.json`. It stores **durable credentials** (a superset of
`settings.FIELDS`) and serves currently-valid tokens:

```json
{ "ebay_client_id": "...", "ebay_client_secret": "...",
  "pricecharting_token": "...", "scandex_token": "..." }
```

Reuse `settings.py`'s allowlist + atomic-write + `0600` logic verbatim — the
keystore file is just a second `Settings` instance on the desktop. For eBay it
can either serve the durable `client_id`/`secret` (the app's local `EbayAuth`
then refreshes tokens, as it does today) or mint and serve the short-lived
access token itself; serving the durable pair is simpler and keeps the phone's
existing refresh path, so that's the default.

### 3.2 Endpoints
| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/v1/health` | none | `{ok:true, version}` |
| GET | `/v1/keys` | Bearer | current keys that are set (never masked — this IS the delivery channel) |
| PUT | `/v1/keys/<field>` | Bearer | set/rotate one key from anywhere (optional; you can also just edit the file / a tiny CLI) |

- Keys travel in the JSON **body / headers, never in the URL** (URLs get
  logged and cached).
- `GET /v1/keys` only returns fields that are actually set, so an unconfigured
  service doesn't overwrite a good local value with "".

### 3.3 Auth model
- A single long random **`KEYSTORE_TOKEN`** (e.g. 32 bytes base64). This is the
  bootstrap secret and the crown key: whoever holds it gets every service key,
  so it must be strong and only entered on trusted devices.
- Sent as `Authorization: Bearer <token>`. Constant-time compare
  (`hmac.compare_digest`). Requests without it get `401`, and the server
  never reveals whether a field exists to an unauthenticated caller.
- Rotating it is a deliberate, rare event (regenerate on desktop, re-enter on
  the phone once).

### 3.4 App-side changes (small — the plumbing already exists)
`settings.py` already has `update()` + `rebuild_clients()`, so the app work is
mostly wiring:
1. Two new **local** settings fields: `keystore_url`, `keystore_token`
   (entered once on the phone; these are NOT sent to the keystore, they're how
   the phone reaches it).
2. A **"Sync keys from server"** button in the settings panel:
   `GET {keystore_url}/v1/keys` with the bearer → `settings.update(payload)` →
   `rebuild_clients()`. Pasted-token UX we already have, now automated.
3. **Auto-sync on app start** (best-effort, short timeout): if `keystore_url`
   is set, try a sync; on any failure fall back silently to the cached keys in
   `settings.json`. This is what makes a rotated key "just work" the next time
   the phone has a path to the desktop, with zero shop-floor friction.

### 3.5 Failure modes (all degrade gracefully)
- Desktop off / tunnel down / offline → app uses cached keys. Scanning still
  works with whatever was last synced.
- Keystore token wrong → `401`, app keeps cached keys, shows one clear message.
- A service key itself expired *and* not yet updated on desktop → same failure
  as today, but now fixable in one place instead of per-device.

---

## 4. Phase 2 — Data vault (own what you collect)

Same desktop server (or a sibling), backed by a central **`spine_vault.db`**
(SQLite, stdlib) holding the assets that are yours regardless of any service:

| Data | Source today | Why vault it |
|---|---|---|
| Learned UPC index (barcode→title+variant+times_scanned) | `upc.py`, on-device | Every scan you confirm is permanent knowledge; centralize so all devices share it |
| ScanDex cache | `scandex_cache.json` | Keeps resolved barcodes if ScanDex goes away/charges |
| **Harvested sold prices** | `store.py` (already SQLite) | The real comp asset; this is also blocker #1 "wire a real comp source" |
| Catalog snapshot | `catalog_data.py` | Already static in-repo; mirror for completeness |

**Sync model (sketch, to be detailed if we proceed):**
- *Push:* when the app teaches a UPC or resolves via ScanDex, POST it to the
  vault so knowledge centralizes rather than stranding on one phone.
- *Pull:* app periodically pulls the merged UPC index + caches so every device
  benefits from every scan.
- *Harvest:* run `store.py` on the always-on desktop to accumulate sold data —
  this is the path to killing synthetic pricing without paying PriceCharting.
- *Conflicts:* UPC entries merge by max(`times_scanned`); caches are additive.

Phase 2 overlaps heavily with the top project priority (real comp source), so
it's deliberately staged after Phase 1 rather than bundled in.

### 4a. Phase 2 — first slice implemented (barcode index vault)

Built 2026-08-25, no eBay dependency. Files: `vault.py` (desktop-only stdlib
SQLite `spine_vault.db`, `merge_upc`/`all_upc`/`stats`), `keystore.py` (adds
`POST`/`GET /v1/vault/upc` on the same server + bearer token), `upc.py`
(`merge_two`, `all_entries`, `merge_entries`), `local_server.py`
(`sync_vault()` + `POST /api/vault/sync` + auto-run on launch after the key
sync), `static/index.html` ("Back up scans now"), `test_vault.py` (11 checks).

The merge rule (`upc.merge_two`, shared by server and phone) is the crux: a
user-confirmed variant is never downgraded, `times_scanned` is max'd not
summed, earliest `first_seen` wins — so push/pull is order-independent and safe
to run automatically. `vault.py` is desktop-only (not in `sync_android.sh`
MODULES); the phone talks to it over HTTP, so no sqlite ships in the APK.

Verified end-to-end: phone A confirms a spine → backs up → phone B (a reset
device) restores it with the variant intact; an unreachable vault leaves the
local index untouched.

Not yet done in Phase 2: the ScanDex cache and catalog snapshot (easy, same
pattern), and the store.py sold-price harvest (needs eBay keys — the real
payoff, deferred with the eBay work).

---

## 5. The reachability decision (your call — see "native + easy" tension)

You chose "reachable anywhere," conditioned on **native in the app and easy**.
There's a real tradeoff between those two words:

| Option | Native in app? | Easy / free / stable | Privacy | Notes |
|---|---|---|---|---|
| **Cloudflare Named Tunnel** | ✅ phone just fetches an HTTPS URL — nothing to install on the phone | ⚠️ one-time `cloudflared` setup on desktop; a **stable** hostname needs a domain you've added to Cloudflare (~$10/yr). Free `trycloudflare` URLs work but change each run | ⚠️ Cloudflare terminates TLS at its edge, so in principle it could see the key payload | Best fit for "native in the app" |
| **Tailscale** | ❌ needs the Tailscale app + login on the phone | ✅ free personal tier, stable MagicDNS name, no domain, no port-forwarding | ✅ WireGuard P2P — no third party sees plaintext | Best fit for "easy + private," worst for "native" |
| LAN-only + cache | ✅ | ✅ | ✅ | You ruled this out, but noting: because keys are cached and don't rotate mid-shop, syncing only at home may actually be enough |

**Recommendation given your priority (native in the app):** Cloudflare
**Named** Tunnel. The phone side is literally just `keystore_url` +
`keystore_token` in settings and a stdlib HTTPS GET — the most "native"
possible. The cost is the desktop-side `cloudflared` setup and, for a stable
URL, owning a domain.

**The privacy caveat is real and worth a decision:** because the app must stay
stdlib-only (no AES on the phone), we can't easily encrypt the key payload
*above* TLS. So with Cloudflare, you're trusting Cloudflare's edge with the
keys in transit. Two ways to close that:
- Put **Cloudflare Access** in front of the tunnel (adds an auth layer; still
  edge-terminated), or
- Use **Tailscale** instead (no third-party plaintext), accepting the phone
  needs the Tailscale app — which nicks the "native" goal.

My honest read: for a personal tool holding *your own* marketplace keys, the
Cloudflare edge exposure is a low, acceptable risk, and Named Tunnel keeps the
app native. But if the edge-sees-plaintext point bothers you, Tailscale is the
cleaner security story at the cost of one app install on the phone.

**→ This is the main thing I need you to pick before Phase 1 is buildable.**

---

## 6. Security summary

- One bootstrap secret (`KEYSTORE_TOKEN`); strong, entered on trusted devices
  only; rotatable.
- Keystore binds `127.0.0.1`; exposed *only* via the chosen tunnel/VPN. Never
  raw port-forwarding.
- Keys in headers/body, never URLs; never logged; `hmac.compare_digest` on the
  token.
- App-private storage + `0600` on both `keystore.json` and `settings.json`
  (already how `settings.py` behaves).
- Threat accepted: physical access to an unlocked, rooted device can read
  cached keys — same as today, and the same bar the platform sets.

---

## 7. What I still need from you

**Resolved:** automatic rotation (§2b). It's already how eBay works — store the
durable app keys once, tokens self-refresh — and the keystore extends that to
"the phone fetches current creds itself." ScanDex is static-token-only with no
refresh API, but its "expiry" was a one-time v2 migration, so store-once is
almost certainly enough.

**Decided (2026-08-25):** the desktop keystore **auto-starts** — via a Windows
Task Scheduler task triggered "at log on" (built-in, no third-party service
wrapper). So the phone can reach it after any reboot without the user doing
anything.

Still open:

1. **Reachability (§5), with cost:** the only cost anywhere is the Cloudflare
   path's domain (~$10/yr) for a stable hostname; `cloudflared`/account/Tunnel/
   Access are free, but the free no-domain "quick tunnel" gives an unstable URL
   that changes on each reboot — which conflicts with autostart. **Tailscale is
   $0 and stable** but needs its app installed once on the phone. Pick: Tailscale
   (free, private, phone app) vs Cloudflare named tunnel (~$10/yr, phone stays
   URL-only).
2. **ScanDex account:** you'll need to sign up and generate the token yourself
   (I can't create accounts). Once you have a fresh token, paste it into the
   app to unbreak scanning today; the keystore then makes that the last manual
   paste.
3. **ScanDex scraper — skip for now?** Recommended: yes, skip. Build it only if
   ScanDex turns out to rotate tokens regularly (no evidence it does).

## 8. Suggested build order (once §7 is settled)
1. `keystore.py` + `keystore.json` on the desktop (reuses `settings.py` logic).
2. Stand up the chosen tunnel/VPN.
3. App: `keystore_url`/`keystore_token` fields + "Sync keys" button +
   best-effort auto-sync on start; fall back to cache.
4. Test the whole loop: rotate the ScanDex token on the desktop → phone
   auto-syncs → scanning works with no re-paste.
5. (Later) Phase 2 vault, starting with the `store.py` harvest that also
   attacks blocker #1.

---

## 9. Phase 1 — implemented (setup steps)

**What shipped (root files):** `keystore.py` (desktop server + CLI),
`settings.py` (+`keystore_url`/`keystore_token` fields, `KEYSTORE_SERVED_FIELDS`),
`local_server.py` (`sync_from_keystore()`, `POST /api/keystore/sync`, best-effort
auto-sync on launch), `static/index.html` (Key-server panel + "Sync now"),
`test_keystore.py` (14 checks). `keystore.py` is **desktop-only** — deliberately
not in `sync_android.sh`'s MODULES, so it is never bundled into the APK.

**One-time setup:**

1. **Tailscale** on the desktop and the phone (same tailnet). Note the
   desktop's MagicDNS name, e.g. `desk.tailXXXX.ts.net`.
2. **Keystore on the desktop:**
   ```
   cd spine-app/ps2arb
   python keystore.py init                 # prints the bearer token — copy it
   python keystore.py set ebay_client_id     <id>
   python keystore.py set ebay_client_secret <secret>
   python keystore.py set scandex_token      <token>
   python keystore.py serve                 # runs on 0.0.0.0:8787
   ```
3. **Auto-start at logon (no admin needed) — a Startup-folder shortcut.** Task
   Scheduler works but needs elevation; a shortcut in the user's Startup folder
   does not, and `pythonw.exe` runs the server with no console window:
   ```
   $py = "C:\Python314\pythonw.exe"
   $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) "SpineKeystore.lnk"
   $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
   $sc.TargetPath = $py
   $sc.Arguments = "keystore.py serve"
   $sc.WorkingDirectory = "C:\Users\<you>\spine\spine-app\ps2arb"
   $sc.WindowStyle = 7
   $sc.Save()
   ```
   (Over Tailscale the transport is encrypted, so binding 0.0.0.0 is fine —
   nothing answers without the bearer token anyway. If the phone can't reach
   the desktop, allow inbound TCP 8787 on the Tailscale interface in Windows
   Firewall.)
4. **Phone, once:** in the app's "Barcode lookup & API keys" panel, under
   **Key server**, set
   - Keystore URL = `http://desk.tailXXXX.ts.net:8787`
   - Keystore token = the token from `keystore init`
   then tap **Save server** → **Sync now**. The app also auto-syncs on launch.

**From then on:** rotate any key once on the desktop
(`python keystore.py set <field> <value>`, or `PUT /v1/keys/<field>`); the phone
picks it up on its next launch/sync. eBay's ~2h access token was always
self-refreshing; you now never paste a service key on the phone again. If the
phone is offline/away, it keeps using the last-synced keys.
