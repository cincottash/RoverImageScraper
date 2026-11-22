"""When and where each photo on disk was taken, looked up online and kept in the app data folder.

Which NASA photo a file is:
- photos this app downloads are recorded as they're saved (the downloader knows);
- photos already on disk are matched by exact file size: the size of each listed photo on NASA's server (a size
  check, nothing downloaded) against the files in the camera folder. Order can't be used: a folder's numbering
  skips photos NASA refused, and gap fills add photos at the end.

Where the details come from:
- Curiosity and Perseverance: Mars Vista's complete photo records (version 2 of its listing), one request per
  100 photos of a sol;
- Spirit and Opportunity: the description block at the start of each original image in NASA's archive (only
  the first 16 KB is read). Files from older downloads of these two rovers can't be matched to single photos
  (their copies differ from the archive's), so they get the details of their sol as a whole.

The photo folder is only read. Everything is saved in photo_details.db in the app data folder.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields

from PySide6.QtCore import QObject, Signal

from download import head_size
from pds import is_pds_rover
from photo_library import cameras_in, photos_in

DB_NAME = "photo_details.db"
SIZE_CHECKS_AT_ONCE = 16


@dataclass
class PhotoInfo:
    photo_id: int
    rover: str
    sol: int
    camera: str
    image_url: str = ""              # the copy the downloader saves (for size matching)
    taken_utc: str | None = None     # ISO time
    mars_time: str | None = None     # "HH:MM:SS", local solar time
    site: int | None = None
    drive_no: int | None = None
    x: float | None = None           # metres from the site's starting point: x north, y east, z down
    y: float | None = None
    z: float | None = None
    mast_az: float | None = None
    mast_el: float | None = None
    filter_name: str | None = None
    credit: str | None = None
    page_url: str | None = None      # NASA's page for the photo, when there is one


_COLUMNS = [f.name for f in fields(PhotoInfo)]


# ---- reading the records -------------------------------------------------------------------------------------

def _clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().strip('"')
        if not v or v.upper() in ("UNK", "NULL", "N/A", "NONE"):
            return None
    return v


def _num(v) -> float | None:
    v = _clean(v)
    if v is None:
        return None
    try:
        return float(str(v).split("<")[0].strip())
    except ValueError:
        return None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _xyz(v) -> tuple[float, float, float] | None:
    if isinstance(v, dict):
        parts = [_num(v.get(k)) for k in ("x", "y", "z")]
    else:
        v = _clean(v)
        if v is None:
            return None
        parts = [_num(p) for p in str(v).strip("()").split(",")]
    if len(parts) != 3 or any(p is None for p in parts) or all(abs(p) < 1e-9 for p in parts):
        return None
    return parts[0], parts[1], parts[2]


_CLOCK = re.compile(r"(\d{1,2}:\d{2}:\d{2})")


def _clock(v) -> str | None:
    v = _clean(v)
    m = _CLOCK.search(str(v)) if v is not None else None
    return m.group(1).zfill(8) if m else None


def _iso(v) -> str | None:
    v = _clean(v)
    if v is None:
        return None
    v = str(v).replace(" ", "T")
    return v if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v) else None


def parse_marsvista(rec: dict, rover: str) -> PhotoInfo:
    """A Mars Vista version 2 record (field_set=complete) as PhotoInfo."""
    a = rec.get("attributes") or {}
    raw = a.get("raw_data") or {}
    ext = raw.get("extended") or {}
    loc = a.get("location") or {}
    tel = a.get("telemetry") or {}
    images = a.get("images") or {}
    cam = (((rec.get("relationships") or {}).get("camera") or {}).get("id")
           or (raw.get("camera") or {}).get("instrument") or "")
    # The copy the downloader saves: Perseverance's 1200-pixel JPEG, the full image for the others
    url = (images.get("large") if rover == "Perseverance" else None) or images.get("full") or a.get("img_src") or ""
    xyz = _xyz(loc.get("coordinates")) or _xyz(raw.get("xyz")) or _xyz(ext.get("xyz"))
    page = _clean(raw.get("link"))
    if page and page.startswith("/"):
        page = None                  # an address on NASA's retired raw-image site
    info = PhotoInfo(
        photo_id=int(rec["id"]), rover=rover, sol=int(a.get("sol") or raw.get("sol") or 0), camera=cam,
        image_url=url,
        # NASA's own times first: Mars Vista's date_taken_utc for Curiosity is 7 hours early (19:07:43 UTC in
        # NASA's record and caption reads 12:07:43), apparently converted from California time.
        taken_utc=_iso(raw.get("date_taken_utc")) or _iso(raw.get("date_taken")) or _iso(a.get("date_taken_utc")),
        mars_time=_clock(a.get("date_taken_mars")) or _clock(raw.get("date_taken_mars")) or _clock(ext.get("lmst")),
        site=_int(loc.get("site")) if _int(loc.get("site")) is not None else _int(raw.get("site")),
        drive_no=_int(raw.get("drive")),
        mast_az=_num(tel.get("mast_azimuth")) if _num(tel.get("mast_azimuth")) is not None else
        _num(ext.get("mast_az")) if _num(ext.get("mast_az")) is not None else _num(ext.get("mastAz")),
        mast_el=_num(tel.get("mast_elevation")) if _num(tel.get("mast_elevation")) is not None else
        _num(ext.get("mast_el")) if _num(ext.get("mast_el")) is not None else _num(ext.get("mastEl")),
        filter_name=_clean(ext.get("filter_name")) or _clean((raw.get("camera") or {}).get("filter_name")),
        credit=_clean(a.get("credit")), page_url=page)
    if xyz:
        info.x, info.y, info.z = xyz
    return info


def _label_value(text: str, key: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text, re.M)
    return m.group(1) if m else None


def parse_label(text: str, photo_id: int, rover: str, sol: int, camera: str, url: str) -> PhotoInfo:
    """The description block (PDS label) at the start of a Spirit or Opportunity original image."""
    info = PhotoInfo(photo_id=photo_id, rover=rover, sol=sol, camera=camera, image_url=url)
    start = _label_value(text, "START_TIME")
    info.taken_utc = _iso(start[:19] if start else None)
    info.mars_time = _clock(_label_value(text, "LOCAL_TRUE_SOLAR_TIME"))
    counter = _label_value(text, "ROVER_MOTION_COUNTER")
    if counter:
        parts = [_int(p) for p in counter.strip("()").split(",")]
        if len(parts) >= 2:
            info.site, info.drive_no = parts[0], parts[1]
    xyz = _xyz(_label_value(text, "ORIGIN_OFFSET_VECTOR"))
    if xyz:
        info.x, info.y, info.z = xyz
    info.mast_az = _num(_label_value(text, "INSTRUMENT_AZIMUTH"))
    info.mast_el = _num(_label_value(text, "INSTRUMENT_ELEVATION"))
    info.filter_name = _clean(_label_value(text, "FILTER_NAME"))
    info.credit = "NASA/JPL-Caltech"
    return info


def pds_label_url(jpg_url: str) -> str:
    """The original image (with its description block) for an archive preview: browse/…/x.img.jpg → data/…/x.img"""
    return jpg_url.replace("/browse/", "/data/")[: -len(".jpg")] if jpg_url.endswith(".jpg") else jpg_url


def read_label(session, jpg_url: str, timeout: float = 30) -> str | None:
    """The first 16 KB of the original image, where its description block is; None if it can't be read."""
    for attempt in range(3):
        try:
            r = session.get(pds_label_url(jpg_url), headers={"Range": "bytes=0-16383"}, timeout=timeout)
            if r.status_code in (200, 206):
                return r.content[:16384].decode("latin-1")
            if r.status_code < 500 and r.status_code != 429:
                return None
        except Exception:  # noqa: BLE001 - network trouble: try again, then give up on this photo
            pass
        time.sleep(1 + attempt * 2)
    return None


