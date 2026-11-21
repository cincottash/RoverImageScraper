"""Image downloads that never overwrite: write to <file>.part, then move into place only if the name is free."""
from __future__ import annotations

import os
import time

import requests


class DownloadError(Exception):
    pass


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
            last = DownloadError(f"HTTP {r.status_code} for {url}")
            if r.status_code < 500 and r.status_code != 429:
                raise last
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        if attempt < retries:
            sleep(2 ** attempt)
    raise DownloadError(f"Giving up on {url}: {last}")


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
