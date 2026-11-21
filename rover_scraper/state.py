"""Photos NASA refuses to serve (HTTP 403/404/410), remembered between runs so a folder that can never be
completed isn't re-listed and re-downloaded on every run. Stored outside the photo folder, so the dump holds
only photos."""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import Counter

PERMANENT_STATUSES = {403, 404, 410}


def default_state_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "RoverImageScraper")


class Unavailable:
    def __init__(self, path: str | None, ignore_existing: bool = False):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path and os.path.exists(path) and not ignore_existing:
            with open(path, encoding="utf-8") as f:
                self.entries = json.load(f)
        self._dirty = False

    def __contains__(self, photo_id: int) -> bool:
        return str(photo_id) in self.entries

    def add(self, photo, status: int) -> None:
        self.entries[str(photo.id)] = {"rover": photo.rover, "sol": photo.sol, "camera": photo.camera,
                                       "url": photo.url, "status": status,
                                       "when": time.strftime("%Y-%m-%d")}
        self._dirty = True

    def discard(self, photo_id: int) -> None:
        if self.entries.pop(str(photo_id), None) is not None:
            self._dirty = True

    def per_camera(self, rover: str, sol: int) -> Counter:
        return Counter(e["camera"] for e in self.entries.values() if e["rover"] == rover and e["sol"] == sol)

    def save(self) -> None:
        if not (self.path and self._dirty):
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=1)
        os.replace(tmp, self.path)
        self._dirty = False
