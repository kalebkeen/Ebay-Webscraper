"""
settings.py — credentials entered on the device, not baked into the APK.

Until this existed, every API key had to be an environment variable set
before launch, which on a phone means editing source and rebuilding. So the
barcode resolvers were wired but permanently off, and every scan fell
through to typing the title by hand.

Stored as JSON in the app's private data directory. On Android that is
`/data/data/<package>/files`, which other apps cannot read. This is not
encryption — anyone with physical access and a rooted phone can read it —
but for a personal tool holding your own marketplace keys, matching the
platform's own app-private guarantee is the right level.

Values fall back to environment variables when unset, so desktop use and
CI keep working unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STORE_PATH = Path(os.environ.get(
    "SPINE_SETTINGS", Path(__file__).parent / "settings.json"))

# Only these keys are accepted. An allowlist rather than a free-form dict so
# a malformed client cannot fill the file with arbitrary data.
FIELDS = {
    "scandex_token":      "SCANDEX_TOKEN",
    "ebay_client_id":     "EBAY_CLIENT_ID",
    "ebay_client_secret": "EBAY_CLIENT_SECRET",
    "pricecharting_token": "PRICECHARTING_TOKEN",
}

# Never echoed back to the client in full.
SECRET_FIELDS = {"scandex_token", "ebay_client_secret", "pricecharting_token"}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or STORE_PATH)
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self._data = {k: v for k, v in raw.items() if k in FIELDS}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        """Atomic write, then lock down the permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, field: str) -> str:
        """Stored value, else the environment, else empty."""
        if field not in FIELDS:
            return ""
        value = self._data.get(field, "")
        if value:
            return value
        return os.environ.get(FIELDS[field], "")

    def set(self, field: str, value: str) -> bool:
        if field not in FIELDS:
            return False
        value = (value or "").strip()
        if value:
            self._data[field] = value
        else:
            self._data.pop(field, None)
        self.save()
        # Export so modules that read os.environ at construction pick it up
        # without a restart -- otherwise saving a token appears to do nothing
        # until the app is killed, which reads as a bug.
        if value:
            os.environ[FIELDS[field]] = value
        else:
            os.environ.pop(FIELDS[field], None)
        return True

    def update(self, payload: dict) -> list[str]:
        changed = []
        for field, value in (payload or {}).items():
            if field in FIELDS and self.set(field, value):
                changed.append(field)
        return changed

    def masked(self) -> dict:
        """Safe to send to the client: presence and a hint, never the value."""
        out = {}
        for field in FIELDS:
            value = self.get(field)
            if not value:
                out[field] = {"set": False, "hint": ""}
            elif field in SECRET_FIELDS:
                tail = value[-4:] if len(value) > 4 else ""
                out[field] = {"set": True, "hint": f"…{tail}"}
            else:
                out[field] = {"set": True, "hint": value}
        return out

    @property
    def scandex_ready(self) -> bool:
        return bool(self.get("scandex_token"))

    @property
    def ebay_ready(self) -> bool:
        return bool(self.get("ebay_client_id") and self.get("ebay_client_secret"))
