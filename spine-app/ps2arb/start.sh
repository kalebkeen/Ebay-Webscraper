#!/usr/bin/env bash
# One command. Sets up, starts the server, opens a public HTTPS URL.
#   ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

# Files download flat from chat; the four web assets must live in static/.
mkdir -p static
for f in index.html sw.js manifest.json icon.svg; do
  [ -f "$f" ] && mv "$f" static/ && echo "moved $f -> static/"
done
for f in index.html sw.js manifest.json; do
  [ -f "static/$f" ] || { echo "MISSING: static/$f"; exit 1; }
done

[ -d .venv ] || { echo "creating venv..."; python3 -m venv .venv; }
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

uvicorn api:app --host 0.0.0.0 --port 8000 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 4

echo
echo "  Local:  http://localhost:8000"
if command -v cloudflared >/dev/null 2>&1; then
  echo "  Opening a public HTTPS URL. Look for trycloudflare.com below,"
  echo "  open it on your phone, then Chrome menu > Add to Home Screen."
  echo
  cloudflared tunnel --url http://localhost:8000
else
  echo
  echo "  For phone access with a working camera, install cloudflared:"
  echo "    macOS:  brew install cloudflared"
  echo "    Linux:  https://github.com/cloudflare/cloudflared/releases"
  echo "  Then run this script again."
  wait $SERVER
fi
