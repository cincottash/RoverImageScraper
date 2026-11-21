"""Mars Vista API client. This is the only module that knows Mars Vista's URLs and JSON.

The v1 (NASA-compatible) endpoints are used on purpose. In testing, the v2 camera filter rejected valid
Perseverance camera names, and v2 paging with its default sort dropped a photo.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from ratelimit import RequestTracker

BASE_URL = "https://api.marsvista.dev"
PAGE_SIZE = 100

# Curiosity and Perseverance come from Mars Vista (checked against the old API's output by hash comparison).
# Spirit and Opportunity come from NASA's PDS archive (see pds.py), because Mars Vista's lists for them are
# incomplete. Their counts were checked against folders made with the old API.
SUPPORTED_ROVERS = ("Curiosity", "Perseverance", "Spirit", "Opportunity")


class ApiError(Exception):
    pass


@dataclass(frozen=True)
class Photo:
    id: int
    rover: str
    sol: int
    camera: str
    url: str


@dataclass(frozen=True)
class RoverInfo:
    name: str
    max_sol: int
    total_photos: int
    cameras: tuple[str, ...]


@dataclass(frozen=True)
class SolSummary:
    total_photos: int | None          # None when the source can't tell without listing the sol (NASA PDS)
    cameras: tuple[str, ...]


KEYRING_SERVICE = "RoverImageScraper"
KEYRING_USER = "marsvista-api-key"


def load_api_key() -> str:
    """Find the API key: the MARSVISTA_API_KEY environment variable, then (Windows) the user registry where `setx`
    stores it, then the key saved by the GUI in the system credential store."""
    key = os.environ.get("MARSVISTA_API_KEY")
    if not key and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "MARSVISTA_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        key = load_saved_api_key()
    if not key:
        raise ApiError("No API key: set the MARSVISTA_API_KEY environment variable, or save one in the GUI")
    return key


def load_saved_api_key() -> str | None:
    """The key saved by the GUI in the system credential store (Windows Credential Manager), if any."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:  # noqa: BLE001 - keyring not installed or no backend: treat as not saved
        return None


def save_api_key(key: str) -> None:
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # noqa: BLE001 - nothing saved
            pass


def compat_image_url(rover: str, url: str) -> str:
    """Return the image variant the old API linked to, so the bytes on disk match.

    Mars Vista's v1 img_src gives Perseverance's 800px browse image, but the old API (and the existing dump)
    used the 1200px one. Verified by SHA-256 against the dump.
    """
    if rover == "Perseverance" and url.endswith("_800.jpg"):
        return url[: -len("_800.jpg")] + "_1200.jpg"
    return url


class Cancelled(Exception):
    """Raised when the user stops a run while the client is waiting."""


class MarsVistaClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: float = 60,
                 max_retries: int = 5, sleep: Callable[[float], None] | None = None,
                 tracker: RequestTracker | None = None, cancel: threading.Event | None = None,
                 on_wait: Callable[[float, str], None] | None = None):
        """tracker counts requests against the quota; cancel lets a Stop interrupt waits; on_wait(seconds,
        reason) is called when a rate-limit wait of 5 s or more starts (the CLI prints it)."""
        self.session = session or requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.tracker = tracker or RequestTracker()
        self.cancel = cancel or threading.Event()
        self.on_wait = on_wait
        self._sleep_fn = sleep

    def _sleep(self, seconds: float) -> None:
        if self._sleep_fn is not None:          # tests
            self._sleep_fn(seconds)
        elif self.cancel.wait(seconds):
            raise Cancelled()
        if self.cancel.is_set():
            raise Cancelled()

    def _wait_for_quota(self, seconds: float, reason: str) -> None:
        self.tracker.begin_wait(seconds, reason)
        if self.on_wait and seconds >= 5:
            self.on_wait(seconds, reason)
        try:
            self._sleep(seconds)
        finally:
            self.tracker.end_wait()

    def _get(self, path: str, params: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if self.cancel.is_set():
                raise Cancelled()
            seconds, reason = self.tracker.seconds_until_allowed()
            if seconds > 0:
                self._wait_for_quota(seconds, reason)
            wait = 2 ** attempt
            try:
                self.tracker.record()
                r = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
            else:
                if r.status_code == 200:
                    return r.json()
                last_error = ApiError(f"HTTP {r.status_code} for {r.url}: {r.text[:300]}")
                if r.status_code == 429:
                    if attempt < self.max_retries:
                        self._wait_for_quota(min(_seconds_until_reset(r) or 60, 3600), "server asked us to wait")
                    continue
                if r.status_code < 500:
                    raise last_error
            if attempt < self.max_retries:
                self._sleep(min(wait, 3600))
        raise ApiError(f"Giving up after {self.max_retries + 1} attempts: {last_error}")

    def rovers(self) -> list[RoverInfo]:
        data = self._get("/api/v1/rovers")
        return [RoverInfo(name=r["name"], max_sol=int(r["max_sol"]), total_photos=int(r["total_photos"]),
                          cameras=tuple(c["name"] for c in r["cameras"]))
                for r in data["rovers"]]

    def manifest(self, rover: str) -> dict[int, SolSummary]:
        """Photo count and camera names for every sol that has photos, in one request."""
        data = self._get(f"/api/v1/manifests/{rover.lower()}")
        pm = data.get("photo_manifest", data)
        return {int(s["sol"]): SolSummary(int(s["total_photos"]), tuple(s["cameras"])) for s in pm["photos"]}

    def camera_counts(self, rover: str, sol: int) -> dict[str, int]:
        """Photo count per camera for one sol (v2 stats: one fast request, no paging). Only used to decide
        whether a sol needs a full listing; the v1 listing is the source of truth for what gets downloaded."""
        data = self._get("/api/v2/photos/stats",
                         {"rovers": rover.lower(), "group_by": "camera", "sol_min": sol, "sol_max": sol})
        return {g["key"]: int(g["count"]) for g in data["data"]["groups"]}

    def photos_for_sol(self, rover: str, sol: int) -> list[Photo]:
        """Every photo for one rover and sol, across all pages, deduplicated by ID and sorted by ID.

        Raises ApiError if the number of unique photos doesn't equal the API's total_count. A missing photo would
        shift the numbering of every later file in that camera folder, so a short list must never be used.
        """
        for _ in range(2):  # one retry in case the listing changed while paging
            by_id: dict[int, Photo] = {}
            total_count, page = None, 1
            while True:
                data = self._get(f"/api/v1/rovers/{rover.lower()}/photos",
                                 {"sol": sol, "page": page, "per_page": PAGE_SIZE})
                pagination = data.get("pagination") or {}
                if total_count is None:
                    total_count = int(pagination.get("total_count", 0))
                for p in data["photos"]:
                    by_id[int(p["id"])] = Photo(id=int(p["id"]), rover=rover, sol=int(p["sol"]),
                                                camera=p["camera"]["name"],
                                                url=compat_image_url(rover, p["img_src"]))
                if not data["photos"] or page >= int(pagination.get("total_pages", 1)):
                    break
                page += 1
            if len(by_id) == total_count:
                return sorted(by_id.values(), key=lambda p: p.id)
        raise ApiError(f"{rover} sol {sol}: got {len(by_id)} unique photos but API reports {total_count}")


def _seconds_until_reset(r: requests.Response) -> float | None:
    reset = r.headers.get("X-RateLimit-Reset")
    try:
        return max(1.0, float(reset) - time.time()) if reset else None
    except ValueError:
        return None
