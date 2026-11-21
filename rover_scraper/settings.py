"""GUI settings, remembered between sessions in the user's profile (never in the repo or the photo folder).
The API key is not stored here: it goes in the system credential store (see api.save_api_key)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

from api import SUPPORTED_ROVERS


@dataclass
class Settings:
    folder: str = ""
    rovers: list[str] = field(default_factory=lambda: list(SUPPORTED_ROVERS))
    # Cameras the user unticked, per rover. Storing exclusions (not inclusions) means cameras Mars Vista adds
    # later are included by default.
    excluded_cameras: dict[str, list[str]] = field(default_factory=dict)
    known_cameras: dict[str, list[str]] = field(default_factory=dict)   # cached from the API, for offline display
    check_existing: bool = True
    sol_from: str = ""
    sol_to: str = ""
    workers: int = 6
    limit_on: bool = False               # the user's own download limit (Options > Advanced)
    limit_files: int = 500
    limit_period: str = "hour"           # minute | hour | day
    confirmed_folders: list[str] = field(default_factory=list)          # folders the user OK'd adding to
    # View menu
    data_unit: str = "auto"              # auto | KB | MB | GB | TB
    speed_unit: str = "auto"             # auto | KB/s | MB/s | Mbit/s
    speed_window: int = 10               # seconds the speed is averaged over
    theme: str = "dark"                  # "system" (match the OS) or a theme key from gui.THEMES
    show_log: bool = True
    collapsed_cards: list[str] = field(default_factory=list)   # titles of cards folded to their title bar
    verbose_log: bool = False            # View > Detailed activity log

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
