"""Photos NASA refuses to serve (HTTP 403/404/410), remembered between runs so a folder that can never be
completed isn't re-listed and re-downloaded on every run. Stored outside the photo folder, so the dump holds
only photos."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import time
from collections import Counter

PERMANENT_STATUSES = {403, 404, 410}


# ---- where the app keeps its own files (settings, scan results, request counts, refused photos) -------------
# The user picks this folder on first run. The choice itself has to be stored somewhere the app can find before
# it opens any file: on Windows the registry under the current user (HKCU\Software\RoverImageScraper, value
# DataFolder); on Linux and macOS a one-line file, ~/.config/RoverImageScraper/data_folder.

APP_DATA_FILES = ("settings.json", "scans.json", "requests.json", "unavailable.json", "photo_details.db",
                  "favorites.json")
_REG_KEY, _REG_VALUE = r"Software\RoverImageScraper", "DataFolder"


def standard_state_dir() -> str:
    """The suggested folder: %LOCALAPPDATA%\\RoverImageScraper on Windows, ~/.local/share/RoverImageScraper
    elsewhere (or $XDG_DATA_HOME/RoverImageScraper)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "RoverImageScraper")


def _choice_file() -> str:
    """Linux/macOS: where the chosen data folder is written ($XDG_CONFIG_HOME or ~/.config)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "RoverImageScraper", "data_folder")


def chosen_state_dir() -> str | None:
    """The folder the user chose at first-run setup, or None if setup hasn't been done."""
    if os.name != "nt":
        try:
            with open(_choice_file(), encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            value = winreg.QueryValueEx(k, _REG_VALUE)[0]
        return value or None
    except OSError:
        return None


def save_state_dir(path: str) -> None:
    """Remember the user's choice of app data folder (Windows registry for the current user; elsewhere a one-line
    file in ~/.config/RoverImageScraper)."""
    if os.name != "nt":
        target = _choice_file()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".part", "w", encoding="utf-8") as f:
            f.write(path + "\n")
        os.replace(target + ".part", target)
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
        winreg.SetValueEx(k, _REG_VALUE, 0, winreg.REG_SZ, path)


# Saved instead of a folder when the user chose not to keep app data between launches.
NO_DATA_FOLDER = "<none>"
_session_dir: str | None = None


def keeps_app_data() -> bool:
    """False if the user chose to start fresh every launch (nothing kept between runs)."""
    return chosen_state_dir() != NO_DATA_FOLDER


def session_state_dir() -> str:
    """A temporary app data folder for this run only, deleted when the program exits."""
    global _session_dir
    if _session_dir is None:
        _session_dir = tempfile.mkdtemp(prefix="RoverImageScraper-session-")
        atexit.register(shutil.rmtree, _session_dir, True)
    return _session_dir


def default_state_dir() -> str:
    """The app data folder in use: the user's choice if made (a temporary one for this run if they chose not to
    keep app data), else the standard folder."""
    choice = chosen_state_dir()
    if choice == NO_DATA_FOLDER:
        return session_state_dir()
    return choice or standard_state_dir()


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
                shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)       # in pieces: photo details can be large
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
