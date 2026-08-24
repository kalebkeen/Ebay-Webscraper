# Building the APK

## Prerequisites

- **Android Studio** (Ladybug or newer). It supplies the SDK, the NDK, and a JDK.
- A device with **USB debugging** on, or an emulator.

Nothing else. The Python side is stdlib-only, so there are no wheels to
source and no `pip` block in the Gradle config — that is why numpy and
rapidfuzz were removed.

## Before every build

```bash
./sync_android.sh
```

The pipeline exists twice: the working copy at the repo root, and
`android/app/src/main/python/` which Chaquopy packages. Editing one and
building the other ships stale logic that passes every desktop test. The
script copies the runtime modules across, checks the bundle imports on its
own, and fails if any non-stdlib import has crept in.

## Build

```
Android Studio -> Open -> select the `android/` folder
Wait for Gradle sync (first run downloads Chaquopy and unpacks CPython)
Run -> app
```

**Use Android Studio for the first build.** The Gradle wrapper (`gradlew`
and `gradle-wrapper.jar`) is generated on first sync and is not checked in —
`gradle/wrapper/gradle-wrapper.properties` pins which Gradle version gets
fetched, but the wrapper scripts themselves appear only after Studio has
opened the project once.

After that first sync the command line works:

```bash
cd android
./gradlew assembleDebug     # app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug      # straight onto a connected device
```

## What happens at launch

1. `MainActivity` starts Chaquopy on a background thread. First run unpacks
   the interpreter and stdlib, which takes a second or two — hence the
   "Starting Python…" screen rather than a frozen window.
2. `android_main.start()` points the UPC map, harvest database and eBay
   token cache at app-private storage, then starts a stdlib HTTP server on
   a random loopback port.
3. The WebView loads `http://127.0.0.1:<port>/` and the existing client runs
   against it unchanged.

Cleartext is permitted to `127.0.0.1` only; everything outbound still
requires TLS. See `res/xml/network_security_config.xml`.

## Barcode scanning

The client prefers `window.SpineNative`, backed by Play Services' code
scanner. It falls back to `BarcodeDetector` when running in a browser.

The fallback is not enough on its own: `BarcodeDetector` is a Chrome API and
is not reliably exposed inside Android's WebView, so without the native
bridge the scan button can report "this browser has no barcode reader" from
inside your own app.

## Live prices

The app starts on **mock data** and the client shows a standing banner
saying so. To go live:

```bash
export EBAY_CLIENT_ID=...
export EBAY_CLIENT_SECRET=...
```

On device, set these in `android_main.py` or read them from a settings
screen. `_build_source()` switches to the harvest store once it holds 20+
observed sales — before that it stays on mock rather than returning empty
results that would read as "no comps" instead of "not ready yet".

Harvesting needs weeks of polling. `python store.py --stats` reports
progress. If you can get PriceCharting API access, use it for comps and let
the harvest run alongside as validation.

## Size and ABIs

`abiFilters` is set to `arm64-v8a` and `armeabi-v7a`. Each ABI bundles its
own CPython, so adding `x86_64` for the emulator roughly increases the
Python payload by half again. Drop `armeabi-v7a` if you only target a modern
phone.

## Release builds

`isMinifyEnabled = false` deliberately. R8 cannot see through Chaquopy's
reflection into Python modules, and enabling it strips classes that are only
referenced from Python. If you turn it on, keep rules for
`com.chaquo.python.**`.

You will also need a signing config; Android Studio's
**Build → Generate Signed Bundle / APK** handles this.
