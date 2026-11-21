"""Spirit and Opportunity photos straight from NASA's Planetary Data System (PDS) Imaging Node.

Mars Vista's lists for these two rovers leave out many photos (most sub-frames), so for them the photo list comes
from the PDS archive's own folders instead. Each camera volume has one folder of JPEG previews per sol:

    https://planetarydata.jpl.nasa.gov/img/data/mer/<rover>/mer<1|2><vol>_0xxx/browse/sol<NNNN>/edr/

File names follow the MER convention, for example 2p126468064eff0000p2121l2m1.img.jpg:
    [0] rover (1 Opportunity, 2 Spirit), [1] camera letter, [2:11] spacecraft clock, [11:14] product type.

The old Mars Rover Photos API (and folders made with it) had every product except thumbnails and calibration
products. Keeping the full-frame (eff), downsampled (edn) and sub-frame (esf) products reproduces its counts.
Nothing here uses the Mars Vista quota.
"""
from __future__ import annotations

import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import requests

from api import ApiError, Cancelled, Photo, SolSummary
from download import USER_AGENT

PDS_BASE = "https://planetarydata.jpl.nasa.gov/img/data/mer"
ROVERS = {"spirit": "2", "opportunity": "1"}

# Camera volumes and the camera each file-name letter stands for. MINITES is the name the old API (and existing
# folders) used for the Microscopic Imager.
VOLUMES = ("no", "po", "ho", "mo", "do")
CAMERA_LETTERS = {"n": "NAVCAM", "p": "PANCAM", "f": "FHAZ", "r": "RHAZ", "m": "MINITES", "e": "ENTRY"}
VOLUME_CAMERAS = {"no": ("NAVCAM",), "po": ("PANCAM",), "ho": ("FHAZ", "RHAZ"), "mo": ("MINITES",),
                  "do": ("ENTRY",)}
KEEP_TYPES = ("eff", "edn", "esf")
LAST_SOL = {"spirit": 2209, "opportunity": 5111}     # last sols with photos in the archive (missions ended)

_LINK = re.compile(r'href="([^"?/]+)(/?)"')


def is_pds_rover(rover: str) -> bool:
    return rover.lower() in ROVERS


def photo_id(name: str) -> int:
    """A stable number for a PDS file: ordered by rover and capture time, unique per file. Far above Mars Vista's
    IDs, so the two never clash in the list of photos NASA refused."""
    clock = int(name[2:11]) if name[2:11].isdigit() else 0
    return (int(name[0]) * 10 ** 10 + clock) * 10 ** 6 + zlib.crc32(name.lower().encode()) % 10 ** 6


class PdsClient:
    """Same methods as MarsVistaClient that the scraper and scans use: manifest, camera_counts, photos_for_sol."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 60, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep, cancel: threading.Event | None = None,
                 log: Callable[[str], None] | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT     # PDS refuses requests that don't say who they are
        self.timeout, self.max_retries, self.sleep = timeout, max_retries, sleep
        self.cancel = cancel or threading.Event()
        self.log = log or (lambda message: None)
        self._sol_cache: tuple[tuple[str, int], list[Photo]] | None = None

    # ---- folder listings ---------------------------------------------------------------------------------

    def _volume_url(self, rover: str, vol: str) -> str:
        r = rover.lower()
        return f"{PDS_BASE}/{r}/mer{ROVERS[r]}{vol}_0xxx/browse/"

    def _list(self, url: str) -> list[tuple[str, bool]] | None:
        """(name, is_folder) for each entry of a PDS folder page, or None if the folder doesn't exist."""
        last = None
        for attempt in range(self.max_retries + 1):
            if self.cancel.is_set():
                raise Cancelled()
            try:
                r = self.session.get(url, timeout=(15, self.timeout))
                if r.status_code == 404:
                    return None
                if r.status_code == 200:
                    seen, out = set(), []
                    for name, slash in _LINK.findall(r.text):
                        if name not in seen and not name.startswith(("mailto:", "http")):
                            seen.add(name)
                            out.append((name, bool(slash)))
                    return out
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:     # includes pages cut off part way
                last = f"{type(e).__name__}: {e}"
            self.sleep(min(60, 2 ** attempt))
        raise ApiError(f"NASA PDS folder {url} could not be listed: {last}")

    # ---- the client interface ----------------------------------------------------------------------------

    def manifest(self, rover: str) -> dict[int, SolSummary]:
        """Every sol of the mission (no request needed: both missions are over). NASA's own lists of sols are
        pages that take 30 s or more to build and often break off, and Mars Vista's list misses hundreds of sols,
        so each sol is simply looked at; one without photos answers "not found" in a fraction of a second.
        Photo counts aren't known without listing a sol, so total_photos is None."""
        cameras = tuple(sorted(c for v in VOLUMES for c in VOLUME_CAMERAS[v]))
        return {sol: SolSummary(None, cameras) for sol in range(0, LAST_SOL[rover.lower()] + 1)}

    def photos_for_sol(self, rover: str, sol: int) -> list[Photo]:
        key = (rover.lower(), sol)
        if self._sol_cache and self._sol_cache[0] == key:
            return list(self._sol_cache[1])
        urls = [f"{self._volume_url(rover, v)}sol{sol:04d}/edr/" for v in VOLUMES]
        with ThreadPoolExecutor(len(urls)) as ex:          # a camera without this sol answers 404 quickly
            listings = list(ex.map(self._list, urls))
        photos = []
        for url, listing in zip(urls, listings):
            for name, is_folder in listing or []:
                low = name.lower()
                if is_folder or not low.endswith(".jpg") or low[11:14] not in KEEP_TYPES:
                    continue
                camera = CAMERA_LETTERS.get(low[1])
                if camera:
                    photos.append(Photo(id=photo_id(low), rover=rover, sol=sol, camera=camera, url=url + name))
        photos.sort(key=lambda p: p.id)
        self._sol_cache = (key, photos)
        return list(photos)

    def camera_counts(self, rover: str, sol: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.photos_for_sol(rover, sol):        # cached, so the listing that follows costs nothing
            counts[p.camera] = counts.get(p.camera, 0) + 1
        return counts


class RoverSources:
    """Routes each rover to its source: Spirit and Opportunity to NASA PDS, the others to Mars Vista."""

    def __init__(self, marsvista, pds: PdsClient):
        self.marsvista, self.pds = marsvista, pds

    def _for(self, rover: str):
        return self.pds if is_pds_rover(rover) else self.marsvista

    def manifest(self, rover: str) -> dict[int, SolSummary]:
        return self._for(rover).manifest(rover)

    def camera_counts(self, rover: str, sol: int) -> dict[str, int]:
        return self._for(rover).camera_counts(rover, sol)

    def photos_for_sol(self, rover: str, sol: int) -> list[Photo]:
        return self._for(rover).photos_for_sol(rover, sol)