# ---- the store -----------------------------------------------------------------------------------------------

def folder_key(folder: str) -> str:
    return os.path.normcase(os.path.abspath(folder)) if folder else ""


class DetailsStore:
    """photo_details.db: which photo each file is (per photo folder), the photos' details, and which sols have
    been looked up. Safe to use from several threads."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            cols = ", ".join(f"{c} {'INTEGER PRIMARY KEY' if c == 'photo_id' else ''}" for c in _COLUMNS)
            self.db.execute(f"CREATE TABLE IF NOT EXISTS photos ({cols})")
            self.db.execute("CREATE TABLE IF NOT EXISTS files (folder TEXT NOT NULL, rover TEXT NOT NULL, "
                            "sol INTEGER NOT NULL, camera TEXT NOT NULL, number INTEGER NOT NULL, "
                            "photo_id INTEGER NOT NULL, url TEXT, how TEXT, "
                            "PRIMARY KEY (folder, rover, sol, camera, number))")
            self.db.execute("CREATE TABLE IF NOT EXISTS sols (folder TEXT NOT NULL, rover TEXT NOT NULL, "
                            "sol INTEGER NOT NULL, checked_at REAL, summary TEXT, "
                            "PRIMARY KEY (folder, rover, sol))")
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.flush()
            self.db.close()

    def flush(self) -> None:
        """Put everything in the main file (before it's copied to another folder)."""
        with self._lock:
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def record_file(self, folder: str, rover: str, sol: int, camera: str, number: int, photo_id: int,
                    url: str | None, how: str, commit: bool = True) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                            (folder_key(folder), rover, sol, camera, number, photo_id, url, how))
            if commit:
                self.db.commit()

    def files_in(self, folder: str, rover: str, sol: int, camera: str) -> dict[int, tuple[int, str, str]]:
        """number -> (photo_id, url, how) for the recorded files of one camera folder."""
        with self._lock:
            rows = self.db.execute("SELECT number, photo_id, url, how FROM files WHERE folder=? AND rover=? AND "
                                   "sol=? AND camera=?", (folder_key(folder), rover, sol, camera)).fetchall()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def save_photos(self, infos: list[PhotoInfo]) -> None:
        if not infos:
            return
        marks = ",".join("?" * len(_COLUMNS))
        with self._lock:
            self.db.executemany(f"INSERT OR REPLACE INTO photos ({','.join(_COLUMNS)}) VALUES ({marks})",
                                [tuple(getattr(i, c) for c in _COLUMNS) for i in infos])
            self.db.commit()

    def photo(self, photo_id: int) -> PhotoInfo | None:
        with self._lock:
            row = self.db.execute(f"SELECT {','.join(_COLUMNS)} FROM photos WHERE photo_id=?",
                                  (photo_id,)).fetchone()
        return PhotoInfo(**dict(zip(_COLUMNS, row))) if row else None

    def file_photo(self, folder: str, rover: str, sol: int, camera: str, number: int
                   ) -> tuple[PhotoInfo | None, tuple | None]:
        """The details of the photo a file is (None if not known yet), and the file's record (None if the file
        isn't matched to a photo)."""
        with self._lock:
            link = self.db.execute("SELECT photo_id, url, how FROM files WHERE folder=? AND rover=? AND sol=? AND "
                                   "camera=? AND number=?",
                                   (folder_key(folder), rover, sol, camera, number)).fetchone()
        if link is None:
            return None, None
        return self.photo(link[0]), link

    def sol_state(self, folder: str, rover: str, sol: int) -> dict | None:
        """The sol's summary if it has been looked up (possibly empty), else None."""
        with self._lock:
            row = self.db.execute("SELECT checked_at, summary FROM sols WHERE folder=? AND rover=? AND sol=?",
                                  (folder_key(folder), rover, sol)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[1] or "{}")
        except ValueError:
            return {}

    def mark_sol(self, folder: str, rover: str, sol: int, summary: dict) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO sols VALUES (?,?,?,?,?)",
                            (folder_key(folder), rover, sol, time.time(), json.dumps(summary)))
            self.db.commit()

    def forget_sol(self, folder: str, rover: str, sol: int) -> None:
        """Look the sol up again next time (a download added photos to it)."""
        with self._lock:
            self.db.execute("DELETE FROM sols WHERE folder=? AND rover=? AND sol=?", (folder_key(folder), rover, sol))
            self.db.commit()

    def sols_done(self, folder: str, rover: str) -> set[int]:
        with self._lock:
            rows = self.db.execute("SELECT sol FROM sols WHERE folder=? AND rover=?",
                                   (folder_key(folder), rover)).fetchall()
        return {r[0] for r in rows}

    def commit(self) -> None:
        with self._lock:
            self.db.commit()


