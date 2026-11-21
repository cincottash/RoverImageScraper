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


# ---- where the app keeps its own files (settings, scan results, request counts, refused photos) -------------
# The user picks this folder on first run. The choice itself is remembered in the Windows registry under the
# current user (HKCU\Software\RoverImageScraper, value DataFolder), since the app must know it before it can
# open any file. On other systems the standard folder is used.

APP_DATA_FILES = ("settings.json", "scans.json", "requests.json", "unavailable.json")
_REG_KEY, _REG_VALUE = r"Software\RoverImageScraper", "DataFolder"


def standard_state_dir() -> str:
    """The suggested folder: %LOCALAPPDATA%\RoverImageScraper (~/.local/share/RoverImageScraper elsewhere)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "RoverImageScraper")


def chosen_state_dir() -> str | None:
    """The folder the user chose at first-run setup, or None if setup hasn't been done."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            value = winreg.QueryValueEx(k, _REG_VALUE)[0]
        return value or None
    except OSError:
        return None


def save_state_dir(path: str) -> None:
    """Remember the user's choice of app data folder (Windows registry, current user only)."""
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
        winreg.SetValueEx(k, _REG_VALUE, 0, winreg.REG_SZ, path)


def default_state_dir() -> str:
    """The app data folder in use: the user's choice if made, else the standard folder."""
    return chosen_state_dir() or standard_state_dir()


def copy_app_data(src: str, dst: str) -> list[str]:
    """Copy the app's data files from src to dst. Never overwrites a file already in dst and never deletes
    anything in src. Returns the names copied."""
    copied = []
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return copied
    os.makedirs(dst, exist_ok=True)
    for name in APP_DATA_FILES:
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isfile(s) and not os.path.exists(d):
            with open(s, "rb") as fin, open(d + ".part", "wb") as fout:
                fout.write(fin.read())
            os.replace(d + ".part", d)
            copied.append(name)
    return copied


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
