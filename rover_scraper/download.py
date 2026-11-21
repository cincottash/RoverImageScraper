"""Image downloads that never overwrite: write to <file>.part, then move into place only if the name is free."""
from __future__ import annotations

import os
import time

import requests


class DownloadError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NasaStatus:
    """When NASA's image servers last pushed back (HTTP 429 or 5xx). Read by the GUI to show "NASA busy"."""

    def __init__(self):
        self.last_busy = 0.0

    def mark_busy(self) -> None:
        self.last_busy = time.time()

    def busy_recently(self, seconds: float = 30) -> bool:
        return time.time() - self.last_busy < seconds


NASA_STATUS = NasaStatus()


def fetch_bytes(session: requests.Session, url: str, timeout: float = 120, retries: int = 4,
                sleep=time.sleep) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("text/"):
                    raise DownloadError(f"{url} returned {ctype}, not an image")
                expected = r.headers.get("Content-Length")
                if expected is not None and r.headers.get("Content-Encoding") is None \
                        and int(expected) != len(r.content):
                    raise DownloadError(f"{url}: got {len(r.content)} bytes, expected {expected}")
                return r.content
            last = DownloadError(f"HTTP {r.status_code} for {url}", status=r.status_code)
            if r.status_code < 500 and r.status_code != 429:
                raise last
            NASA_STATUS.mark_busy()
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        if attempt < retries:
            sleep(2 ** attempt)
    raise DownloadError(f"Giving up on {url}: {last}")


def head_size(session: requests.Session, url: str, timeout: float = 60, retries: int = 2,
              sleep=time.sleep) -> int | None:
    """Size of the remote file from a HEAD request (no download), or None if the server doesn't say."""
    for attempt in range(retries + 1):
        try:
            r = session.head(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.headers.get("Content-Encoding") is None:
                length = r.headers.get("Content-Length")
                return int(length) if length is not None else None
            if r.status_code < 500 and r.status_code != 429:
                return None
        except (requests.ConnectionError, requests.Timeout):
            pass
        if attempt < retries:
            sleep(2 ** attempt)
    return None


def make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=pool_size)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def save_new_file(path: str, data: bytes) -> None:
    """Write data to path. Raises FileExistsError if path already exists; it never overwrites."""
    if os.path.lexists(path):
        raise FileExistsError(path)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        if os.name == "nt":
            os.rename(tmp, path)        # fails if path exists
        else:
            os.link(tmp, path)          # fails if path exists (os.rename would overwrite on POSIX)
            os.unlink(tmp)
    except FileExistsError:
        os.unlink(tmp)
        raise
