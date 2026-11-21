"""Mars Vista API client. This is the only module that knows Mars Vista's URLs and JSON.

The v1 (NASA-compatible) endpoints are used on purpose. In testing, the v2 camera filter rejected valid
Perseverance camera names, and v2 paging with its default sort dropped a photo. See HANDOFF.md.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

import requests

BASE_URL = "https://api.marsvista.dev"
PAGE_SIZE = 100

# Rovers whose Mars Vista data has been checked against the old API's output by hash comparison.
# Spirit and Opportunity image links from Mars Vista were broken or did not match, so they are excluded.
VERIFIED_ROVERS = ("Curiosity", "Perseverance")


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
    total_photos: int
    cameras: tuple[str, ...]


def load_api_key() -> str:
    """Read MARSVISTA_API_KEY from the environment. On Windows, fall back to the user registry, where `setx`
    stores it, so the key works in shells that were opened before it was set."""
    key = os.environ.get("MARSVISTA_API_KEY")
    if not key and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "MARSVISTA_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise ApiError("No API key: set the MARSVISTA_API_KEY environment variable")
    return key


def compat_image_url(rover: str, url: str) -> str:
    """Return the image variant the old API linked to, so the bytes on disk match.

    Mars Vista's v1 img_src gives Perseverance's 800px browse image, but the old API (and the existing dump)
    used the 1200px one. Verified by SHA-256 against the dump.
    """
    if rover == "Perseverance" and url.endswith("_800.jpg"):
        return url[: -len("_800.jpg")] + "_1200.jpg"
    return url


class MarsVistaClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, timeout: float = 60,
                 max_retries: int = 5, sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def _get(self, path: str, params: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = 2 ** attempt
            try:
                r = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
            else:
                if r.status_code == 200:
                    return r.json()
                last_error = ApiError(f"HTTP {r.status_code} for {r.url}: {r.text[:300]}")
                if r.status_code == 429:
                    wait = _seconds_until_reset(r) or 60
                elif r.status_code < 500:
                    raise last_error
            if attempt < self.max_retries:
                self.sleep(min(wait, 3600))
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