# ---- looking up a sol ----------------------------------------------------------------------------------------

@dataclass
class SolResult:
    files: int = 0                 # photo files in the sol's folder
    known: int = 0                 # matched to a photo before this look-up
    matched: int = 0               # newly matched now
    unmatched: int = 0             # no photo found for them (older copies, thumbnails NASA no longer lists)


def _summary(infos: list[PhotoInfo]) -> dict:
    """The sol as a whole: first and last time taken, Mars local times, sites and drives."""
    timed = sorted((i for i in infos if i.taken_utc), key=lambda i: i.taken_utc)
    s: dict = {"photos": len(infos)}
    if timed:
        s["first_utc"], s["last_utc"] = timed[0].taken_utc, timed[-1].taken_utc
    clocks = sorted(i.mars_time for i in infos if i.mars_time)
    if clocks:
        s["first_mars"], s["last_mars"] = clocks[0], clocks[-1]
    sites = sorted({i.site for i in infos if i.site is not None})
    drives = sorted({i.drive_no for i in infos if i.drive_no is not None})
    if sites:
        s["sites"] = sites
    if drives:
        s["drives"] = [drives[0], drives[-1]]
    return s


def _match_by_size(store: DetailsStore, folder: str, rover: str, sol: int, camera: str,
                   candidates: list[PhotoInfo], nasa, cancel: threading.Event, result: SolResult,
                   probe_first: bool = False) -> None:
    """Match the camera folder's unrecorded files to listed photos with the same byte size on NASA's server."""
    files = photos_in(folder, rover, sol, camera)
    linked = store.files_in(folder, rover, sol, camera)
    result.files += len(files)
    result.known += sum(1 for f in files if f.number in linked)
    todo = [f for f in files if f.number not in linked]
    taken = {v[0] for v in linked.values()}
    candidates = [c for c in candidates if c.photo_id not in taken and c.image_url]
    if not todo:
        return
    if not candidates:
        result.unmatched += len(todo)
        return
    sizes_on_disk = {}
    for f in todo:
        try:
            sizes_on_disk[f.number] = os.path.getsize(f.path)
        except OSError:
            pass
    if probe_first:
        # Folders from older downloads hold different copies than NASA's archive serves: one size check tells
        # whether this folder can match at all, before checking every photo.
        first = head_size(nasa, candidates[0].image_url, timeout=30, retries=1)
        if first is None or first not in set(sizes_on_disk.values()):
            result.unmatched += len(todo)
            return
    with ThreadPoolExecutor(SIZE_CHECKS_AT_ONCE) as ex:
        sizes = list(ex.map(lambda c: None if cancel.is_set() else head_size(nasa, c.image_url, timeout=30,
                                                                                retries=1), candidates))
    if cancel.is_set():
        return
    by_size: dict[int, list[PhotoInfo]] = defaultdict(list)
    for c, s in zip(candidates, sizes):
        if s is not None:
            by_size[s].append(c)
    for f in todo:                       # in number order; photos of the same size are taken in ID order
        s = sizes_on_disk.get(f.number)
        if s is not None and by_size.get(s):
            c = by_size[s].pop(0)
            # No address: the photo's record has it (saves about 130 bytes per file in a library of a million)
            store.record_file(folder, rover, sol, camera, f.number, c.photo_id, None, "size", commit=False)
            result.matched += 1
        else:
            result.unmatched += 1
    store.commit()


