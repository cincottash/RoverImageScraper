"""Library status: what's in the photo folder compared with what Mars Vista has. Reads the folder only.

Quick scan: counts sols, photos and bytes on disk, plus one manifest request per rover (new sols available).
Full check: a quick scan plus a dry run over the sols already on disk, which finds gaps (the same per-camera count
check a download does, so it uses roughly one API request per existing sol).
Results are saved outside the photo folder, so the GUI can show them next time it opens.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from api import Cancelled, MarsVistaClient
from scraper import Report, Scraper
from state import Unavailable


@dataclass
class RoverStatus:
    sols_on_disk: int | None = None
    first_sol: int | None = None
    last_sol: int | None = None
    photos: int | None = None
    bytes: int | None = None
    latest_sol: int | None = None        # newest sol Mars Vista has photos for
    new_sols: int | None = None          # sols after last_sol that have photos
    new_photos: int | None = None
    gap_sols: int | None = None          # None = not checked (Full check not run since the last quick scan)
    gap_photos: int | None = None        # upper bound
    scanned_at: float | None = None
    full_checked_at: float | None = None


@dataclass
class FolderStatus:
    rovers: dict[str, RoverStatus] = field(default_factory=dict)
    outdated: bool = False               # a download ran after the last scan


def disk_summary(dest: str, rover: str, progress: Callable[[dict], None] | None = None,
                 cancel: threading.Event | None = None) -> RoverStatus:
    """Count sol folders, numbered photo files and their total size for one rover."""
    base = os.path.join(dest, rover)
    try:
        sol_names = sorted((e.name for e in os.scandir(base) if e.is_dir() and e.name.isdigit()), key=int)
    except FileNotFoundError:
        sol_names = []
    photos = size = 0
    for i, sol in enumerate(sol_names, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        for cam in os.scandir(os.path.join(base, sol)):
            if not cam.is_dir():
                continue
            for f in os.scandir(cam.path):
                if f.name.isdigit() and f.is_file():
                    photos += 1
                    size += f.stat().st_size
        if progress and (i % 50 == 0 or i == len(sol_names)):
            progress({"event": "scan", "rover": rover, "index": i, "total": len(sol_names)})
    sols = [int(s) for s in sol_names]
    return RoverStatus(sols_on_disk=len(sols), first_sol=sols[0] if sols else None,
                       last_sol=sols[-1] if sols else None, photos=photos, bytes=size)


def quick_scan(client: MarsVistaClient, dest: str, rover: str,
               progress: Callable[[dict], None] | None = None,
               cancel: threading.Event | None = None) -> RoverStatus:
    status = disk_summary(dest, rover, progress, cancel)
    manifest = client.manifest(rover)
    last = status.last_sol if status.last_sol is not None else -1
    newer = {s: v for s, v in manifest.items() if s > last}
    status.latest_sol = max(manifest) if manifest else None
    status.new_sols = len(newer)
    counts = [v.total_photos for v in newer.values()]
    status.new_photos = None if None in counts else sum(counts)     # unknown for NASA PDS rovers
    status.scanned_at = time.time()
    return status


def full_check(client: MarsVistaClient, dest: str, rover: str, unavailable: Unavailable,
               log: Callable[[str], None] = print, progress: Callable[[dict], None] | None = None,
               cancel: threading.Event | None = None) -> RoverStatus:
    status = quick_scan(client, dest, rover, progress, cancel)
    if status.last_sol is None:
        status.gap_sols, status.gap_photos = 0, 0
    else:
        scraper = Scraper(client, dest, dry_run=True, log=log, cancel=cancel, unavailable=unavailable,
                          progress=progress)
        report = scraper.run(rover, 0, status.last_sol, Report())
        if report.cancelled:
            raise Cancelled()
        status.gap_sols, status.gap_photos = report.sols_with_work, report.would_download
    status.full_checked_at = time.time()
    return status


class ScanStore:
    """Saved scan results, keyed by photo folder."""

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, dict] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {}

    @staticmethod
    def _key(dest: str) -> str:
        return os.path.normcase(os.path.abspath(dest))

    def get(self, dest: str) -> FolderStatus:
        raw = self._data.get(self._key(dest))
        if not raw:
            return FolderStatus()
        return FolderStatus(rovers={r: RoverStatus(**v) for r, v in raw.get("rovers", {}).items()},
                            outdated=bool(raw.get("outdated")))

    def put_rover(self, dest: str, rover: str, status: RoverStatus) -> None:
        entry = self._data.setdefault(self._key(dest), {"rovers": {}, "outdated": False})
        entry["rovers"][rover] = asdict(status)
        entry["outdated"] = False
        self._save()

    def mark_outdated(self, dest: str) -> None:
        entry = self._data.get(self._key(dest))
        if entry:
            entry["outdated"] = True
            self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1)
        os.replace(tmp, self.path)
