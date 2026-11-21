"""Runs a scrape: quick per-sol check from the manifest, then per-camera fill-in. Shared by the CLI and GUI."""
from __future__ import annotations

import hashlib
import itertools
import os
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import requests

from .api import MarsVistaClient, Photo
from .download import fetch_bytes, head_size, make_session, save_new_file
from .plan import Action, CameraPlan, cameras_short, disk_camera_counts, exact_match, plan_sol
from .state import PERMANENT_STATUSES, Unavailable


@dataclass
class Report:
    sols_checked: int = 0
    sols_skipped_quick: int = 0          # same cameras and total as the manifest: no request needed
    sols_skipped_counts: int = 0         # every camera has at least the API's count (one stats request)
    actions: Counter = field(default_factory=Counter)
    downloaded: int = 0
    already_present: int = 0             # FILL: matched an existing file by size or bytes, not saved again
    would_download: int = 0              # dry run: upper bound (a FILL may find some already present)
    newly_unavailable: int = 0           # NASA refused (403/404/410) this run; remembered for next time
    known_unavailable: int = 0           # skipped because NASA refused them on an earlier run
    problems: list[str] = field(default_factory=list)
    cancelled: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _call(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - returned to the caller, which decides
        return e


class Scraper:
    def __init__(self, client: MarsVistaClient, dest: str, http: requests.Session | None = None,
                 dry_run: bool = False, log: Callable[[str], None] = print,
                 cancel: threading.Event | None = None, fetch=fetch_bytes, head=head_size,
                 unavailable: Unavailable | None = None, workers: int = 6):
        self.client, self.dest, self.dry_run, self.log = client, dest, dry_run, log
        self.workers = max(1, workers)
        self.http = http or make_session(self.workers)
        self.cancel = cancel or threading.Event()
        self.fetch, self.head = fetch, head
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

    def _ordered_parallel(self, fn, items: Iterable) -> Iterator[tuple]:
        """Run fn(session, item.url) on worker threads, yielding (item, result-or-exception) in input order.
        At most 2 x workers results are held at once, so a big folder doesn't fill memory."""
        it = iter(items)
        with ThreadPoolExecutor(self.workers) as ex:
            window = deque((x, ex.submit(_call, fn, self.http, x.url))
                           for x in itertools.islice(it, self.workers * 2))
            while window:
                item, fut = window.popleft()
                result = fut.result()
                nxt = next(it, None)
                if nxt is not None:
                    window.append((nxt, ex.submit(_call, fn, self.http, nxt.url)))
                yield item, result

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

        todo = [p for p in plan.photos if p.id not in self.unavailable]
        report.known_unavailable += len(plan.photos) - len(todo)
        have = Counter()
        if plan.action is Action.FILL:
            todo = self._drop_by_size(plan.folder, todo, report)
            if todo:
                # Bytes already in the folder, as a multiset: identical images listed twice are kept twice.
                have = Counter(self._existing_hashes(plan.folder))
        if not todo:
            return

        os.makedirs(plan.folder, exist_ok=True)
        index = plan.next_index
        results = self._ordered_parallel(self.fetch, todo)
        try:
            for photo, result in results:
                if self.cancel.is_set():
                    report.cancelled = True
                    return
                if isinstance(result, Exception):
                    self._record_failure(where, photo, result, report)
                    continue
                h = _sha256(result)
                if have[h] > 0:
                    have[h] -= 1
                    report.already_present += 1
                    continue
                save_new_file(os.path.join(plan.folder, str(index)), result)
                index += 1
                report.downloaded += 1
        finally:
            results.close()

    def _drop_by_size(self, folder: str, photos: list[Photo], report: Report) -> list[Photo]:
        """Cheap first pass for FILL: a listed photo whose remote size (HEAD, no download) equals the size of a
        file already in the folder is taken as present. Only the rest are downloaded and compared by bytes."""
        sizes = Counter(e.stat().st_size for e in os.scandir(folder) if e.name.isdigit() and e.is_file())
        keep = []
        for photo, size in self._ordered_parallel(self.head, photos):
            if isinstance(size, int) and sizes[size] > 0:
                sizes[size] -= 1
                report.already_present += 1
            else:
                keep.append(photo)
        return keep

    def _record_failure(self, where: str, photo: Photo, error: Exception, report: Report) -> None:
        status = getattr(error, "status", None)
        if status in PERMANENT_STATUSES:
            self.unavailable.add(photo, status)   # don't count it as missing on later runs
            report.newly_unavailable += 1
        else:                                      # temporary: the next run tries again
            report.problems.append(f"{where}: photo {photo.id} not downloaded: {error}")
            self.log(f"ERROR {where}: photo {photo.id}: {error}")

    @staticmethod
    def _existing_hashes(folder: str) -> list[str]:
        out = []
        for e in os.scandir(folder):
            if e.name.isdigit() and e.is_file():
                with open(e.path, "rb") as f:
                    out.append(_sha256(f.read()))
        return out
