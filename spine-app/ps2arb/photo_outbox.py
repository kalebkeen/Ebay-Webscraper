"""
photo_outbox.py — a durable on-phone queue for confirmed photos that haven't
reached the desktop vault yet.

A confirmed cover photo is training data we don't want to lose, but the phone
is usually offline while you're out scanning. So a confirmed photo is written
HERE first and pushed to the vault in the background; whatever can't send now
stays queued and is retried on the next sync (app launch, the manual "back up"
button, or the next capture). The barcode index has the same property already
— it's saved to disk on confirm and merged up on sync — so this fills the one
gap: photos, which previously went straight to the vault or were lost.

Stdlib only, so it ships inside the APK. One JSON file per pending item, so a
crash mid-write can lose at most the single item being written, never the
whole queue.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class PhotoOutbox:
    """A directory of pending {image, title, variant, barcode} records."""

    def __init__(self, directory) -> None:
        self.dir = Path(directory)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def enqueue(self, item: dict) -> str:
        """Persist one pending photo durably. Returns its id ("" on failure).
        Never raises — a queue write must not break a confirm."""
        pid = uuid.uuid4().hex
        rec = dict(item)
        rec["id"] = pid
        rec.setdefault("queued_at", time.time())
        try:
            tmp = self.dir / f".{pid}.tmp"
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            tmp.replace(self.dir / f"{pid}.json")   # atomic swap into place
        except OSError:
            return ""
        return pid

    def pending(self) -> list[dict]:
        """Every queued item, oldest first. Skips anything unreadable."""
        out = []
        try:
            files = list(self.dir.glob("*.json"))
        except OSError:
            return out
        for f in files:
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        out.sort(key=lambda r: r.get("queued_at", 0))
        return out

    def remove(self, pid: str) -> None:
        """Drop one item once it's safely in the vault. Never raises."""
        if not pid:
            return
        try:
            (self.dir / f"{pid}.json").unlink()
        except OSError:
            pass

    def count(self) -> int:
        try:
            return sum(1 for _ in self.dir.glob("*.json"))
        except OSError:
            return 0