def look_up_sol(folder: str, rover: str, sol: int, sources, store: DetailsStore, nasa,
                cancel: threading.Event) -> SolResult:
    """Fetch the details of a sol's photos, match the files on disk to them, and remember the sol as done.
    sources: pds.RoverSources (or anything with .marsvista and .pds); nasa: a requests session for NASA."""
    result = SolResult()
    cams = [c for c, _ in cameras_in(folder, rover, sol)]
    if is_pds_rover(rover):
        listed = sources.pds.photos_for_sol(rover, sol)
        by_cam: dict[str, list[PhotoInfo]] = defaultdict(list)
        for p in listed:
            by_cam[p.camera].append(PhotoInfo(photo_id=p.id, rover=rover, sol=sol, camera=p.camera, image_url=p.url))
        infos = []
        for cam in cams:
            if cancel.is_set():
                return result
            _match_by_size(store, folder, rover, sol, cam, by_cam.get(cam, []), nasa, cancel, result,
                           probe_first=True)
            listed_urls = {p.id: p.url for p in listed}
            for number, (photo_id, url, _how) in store.files_in(folder, rover, sol, cam).items():
                url = url or listed_urls.get(photo_id)            # files matched by size keep no address
                if store.photo(photo_id) is None and url:
                    text = read_label(nasa, url)
                    if text:
                        infos.append(parse_label(text, photo_id, rover, sol, cam, url))
        # The sol as a whole, from its first and last photo, for files that can't be matched to one photo
        ends = []
        for p in ([listed[0], listed[-1]] if len(listed) > 1 else listed):
            text = read_label(nasa, p.url)
            if text:
                ends.append(parse_label(text, p.id, rover, sol, p.camera, p.url))
        store.save_photos(infos)
        summary = _summary(ends)
        summary["photos"] = len(listed)
    else:
        records = sources.marsvista.photo_records_for_sol(rover, sol)
        infos = [parse_marsvista(r, rover) for r in records]
        store.save_photos(infos)
        by_cam = defaultdict(list)
        for i in infos:
            by_cam[i.camera].append(i)
        for cam in cams:
            if cancel.is_set():
                return result
            _match_by_size(store, folder, rover, sol, cam, by_cam.get(cam, []), nasa, cancel, result)
        summary = _summary(infos)
    if not cancel.is_set():
        summary.update(files=result.files, matched=result.known + result.matched)
        store.mark_sol(folder, rover, sol, summary)
    return result


