"""Runs a scrape: quick per-sol check from the manifest, then per-camera fill-in. Shared by the CLI and GUI."""
from __future__ import annotations

import hashlib
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import requests

from .api import MarsVistaClient
from .download import fetch_bytes, save_new_file
from .plan import Action, CameraPlan, cameras_short, disk_camera_counts, exact_match, plan_sol
from .state import PERMANENT_STATUSES, Unavailable


@dataclass
class Report:
    sols_checked: int = 0
    sols_skipped_quick: int = 0          # same cameras and total as the manifest: no request needed
    sols_skipped_counts: int = 0         # every camera has at least the API's count (one stats request)
    actions: Counter = field(default_factory=Counter)
    downloaded: int = 0
    already_present: int = 0             # FILL: photos whose bytes were already in the folder
    would_download: int = 0              # dry run: upper bound (a FILL may find some already present)
    newly_unavailable: int = 0           # NASA refused (403/404/410) this run; remembered for next time
    known_unavailable: int = 0           # skipped because NASA refused them on an earlier run
    problems: list[str] = field(default_factory=list)
    cancelled: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Scraper:
    def __init__(self, client: MarsVistaClient, dest: str, http: requests.Session | None = None,
                 dry_run: bool = False, log: Callable[[str], None] = print,
                 cancel: threading.Event | None = None, fetch=fetch_bytes,
                 unavailable: Unavailable | None = None):
        self.client, self.dest, self.dry_run, self.log = client, dest, dry_run, log
        self.http = http or requests.Session()
        self.cancel = cancel or threading.Event()
        self.fetch = fetch
        self.unavailable = unavailable or Unavailable(None)

    def run(self, rover: str, sol_min: int, sol_max: int, report: Report | None = None) -> Report:
        report = report or Report()
        manifest = self.client.manifest(rover)
        for sol in sorted(s for s in manifest if sol_min <= s <= sol_max):
            if self.cancel.is_set():
                report.cancelled = True
                break
            report.sols_checked += 1
            summary = manifest[sol]
            unavailable = self.unavailable.per_camera(rover, sol)
            try:
                disk = disk_camera_counts(self.dest, rover, sol)
                if disk is not None:
                    if exact_match(disk, summary.total_photos, list(summary.cameras)):
                        report.sols_skipped_quick += 1
                        continue
                    if not cameras_short(disk, self.client.camera_counts(rover, sol), unavailable):
                        report.sols_skipped_counts += 1
                        continue
                photos = self.client.photos_for_sol(rover, sol)
            except Exception as e:  # noqa: BLE001 - record and carry on with the next sol
                report.problems.append(f"{rover} sol {sol}: listing failed: {e}")
                self.log(f"ERROR {rover} sol {sol}: listing failed: {e}")
                continue
            for plan in plan_sol(self.dest, rover, sol, photos, unavailable):
                if self.cancel.is_set():
                    report.cancelled = True
                    break
                self._do_camera(plan, report)
            if not self.dry_run:
                self.unavailable.save()   # after every sol, so an interrupted run keeps what it learned
        return report

    def _do_camera(self, plan: CameraPlan, report: Report) -> None:
        where = f"{plan.rover}/{plan.sol}/{plan.camera}"
        report.actions[plan.action.value] += 1
        if plan.action is Action.COMPLETE:
            return
        n = len(plan.photos)
        if plan.action is Action.NEW:
            self.log(f"{'PLAN' if self.dry_run else 'GET '} {where}: new folder, {n} file(s)")
        else:
            self.log(f"{'PLAN' if self.dry_run else 'FILL'} {where}: {plan.existing} on disk, {n} listed")
        if self.dry_run:
            report.would_download += n - plan.existing if plan.action is Action.FILL else n
            return

        os.makedirs(plan.folder, exist_ok=True)
        # Bytes already in the folder, as a multiset: identical images listed twice are kept twice.
        have = Counter(self._existing_hashes(plan.folder)) if plan.action is Action.FILL else Counter()
        index = plan.next_index
        for photo in plan.photos:
            if self.cancel.is_set():
                report.cancelled = True
                return
            if photo.id in self.unavailable:
                report.known_unavailable += 1
                continue
            try:
                data = self.fetch(self.http, photo.url)
            except Exception as e:  # noqa: BLE001 - skip this photo
                status = getattr(e, "status", None)
                if status in PERMANENT_STATUSES:
                    self.unavailable.add(photo, status)   # don't count it as missing on later runs
                    report.newly_unavailable += 1
                else:                                      # temporary: the next run tries again
                    report.problems.append(f"{where}: photo {photo.id} not downloaded: {e}")
                    self.log(f"ERROR {where}: photo {photo.id}: {e}")
                continue
            h = _sha256(data)
            if have[h] > 0:
                have[h] -= 1
                report.already_present += 1
                continue
            save_new_file(os.path.join(plan.folder, str(index)), data)
            index += 1
            report.downloaded += 1

    @staticmethod
    def _existing_hashes(folder: str) -> list[str]:
        out = []
        for e in os.scandir(folder):
            if e.name.isdigit() and e.is_file():
                with open(e.path, "rb") as f:
                    out.append(_sha256(f.read()))
        return out
