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
    "soldcomps_token":    "SOLDCOMPS_TOKEN",
    # Desktop-only cover-art seeding (seed_covers.py); the phone never uses it,
    # but it is a keystore field so `keystore.py set` works uniformly for it.
    "thegamesdb_token":   "THEGAMESDB_TOKEN",
    # Photo identification (a scanned cover/spine -> title). vision_api_key is
    # a secret; provider/model/base_url are plain config with defaults in
    # identify.py. anthropic_api_key stays as a back-compat fallback key.
    "vision_provider":    "VISION_PROVIDER",
    "vision_api_key":     "VISION_API_KEY",
    "vision_model":       "VISION_MODEL",
    "vision_base_url":    "VISION_BASE_URL",
    "anthropic_api_key":  "ANTHROPIC_API_KEY",
    # Offline cover matching (phash_index.py). Tuning knobs rather than
    # credentials, but keystore-served for the same reason vision_model is:
    # if the thresholds turn out wrong in the field they can be corrected from
    # the desktop instead of waiting on an APK rebuild. Raising the cutoff
    # finds more covers; raising the margin makes it abstain more readily.
    "phash_cutoff":       "PS2ARB_PHASH_CUTOFF",
    "phash_margin":       "PS2ARB_PHASH_MARGIN",
    # How the phone reaches the desktop keystore. These are LOCAL config, not
    # service credentials: the keystore never stores or serves them — they are
    # how you get to it. keystore_url is e.g. http://desk.tailXXXX.ts.net:8787
    "keystore_url":       "SPINE_KEYSTORE_URL",
    "keystore_token":     "SPINE_KEYSTORE_TOKEN",
}

# Never echoed back to the client in full.
SECRET_FIELDS = {"scandex_token", "ebay_client_secret", "pricecharting_token",
                 "soldcomps_token", "thegamesdb_token", "vision_api_key",
                 "anthropic_api_key", "keystore_token"}

# The durable service credentials/config the desktop keystore stores and
# serves. A sync writes only these back into settings, so it can never clobber
# the keystore_url / keystore_token the phone needs to reach the keystore.
KEYSTORE_SERVED_FIELDS = {
    "scandex_token", "ebay_client_id", "ebay_client_secret",
    "pricecharting_token", "soldcomps_token", "thegamesdb_token",
    "vision_provider", "vision_api_key", "vision_model", "vision_base_url",
    "anthropic_api_key", "phash_cutoff", "phash_margin",
}

# The desktop keystore writes its OWN store (keystore.json); the desktop's
# other tools -- precompute, seed_covers, service (via sources.build_source) --
# read the settings store here. Those are different files, so a token set with
# `keystore.py set` would otherwise be invisible to them. resolve() bridges the
# gap. On the phone there is no keystore.json (settings.json is the synced
# copy), so this degrades to the ordinary lookup there.
KEYSTORE_STORE_PATH = Path(os.environ.get(
    "SPINE_KEYSTORE_STORE", Path(__file__).parent / "keystore.json"))


def resolve(field: str) -> str:
    """A credential from the local settings store, else the desktop keystore
    store, else the environment. Never raises; returns '' when unset.

    So `keystore.py set soldcomps_token ...` (or thegamesdb_token) is picked up
    by the desktop tools even though those tools read a different store than the
    keystore writes.
    """
    primary = Settings()
    value = primary.get(field)                # settings.json data, else env
    if value:
        return value
    try:
        if (field in FIELDS and KEYSTORE_STORE_PATH.exists()
                and KEYSTORE_STORE_PATH != primary.path):
            return Settings(KEYSTORE_STORE_PATH).get(field)
    except Exception:
        pass
    return ""


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
