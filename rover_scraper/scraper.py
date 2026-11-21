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

from api import Cancelled, MarsVistaClient, Photo
from download import fetch_bytes, file_extension, head_size, make_session, save_new_file
from pds import is_pds_rover
from plan import (Action, CameraPlan, cameras_short, disk_camera_counts, exact_match, is_photo_file,
                  last_sol_on_disk, plan_sol)
from state import PERMANENT_STATUSES, Unavailable


@dataclass
class Report:
    sols_checked: int = 0
    sols_skipped_quick: int = 0          # same cameras and total as the manifest: no request needed
    sols_skipped_counts: int = 0         # every camera has at least the API's count (one stats request)
    actions: Counter = field(default_factory=Counter)
    downloaded: int = 0
    bytes_saved: int = 0                 # size of the photos written to disk this run
    bytes_received: int = 0              # all photo data received (includes fills that turned out to be present)
    already_present: int = 0             # photos already on disk: in complete sols, or matched while filling
    would_download: int = 0              # dry run: upper bound (a FILL may find some already present)
    newly_unavailable: int = 0           # NASA refused (403/404/410) this run; remembered for next time
    known_unavailable: int = 0           # skipped because NASA refused them on an earlier run
    sols_with_work: int = 0              # sols that needed (or, in a dry run, would need) downloads
    left_as_is: int = 0                  # Spirit/Opportunity folders from another source, not topped up
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
                 unavailable: Unavailable | None = None, workers: int = 6,
                 progress: Callable[[dict], None] | None = None,
                 cameras: dict[str, set[str]] | None = None, limiter=None,
                 detail: Callable[[str], None] | None = None):
        """progress(event) receives dicts such as {"event": "sol", "rover", "sol", "index", "total"} for the GUI.
        cameras maps a rover name to the camera names to include; a rover not in the map gets all cameras.
        limiter (ratelimit.FileLimiter) caps how many photos are downloaded per minute/hour/day.
        detail(message) receives extra lines for a verbose log (every sol and photo); off by default.
        workers and limiter may be changed while a run is going: each camera folder uses the current values."""
        self.client, self.dest, self.dry_run, self.log = client, dest, dry_run, log
        self.workers = max(1, workers)
        # Room for up to 16 parallel downloads, so the number can be raised while a run is going.
        self.http = http or make_session(16)
        self.cancel = cancel or threading.Event()
        self.fetch, self.head = fetch, head
        self.unavailable = unavailable or Unavailable(None)
        self.progress = progress or (lambda event: None)
        self.cameras = cameras or {}
        self.limiter = limiter
        self.detail = detail or (lambda message: None)

    def run(self, rover: str, sol_min: int, sol_max: int, report: Report | None = None,
            new_only: bool = False, resume_from: int | None = None) -> Report:
        """Check sols sol_min..sol_max in order and add what's missing. new_only skips sols up to the last sol
        already on disk, so existing sols aren't checked for gaps (much faster when they're known complete).
        resume_from continues a paused run: sols before it are skipped without any checks, while progress still
        counts them, so the bar continues where it stopped. (new_only is ignored when resuming, because the
        paused sol may already be on disk; pass the sol_min the paused run started with.)"""
        report = report or Report()
        if new_only and resume_from is None:
            last = last_sol_on_disk(self.dest, rover)
            if last is not None:
                sol_min = max(sol_min, last + 1)
        try:
            manifest = self.client.manifest(rover)
        except Cancelled:
            report.cancelled = True
            return report
        except Exception as e:  # noqa: BLE001 - this rover can't be checked; the others still run
            report.problems.append(f"{rover}: list of sols could not be fetched: {e}")
            self.log(f"ERROR {rover}: list of sols could not be fetched: {e}")
            return report
        allowed = self.cameras.get(rover)
        sols = sorted(s for s in manifest if sol_min <= s <= sol_max)
        self.progress({"event": "rover", "rover": rover, "total": len(sols), "sol_min": sol_min,
                       "sol_max": sol_max})
        for index, sol in enumerate(sols, 1):
            if self.cancel.is_set():
                report.cancelled = True
                break
            if resume_from is not None and sol < resume_from:
                continue
            report.sols_checked += 1
            summary = manifest[sol]
            manifest_cams = [c for c in summary.cameras if allowed is None or c in allowed]
            if not manifest_cams:
                continue
            self.progress({"event": "sol", "rover": rover, "sol": sol, "index": index, "total": len(sols)})
            unavailable = self.unavailable.per_camera(rover, sol)
            if report.sols_checked % 100 == 0:
                self.log(f"... {rover}: checked {report.sols_checked} sols, now at sol {sol}")
            try:
                disk = disk_camera_counts(self.dest, rover, sol)
                known_total = summary.total_photos is not None    # NASA PDS sols must be listed to count
                if disk is None and self.dry_run and known_total:
                    # Whole sol is missing: the manifest already gives its size, no listing needed. With a
                    # camera filter this is an upper bound, since the manifest doesn't split photos by camera.
                    self.log(f"PLAN {rover}/{sol}: new sol, {summary.total_photos} file(s) "
                             f"in {len(manifest_cams)} camera(s)")
                    report.actions["new"] += len(manifest_cams)
                    report.would_download += summary.total_photos
                    report.sols_with_work += 1
                    continue
                if disk is not None:
                    if allowed is None and known_total and exact_match(disk, summary.total_photos, list(summary.cameras)):
                        report.sols_skipped_quick += 1
                        report.already_present += sum(disk.values())
                        self.detail(f"     {rover}/{sol}: complete, {sum(disk.values())} photo(s) on disk match the "
                                    f"list")
                        continue
                    counts = self.client.camera_counts(rover, sol)
                    if allowed is not None:
                        counts = {c: n for c, n in counts.items() if c in allowed}
                    if not cameras_short(disk, counts, unavailable):
                        report.sols_skipped_counts += 1
                        on_disk = sum(n for c, n in disk.items() if allowed is None or c in allowed)
                        report.already_present += on_disk
                        self.detail(f"     {rover}/{sol}: complete by camera, {on_disk} photo(s) on disk "
                                    f"({', '.join(f'{c} {n}' for c, n in sorted(counts.items()))})")
                        continue
                self.detail(f"     {rover}/{sol}: listing photos"
                            + ("" if disk is None else f" ({sum(disk.values())} on disk)"))
                photos = self.client.photos_for_sol(rover, sol)
            except Cancelled:
                report.cancelled = True
                break
            except Exception as e:  # noqa: BLE001 - record and carry on with the next sol
                report.problems.append(f"{rover} sol {sol}: listing failed: {e}")
                self.log(f"ERROR {rover} sol {sol}: listing failed: {e}")
                continue
            if allowed is not None:
                photos = [p for p in photos if p.camera in allowed]
            plans = plan_sol(self.dest, rover, sol, photos, unavailable)
            worked = False
            for plan in plans:
                if self.cancel.is_set():
                    report.cancelled = True
                    break
                worked = self._do_camera(plan, report) or worked
            if worked:
                report.sols_with_work += 1
            if not self.dry_run:
                self.unavailable.save()   # after every sol, so an interrupted run keeps what it learned
        return report

    def _ordered_parallel(self, fn, items: Iterable) -> Iterator[tuple]:
        """Run fn(session, item.url) on worker threads, yielding (item, result-or-exception) in input order.
        At most 2 x workers results are held at once, so a big folder doesn't fill memory."""
        it = iter(items)
        with ThreadPoolExecutor(self.workers) as ex:
            try:
                window = deque((x, ex.submit(_call, fn, self.http, x.url))
                               for x in itertools.islice(it, self.workers * 2))
                while window:
                    item, fut = window.popleft()
                    result = fut.result()
                    nxt = next(it, None)
                    if nxt is not None:
                        window.append((nxt, ex.submit(_call, fn, self.http, nxt.url)))
                    yield item, result
            except KeyboardInterrupt:
                self.cancel.set()     # wake threads waiting on the download limit, so Ctrl+C quits promptly
                raise

    def _from_other_source(self, plan: CameraPlan) -> bool:
        """Spirit/Opportunity only: True if a short folder's photos came from somewhere other than NASA's archive
        (folders made with the old API hold NASA's old public-gallery copies). Those copies never match the
        archive's files, so topping the folder up would add a second copy of photos already there. If any file
        here has the size of an archive photo, the folder is this program's own unfinished download, and it is
        completed as usual. Uses size checks only (no downloads)."""
        if not (is_pds_rover(plan.rover) and plan.action is Action.FILL and plan.existing):
            return False
        sizes = {e.stat().st_size for e in os.scandir(plan.folder) if is_photo_file(e)}
        known = False
        results = self._ordered_parallel(self.head, plan.photos)
        try:
            for _, size in results:
                if isinstance(size, int):
                    known = True
                    if size in sizes:
                        return False
        finally:
            results.close()
        return known              # if the archive gave no sizes, it can't be told apart: fill as usual

    def _do_camera(self, plan: CameraPlan, report: Report) -> bool:
        """Download what's missing for one camera folder. Returns True if it needed (or would need) downloads."""
        where = f"{plan.rover}/{plan.sol}/{plan.camera}"
        report.actions[plan.action.value] += 1
        if plan.action is Action.COMPLETE:
            return False
        if self._from_other_source(plan):
            report.left_as_is += 1
            self.log(f"KEEP {where}: {plan.existing} photo(s) from another source (such as NASA's old gallery); "
                     f"the archive lists {len(plan.photos)}. Left as is, so nothing is duplicated.")
            return False
        self.progress({"event": "camera", "rover": plan.rover, "sol": plan.sol, "camera": plan.camera})
        n = len(plan.photos)
        if plan.action is Action.NEW:
            self.log(f"{'PLAN' if self.dry_run else 'GET '} {where}: new folder, {n} file(s)")
        else:
            self.log(f"{'PLAN' if self.dry_run else 'FILL'} {where}: {plan.existing} on disk, {n} listed")
        if self.dry_run:
            report.would_download += n - plan.existing if plan.action is Action.FILL else n
            return True

        todo = [p for p in plan.photos if p.id not in self.unavailable]
        report.known_unavailable += len(plan.photos) - len(todo)
        if len(todo) < len(plan.photos):
            self.detail(f"     {where}: skipping {len(plan.photos) - len(todo)} photo(s) NASA refused on an earlier "
                        f"run")
        have = Counter()
        if plan.action is Action.FILL:
            skipped = len(plan.photos) - len(todo)
            self.log(f"     checking sizes of {len(todo)} photos..."
                     + (f" ({skipped} skipped: NASA refused them before)" if skipped else ""))
            checked = len(todo)
            todo = self._drop_by_size(plan.folder, todo, report)
            self.log(f"     {checked - len(todo)} already on disk, downloading {len(todo)}")
            if todo:
                # Bytes already in the folder, as a multiset: identical images listed twice are kept twice.
                have = Counter(self._existing_hashes(plan.folder))
        if not todo:
            return True

        os.makedirs(plan.folder, exist_ok=True)
        index = plan.next_index
        results = self._ordered_parallel(self._limited_fetch, todo)
        try:
            for photo, result in results:
                if self.cancel.is_set():
                    report.cancelled = True
                    return True
                if isinstance(result, Exception):
                    self._record_failure(where, photo, result, report)
                    continue
                report.bytes_received += len(result)
                h = _sha256(result)
                if have[h] > 0:
                    have[h] -= 1
                    report.already_present += 1
                    self.detail(f"     {where}: {photo.url.rsplit('/', 1)[-1]} is already here (same bytes)")
                    continue
                name = f"{index}{file_extension(result)}"
                save_new_file(os.path.join(plan.folder, name), result)
                self.detail(f"     {where}/{name}: saved {len(result) / 1024:,.0f} KB "
                            f"({photo.url.rsplit('/', 1)[-1]})")
                index += 1
                report.downloaded += 1
                report.bytes_saved += len(result)
        finally:
            results.close()
        return True

    def _limited_fetch(self, session, url: str) -> bytes:
        """Download one photo, first waiting for the user's download limit if one is set."""
        if self.limiter is not None:
            self.limiter.acquire()
        return self.fetch(session, url)

    def _drop_by_size(self, folder: str, photos: list[Photo], report: Report) -> list[Photo]:
        """Cheap first pass for FILL: a listed photo whose remote size (HEAD, no download) equals the size of a
        file already in the folder is taken as present. Only the rest are downloaded and compared by bytes."""
        sizes = Counter(e.stat().st_size for e in os.scandir(folder) if is_photo_file(e))
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
            self.detail(f"     {where}: NASA refused {photo.url.rsplit('/', 1)[-1]} (HTTP {status}); "
                        f"remembered and skipped from now on")
        else:                                      # temporary: the next run tries again
            report.problems.append(f"{where}: photo {photo.id} not downloaded: {error}")
            self.log(f"ERROR {where}: photo {photo.id}: {error}")

    @staticmethod
    def _existing_hashes(folder: str) -> list[str]:
        out = []
        for e in os.scandir(folder):
            if is_photo_file(e):
                with open(e.path, "rb") as f:
                    out.append(_sha256(f.read()))
        return out
