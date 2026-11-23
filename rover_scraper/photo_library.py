"""What's in a photo folder, for the Photos page: rovers, sols, cameras and photo files, plus the details that can be
worked out without asking anyone (the camera's full name, the Earth date a sol fell on).

The folder layout is the one the downloader writes:  <folder>/<rover>/<sol>/<camera>/<n>.<ext>
Nothing here writes to the photo folder.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from api import SUPPORTED_ROVERS
from plan import photo_number

# Full camera names, by the folder name the downloader uses.
CAMERA_NAMES = {
    # Curiosity
    "FHAZ": "Front Hazard Avoidance Camera", "RHAZ": "Rear Hazard Avoidance Camera", "MAST": "Mast Camera",
    "CHEMCAM": "Chemistry and Camera Complex", "MAHLI": "Mars Hand Lens Imager", "MARDI": "Mars Descent Imager",
    "NAVCAM": "Navigation Camera",
    # Perseverance
    "EDL_RUCAM": "Rover Up-Look Camera", "EDL_RDCAM": "Rover Down-Look Camera",
    "EDL_DDCAM": "Descent Stage Down-Look Camera", "EDL_PUCAM1": "Parachute Up-Look Camera A",
    "EDL_PUCAM2": "Parachute Up-Look Camera B", "NAVCAM_LEFT": "Navigation Camera, Left",
    "NAVCAM_RIGHT": "Navigation Camera, Right", "MCZ_LEFT": "Mastcam-Z, Left", "MCZ_RIGHT": "Mastcam-Z, Right",
    "FRONT_HAZCAM_LEFT_A": "Front Hazard Avoidance Camera, Left",
    "FRONT_HAZCAM_RIGHT_A": "Front Hazard Avoidance Camera, Right",
    "REAR_HAZCAM_LEFT": "Rear Hazard Avoidance Camera, Left", "REAR_HAZCAM_RIGHT": "Rear Hazard Avoidance Camera, Right",
    "SKYCAM": "MEDA SkyCam", "SHERLOC_WATSON": "SHERLOC WATSON Camera", "SHERLOC_ACI": "SHERLOC Context Imager",
    "SUPERCAM_RMI": "SuperCam Remote Micro-Imager", "LCAM": "Lander Vision System Camera",
    "PIXL_MCC": "PIXL Micro-Context Camera", "CACHECAM": "CacheCam",
    # Spirit and Opportunity (MINITES is the folder name the old photo list used for the Microscopic Imager)
    "PANCAM": "Panoramic Camera", "MINITES": "Microscopic Imager", "ENTRY": "Entry, Descent and Landing Camera",
}


def camera_name(code: str) -> str:
    return CAMERA_NAMES.get(code, code)


# ---- the Earth date of a sol ---------------------------------------------------------------------------------

SOL_SECONDS = 88775.244          # one Mars day
# A known moment per rover: (sol, UTC time, Mars local time in hours at that moment). Landings for Curiosity and
# Perseverance; for Spirit and Opportunity a photo's time from NASA's archive (their landing day was sol 1).
_ANCHORS = {
    "Curiosity": (0, datetime(2012, 8, 6, 5, 17, 57, tzinfo=timezone.utc), 15.0),
    "Perseverance": (0, datetime(2021, 2, 18, 20, 55, 0, tzinfo=timezone.utc), 15.9),
    "Spirit": (100, datetime(2004, 4, 14, 16, 31, 47, tzinfo=timezone.utc), 9.58),
    "Opportunity": (3000, datetime(2012, 7, 2, 12, 54, 44, tzinfo=timezone.utc), 11.87),
}


def sol_noon_utc(rover: str, sol: int) -> datetime | None:
    """The UTC time of midday on a sol at the rover's place (to within about an hour)."""
    anchor = _ANCHORS.get(rover)
    if anchor is None:
        return None
    a_sol, a_utc, a_hours = anchor
    return a_utc + timedelta(seconds=((sol - a_sol) + (12 - a_hours) / 24) * SOL_SECONDS)


