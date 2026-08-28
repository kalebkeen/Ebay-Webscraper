#!/usr/bin/env bash
# Copy the pipeline into the APK bundle and verify nothing drifted.
#
# The Python lives in two places: the working copy at the repo root, and
# android/app/src/main/python/ which Chaquopy packages into the APK. Editing
# one and building the other ships stale logic that passes every test on the
# desktop -- which is exactly the failure that is impossible to notice.
# Run this before every build.
set -euo pipefail
cd "$(dirname "$0")"
DEST="android/app/src/main/python"

# Runtime modules only. The test suites, backtest harness, timeline
# simulator and FastAPI service are desktop-side and would only bloat the APK.
# core.py and local_server.py live at the root too, because local_server.py
# imports core and both are useful to run on a desktop. They drifted once
# already -- a fix landed in the bundled copy only -- so they are synced
# like everything else rather than trusted to stay aligned by hand.
MODULES=(catalog.py catalog_data.py comps.py core.py decide.py ebay.py
         economics.py fuzzy.py httpjson.py identify.py listing_parser.py
         local_server.py mock_sources.py outcomes.py photo_outbox.py
         pipeline.py pricecache.py pricecharting.py scandex.py sequel.py
         settings.py soldcomps.py sources.py store.py upc.py)

mkdir -p "$DEST/static"
for f in "${MODULES[@]}"; do
  cp "$f" "$DEST/$f"
done
for f in index.html sw.js manifest.json icon.svg; do
  [ -f "static/$f" ] && cp "static/$f" "$DEST/static/$f"
done

echo "synced ${#MODULES[@]} modules + web assets -> $DEST"

# android_main.py is device-only and lives solely in DEST, so it is never
# overwritten.
echo
echo "verifying the bundle imports standalone..."
( cd "$DEST" && python3 -c "
import android_main, core, local_server, decide, comps, economics, store, ebay
print('  all device modules import cleanly')
" )

echo
echo "checking for non-stdlib imports (each one is a wheel that must exist for Android)..."
( cd "$DEST" && python3 - <<'PY'
import ast, pathlib, sys
std = set(sys.stdlib_module_names)
local = {p.stem for p in pathlib.Path('.').glob('*.py')}
bad = {}
for f in sorted(pathlib.Path('.').glob('*.py')):
    for node in ast.walk(ast.parse(f.read_text())):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module.split('.')[0]]
        for m in mods:
            if m not in std and m not in local and m != 'com':
                bad.setdefault(m, set()).add(f.name)
if bad:
    print('  NON-STDLIB IMPORTS FOUND:', bad)
    print('  Add a pip{} block in app/build.gradle.kts or remove them.')
    sys.exit(1)
print('  stdlib only — no pip block needed')
PY
)