# ---- what the Details panel shows ------------------------------------------------------------------------------

def fmt_utc(iso: str | None, with_date: bool = True) -> str:
    if not iso:
        return ""
    try:
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return iso
    return (time.strftime("%d %b %Y, %H:%M:%S UTC", t) if with_date else time.strftime("%H:%M:%S", t)).lstrip("0")


def fmt_place(x: float, y: float, z: float) -> str:
    """The rover's place in its site's frame (x north, y east, z down) in plain words."""
    parts = []
    if abs(x) >= 0.05:
        parts.append(f"{abs(x):,.1f} m {'north' if x > 0 else 'south'}")
    if abs(y) >= 0.05:
        parts.append(f"{abs(y):,.1f} m {'east' if y > 0 else 'west'}")
    text = " and ".join(parts) + " of the site's starting point" if parts else "At the site's starting point"
    if abs(z) >= 0.05:
        text += f", {abs(z):,.1f} m {'lower' if z > 0 else 'higher'}"
    return text


def describe(info: PhotoInfo) -> list[tuple[str, str]]:
    rows = []
    if info.taken_utc:
        rows.append(("Taken", fmt_utc(info.taken_utc)))
    if info.mars_time:
        rows.append(("Mars Local Time", info.mars_time))
    if info.site is not None:
        rows.append(("Where", f"Site {info.site}" + (f", drive {info.drive_no}" if info.drive_no is not None else "")))
    if info.x is not None:
        rows.append(("Position", fmt_place(info.x, info.y, info.z)))
    if info.mast_az is not None and info.mast_el is not None:
        rows.append(("Pointing", f"Azimuth {info.mast_az:.1f}°, elevation {info.mast_el:.1f}°"))
    if info.filter_name:
        rows.append(("Filter", info.filter_name))
    if info.credit:
        rows.append(("Credit", info.credit))
    link = info.page_url or info.image_url
    if link:
        rows.append(("NASA", f'<a href="{link}">{"Photo page" if info.page_url else "Original image"}</a>'))
    return rows