def earth_date(rover: str, sol: int) -> date | None:
    """The Earth date (UTC) of midday on a sol. A sol is 24 h 40 min long, so photos taken early or late in it can
    have the day before or after."""
    noon = sol_noon_utc(rover, sol)
    return noon.date() if noon else None


# ---- the folder ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PhotoFile:
    path: str
    rover: str
    sol: int
    camera: str
    number: int

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def _subfolders(path: str) -> list[str]:
    try:
        with os.scandir(path) as it:
            return [e.name for e in it if e.is_dir()]
    except OSError:
        return []


def rovers_in(folder: str) -> list[str]:
    """Rovers that have a folder here, in the app's usual order."""
    present = set(_subfolders(folder)) if folder else set()
    return [r for r in SUPPORTED_ROVERS if r in present]


def sols_in(folder: str, rover: str) -> list[int]:
    """Sol numbers that have a folder, in order."""
    return sorted(int(n) for n in _subfolders(os.path.join(folder, rover)) if n.isdigit())


def _photo_entries(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return [e for e in it if photo_number(e.name) is not None and e.is_file()]
    except OSError:
        return []


def cameras_in(folder: str, rover: str, sol: int) -> list[tuple[str, int]]:
    """(camera, number of photos) for each camera folder of a sol that holds photos, sorted by camera."""
    sol_path = os.path.join(folder, rover, str(sol))
    out = []
    for cam in sorted(_subfolders(sol_path)):
        n = len(_photo_entries(os.path.join(sol_path, cam)))
        if n:
            out.append((cam, n))
    return out


def sol_photo_count(folder: str, rover: str, sol: int) -> int:
    return sum(n for _, n in cameras_in(folder, rover, sol))


def photos_in(folder: str, rover: str, sol: int, camera: str | None = None) -> list[PhotoFile]:
    """The photos of one camera, or of every camera of the sol, in camera then number order."""
    sol_path = os.path.join(folder, rover, str(sol))
    cams = [camera] if camera else sorted(_subfolders(sol_path))
    out = []
    for cam in cams:
        entries = _photo_entries(os.path.join(sol_path, cam))
        entries.sort(key=lambda e: photo_number(e.name))
        out += [PhotoFile(e.path, rover, sol, cam, photo_number(e.name)) for e in entries]
    return out


def find_photos(folder: str, keys: list[tuple[str, int, str, int]]) -> tuple[list[PhotoFile], int]:
    """The photos named by (rover, sol, camera, number), in that order, reading each camera folder once.
    Returns (photos found, how many weren't in the folder)."""
    by_folder: dict[tuple[str, int, str], dict[int, os.DirEntry]] = {}
    found, missing = [], 0
    for rover, sol, camera, number in keys:
        k = (rover, sol, camera)
        if k not in by_folder:
            entries = _photo_entries(os.path.join(folder, rover, str(sol), camera))
            by_folder[k] = {photo_number(e.name): e for e in entries}
        e = by_folder[k].get(number)
        if e is None:
            missing += 1
        else:
            found.append(PhotoFile(e.path, rover, sol, camera, number))
    return found, missing


def photo_from_path(folder: str, path: str) -> PhotoFile | None:
    """The PhotoFile for a path inside the folder's <rover>/<sol>/<camera>/<n>.<ext> layout, or None."""
    try:
        rel = os.path.relpath(path, folder)
    except ValueError:              # another drive
        return None
    parts = rel.replace("\\", "/").split("/")
    if len(parts) != 4 or parts[0] not in SUPPORTED_ROVERS or not parts[1].isdigit():
        return None
    number = photo_number(parts[3])
    if number is None:
        return None
    return PhotoFile(os.path.join(folder, *parts), parts[0], int(parts[1]), parts[2], number)
