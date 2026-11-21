"""GUI settings, remembered between sessions in the user's profile (never in the repo or the photo folder).
The API key is not stored here: it goes in the system credential store (see api.save_api_key)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

from api import VERIFIED_ROVERS


@dataclass
class Settings:
    folder: str = ""
    rovers: list[str] = field(default_factory=lambda: list(VERIFIED_ROVERS))
    # Cameras the user unticked, per rover. Storing exclusions (not inclusions) means cameras Mars Vista adds
    # later are included by default.
    excluded_cameras: dict[str, list[str]] = field(default_factory=dict)
    known_cameras: dict[str, list[str]] = field(default_factory=dict)   # cached from the API, for offline display
    check_existing: bool = True
    sol_from: str = ""
    sol_to: str = ""
    workers: int = 6
    confirmed_folders: list[str] = field(default_factory=list)          # folders the user OK'd adding to

    @classmethod
    def load(cls, path: str) -> "Settings":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in names})

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=1)
        os.replace(tmp, path)
