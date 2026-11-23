"""Favorites (photos, sols and camera folders, each with an optional note) and saved flipbooks, kept in
favorites.json in the app data folder. Nothing here has to do with windows.

Everything is stored by rover, sol, camera and photo number, not by path, so it still applies after the photo
folder moves. The file is written whole after each change (to a temporary file first, then swapped in), so it's
never left half written.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

from api import SUPPORTED_ROVERS

FAVORITES_FILE = "favorites.json"
FAVORITES_VERSION = 1

FavKey = tuple  # (rover, sol, camera or None, number or None)


@dataclass
class Favorite:
    rover: str
    sol: int
    camera: str | None = None           # None: the whole sol
    number: int | None = None           # None: the whole camera folder (or sol)
    note: str = ""
    added: float = 0.0

    @property
    def key(self) -> FavKey:
        return (self.rover, self.sol, self.camera, self.number)

    @property
    def kind(self) -> str:
        return "photo" if self.number is not None else "camera" if self.camera else "sol"

    def label(self) -> str:
        """For lists: "Opportunity · Sol 1,912" or "Spirit · Sol 6 · PANCAM" or "… · PANCAM 248"."""
        text = f"{self.rover} · Sol {self.sol:,}"
        if self.camera:
            text += f" · {self.camera}" + (f" {self.number}" if self.number is not None else "")
        return text


@dataclass
class Flipbook:
    name: str
    rover: str
    sol: int
    camera: str
    first: int                          # photo numbers in the camera folder, both included
    last: int
    fps: float = 8.0
    loop: bool = True
    same_size: bool = True              # skip photos of another size than the first (thumbnails, sub-frames)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = 0.0
    changed: float = 0.0


def fav_key(rover: str, sol: int, camera: str | None = None, number: int | None = None) -> FavKey:
    return (rover, int(sol), camera or None, None if number is None else int(number))


class FavoritesStore:
    """path None: kept in memory only (tests, or no app data kept)."""

    def __init__(self, path: str | None):
        self.path = path
        self.favorites: list[Favorite] = []
        self.flipbooks: list[Flipbook] = []
        self.problem = ""                  # why the file couldn't be read, if it couldn't
        self._index: dict[FavKey, Favorite] = {}
        self._load()

    # ---- the file ---------------------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            favs = [Favorite(**{k: v for k, v in d.items() if k in Favorite.__dataclass_fields__})
                    for d in data.get("favorites", [])]
            books = [Flipbook(**{k: v for k, v in d.items() if k in Flipbook.__dataclass_fields__})
                     for d in data.get("flipbooks", [])]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            # Kept aside, never overwritten: the next save starts a new file.
            aside = f"{self.path}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(self.path, aside)
                self.problem = f"Favorites couldn't be read ({e}). The file was kept as {aside}."
            except OSError:
                self.problem = f"Favorites couldn't be read ({e})."
                self.path = None            # don't overwrite what couldn't be read or moved
            return
        self.favorites, self.flipbooks = favs, books
        self._reindex()

    def save(self) -> None:
        if not self.path:
            return
        data = {"version": FAVORITES_VERSION,
                "favorites": [asdict(f) for f in self.favorites],
                "flipbooks": [asdict(b) for b in self.flipbooks]}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _reindex(self) -> None:
        self._index = {f.key: f for f in self.favorites}

    # ---- favorites ---------------------------------------------------------------------------------------

    def get(self, key: FavKey) -> Favorite | None:
        return self._index.get(key)

    def is_favorite(self, key: FavKey) -> bool:
        return key in self._index

    def add(self, rover: str, sol: int, camera: str | None = None, number: int | None = None,
            note: str = "") -> Favorite:
        key = fav_key(rover, sol, camera, number)
        fav = self._index.get(key)
        if fav is None:
            fav = Favorite(*key, note=note, added=time.time())
            self.favorites.append(fav)
            self._index[key] = fav
            self.save()
        return fav

    def remove(self, key: FavKey) -> None:
        if key in self._index:
            self.favorites = [f for f in self.favorites if f.key != key]
            self._reindex()
            self.save()

    def set_note(self, key: FavKey, note: str) -> None:
        fav = self._index.get(key)
        if fav is not None and fav.note != note:
            fav.note = note
            self.save()

    def photos(self) -> list[Favorite]:
        return [f for f in self.favorites if f.kind == "photo"]

    def folders(self) -> list[Favorite]:
        return [f for f in self.favorites if f.kind != "photo"]

    # ---- flipbooks ---------------------------------------------------------------------------------------

    def flipbook(self, book_id: str) -> Flipbook | None:
        return next((b for b in self.flipbooks if b.id == book_id), None)

    def save_flipbook(self, book: Flipbook) -> Flipbook:
        """Adds it, or replaces the one with its id."""
        book.changed = time.time()
        if not book.created:
            book.created = book.changed
        for i, b in enumerate(self.flipbooks):
            if b.id == book.id:
                self.flipbooks[i] = book
                break
        else:
            self.flipbooks.append(book)
        self.save()
        return book

    def delete_flipbook(self, book_id: str) -> None:
        before = len(self.flipbooks)
        self.flipbooks = [b for b in self.flipbooks if b.id != book_id]
        if len(self.flipbooks) != before:
            self.save()


# ---- a list of favorites typed by hand ("Notable Pics") -------------------------------------------------

_ROVERS = {r.lower(): r for r in SUPPORTED_ROVERS}


def parse_favorites_list(text: str) -> tuple[list[Favorite], list[str]]:
    """Lines like "Perseverance/58/NAVCAM_LEFT/8   INGENUITY IN MID AIR": rover/sol[/camera[/number]], then an
    optional note. A leading folder name or slash is ignored. Returns (favorites, lines not understood)."""
    favs, problems, seen = [], [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(\S*?(?:%s)/\S*)\s*(.*)$" % "|".join(SUPPORTED_ROVERS), line, re.IGNORECASE)
        if not m:
            problems.append(f"{line}  (no rover name)")
            continue
        path, note = m.group(1), m.group(2).strip()
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        start = next((i for i, p in enumerate(parts) if p.lower() in _ROVERS), None)
        parts = parts[start:]
        rover = _ROVERS[parts[0].lower()]
        if len(parts) < 2 or not parts[1].isdigit():
            problems.append(f"{line}  (no sol number after the rover)")
            continue
        sol = int(parts[1])
        camera = number = None
        if len(parts) >= 3:
            if parts[2].isdigit():
                problems.append(f"{line}  (a number where the camera should be)")
                continue
            camera = parts[2]
        if len(parts) >= 4:
            n = re.sub(r"\.\w+$", "", parts[3])          # "8.jpg" or "8"
            if not n.isdigit():
                problems.append(f"{line}  (the photo number isn't a number)")
                continue
            number = int(n)
        if len(parts) > 4:
            problems.append(f"{line}  (more parts than rover/sol/camera/number)")
            continue
        key = fav_key(rover, sol, camera, number)
        if key in seen:
            problems.append(f"{line}  (listed twice)")
            continue
        seen.add(key)
        favs.append(Favorite(*key, note=note))
    return favs, problems
