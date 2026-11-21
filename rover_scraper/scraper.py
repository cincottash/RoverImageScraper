"""Runs a scrape: list photos per sol, plan what's missing, verify top-ups, download. Shared by the CLI and GUI."""
from __future__ import annotations

import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import requests

from .api import MarsVistaClient
from .download import fetch_bytes, save_new_file
from .plan import Action, CameraPlan, plan_sol


@dataclass
class Report:
    actions: Counter = field(default_factory=Counter)
    downloaded: int = 0
    would_download: int = 0
    problems: list[str] = field(default_factory=list)     # skipped folders, failed checks, errors
    cancelled: bool = False


class Scraper:
    def __init__(self, client: MarsVistaClient, dest: str, http: requests.Session | None = None,
                 dry_run: bool = False, log: Callable[[str], None] = print,
                 cancel: threading.Event | None = None, fetch=fetch_bytes):
        self.client, self.dest, self.dry_run, self.log = client, dest, dry_run, log
        self.http = http or requests.Session()
        self.cancel = cancel or threading.Event()
        self.fetch = fetch

    def run(self, rover: str, sol_min: int, sol_max: int, report: Report | None = None) -> Report:
        report = report or Report()
        for sol in range(sol_min, sol_max + 1):
            if self.cancel.is_set():
                report.cancelled = True
                break
            try:
                photos = self.client.photos_for_sol(rover, sol)
            except Exception as e:  # noqa: BLE001 - record and carry on with the next sol
                report.problems.append(f"{rover} sol {sol}: listing failed: {e}")
                self.log(f"ERROR {rover} sol {sol}: listing failed: {e}")
                continue
            for plan in plan_sol(self.dest, rover, sol, photos):
                if self.cancel.is_set():
                    report.cancelled = True
                    break
                self._do_camera(plan, report)
        return report

    def _do_camera(self, plan: CameraPlan, report: Report) -> None:
        where = f"{plan.rover}/{plan.sol}/{plan.camera}"
        report.actions[plan.action.value] += 1
        if plan.action in (Action.SKIP_EXTRA, Action.CONFLICT):
            report.problems.append(f"{where}: skipped ({plan.action.value}): {plan.reason}")
            self.log(f"SKIP {where}: {plan.reason}")
            return
        if plan.action is Action.COMPLETE:
            return
        missing = plan.missing
        if plan.action is Action.TOPUP:
            self.log(f"TOPUP {where}: {plan.reason}, verifying existing files")
            if not self._verify_prefix(plan, report):
                return
        self.log(f"{'PLAN' if self.dry_run else 'GET '} {where}: {len(missing)} file(s) "
                 f"({missing[0][0]}..{missing[-1][0]})")
        if self.dry_run:
            report.would_download += len(missing)
            return
        os.makedirs(plan.folder, exist_ok=True)
        for index, photo in missing:
            if self.cancel.is_set():
                report.cancelled = True
                return
            try:
                save_new_file(os.path.join(plan.folder, str(index)), self.fetch(self.http, photo.url))
                report.downloaded += 1
            except Exception as e:  # noqa: BLE001
                # Stop this camera: a gap would shift the numbering of every later file.
                report.problems.append(f"{where}/{index}: download failed, camera stopped: {e}")
                self.log(f"ERROR {where}/{index}: {e}")
                return

    def _verify_prefix(self, plan: CameraPlan, report: Report) -> bool:
        """The existing files 0..k-1 must be byte-identical to the first k photos, otherwise appending would
        number new files wrongly. This downloads those k images (top-ups are rare: mostly interrupted last sols)."""
        where = f"{plan.rover}/{plan.sol}/{plan.camera}"
        for i in range(plan.existing):
            path = os.path.join(plan.folder, str(i))
            try:
                remote = self.fetch(self.http, plan.photos[i].url)
            except Exception as e:  # noqa: BLE001
                report.problems.append(f"{where}: top-up check could not download file {i}: {e}")
                return False
            with open(path, "rb") as f:
                local = f.read()
            if local != remote:
                report.problems.append(f"{where}: top-up refused, file {i} differs from Mars Vista photo "
                                       f"{plan.photos[i].id}")
                self.log(f"SKIP {where}: file {i} differs from Mars Vista; not adding files")
                return False
        return True