def describe_sol(summary: dict) -> list[tuple[str, str]]:
    """Details for the whole sol, for a file that couldn't be matched to one photo."""
    rows = []
    first, last = summary.get("first_utc"), summary.get("last_utc")
    if first and last:
        same_day = first[:10] == last[:10]
        rows.append(("Sol's Photos Taken", fmt_utc(first) if first == last else
                     f"{fmt_utc(first)} to {fmt_utc(last, with_date=not same_day)}" +
                     (" UTC" if same_day else "")))
    if summary.get("first_mars"):
        a, b = summary["first_mars"], summary.get("last_mars") or summary["first_mars"]
        rows.append(("Mars Local Time", a if a == b else f"{a} to {b}"))
    sites, drives = summary.get("sites"), summary.get("drives")
    if sites:
        if len(sites) == 1:
            where = f"Site {sites[0]}"
            if drives:              # drive numbers restart at each site, so they're shown for one site only
                where += f", drive {drives[0]}" if drives[0] == drives[-1] else f", drives {drives[0]} to {drives[-1]}"
        else:
            where = "Sites " + ", ".join(str(s) for s in sites[:-1]) + f" and {sites[-1]}"
        rows.append(("Where", where))
    if rows:
        rows.append(("Details For", "The whole sol: this file couldn't be matched to one NASA photo"))
    return rows


# ---- look-ups for the Photos page -----------------------------------------------------------------------------

class SolLookups(QObject):
    """Looks up sols on a background thread when the Photos page shows a photo without details.
    finished(rover, sol, error) arrives on the GUI thread; error is "" when it worked."""

    finished = Signal(str, int, str)

    def __init__(self, store: DetailsStore, make_sources, make_nasa, parent=None):
        """make_sources(rover) gives a RoverSources (or None when a Mars Vista key is needed but missing);
        make_nasa() gives a requests session for NASA's servers."""
        super().__init__(parent)
        self.store, self.make_sources, self.make_nasa = store, make_sources, make_nasa
        self.cancel = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._pending: set[tuple[str, str, int]] = set()
        self.failed: dict[tuple[str, str, int], str] = {}
        self._thread = threading.Thread(target=self._run, daemon=True, name="sol-lookups")
        self._thread.start()

    def pending(self, folder: str, rover: str, sol: int) -> bool:
        return (folder_key(folder), rover, sol) in self._pending

    def request(self, folder: str, rover: str, sol: int) -> None:
        key = (folder_key(folder), rover, sol)
        if key in self._pending:
            return
        self.failed.pop(key, None)
        self._pending.add(key)
        self._queue.put((folder, rover, sol))

    def close(self) -> None:
        self.cancel.set()
        self._queue.put(None)

    def _run(self) -> None:
        nasa = None
        while True:
            job = self._queue.get()
            if job is None or self.cancel.is_set():
                return
            folder, rover, sol = job
            key = (folder_key(folder), rover, sol)
            error = ""
            try:
                sources = self.make_sources(rover)
                if sources is None:
                    error = "no key"
                else:
                    nasa = nasa or self.make_nasa()
                    look_up_sol(folder, rover, sol, sources, self.store, nasa, self.cancel)
            except Exception as e:  # noqa: BLE001 - shown in the details panel
                error = f"{type(e).__name__}: {e}"[:300]
            if error:
                self.failed[key] = error
            self._pending.discard(key)
            if not self.cancel.is_set():
                self.finished.emit(rover, sol, error)
