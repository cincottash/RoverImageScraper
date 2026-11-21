"""Decide what is missing on disk. Pure logic: reads directory listings only, never file contents.

Layout (unchanged from the original scraper): <dest>/<Rover>/<sol>/<CAMERA>/<n>, no extension, n = 0, 1, 2...
The order of photos inside a camera folder does not matter (the user's rule), so completeness is judged by
counts: a folder with at least as many files as the API lists is complete. Curiosity folders from the old API
often hold more (NASA thumbnails, which Mars Vista doesn't list).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from .api import Photo


class Action(str, Enum):
    NEW = "new"            # camera folder missing: download all photos as 0..n-1
    COMPLETE = "complete"  # at least as many files on disk as the API lists: nothing to do
    FILL = "fill"          # fewer files on disk: add the photos whose bytes aren't already in the folder


@dataclass
class CameraPlan:
    rover: str
    sol: int
    camera: str
    folder: str
    photos: list[Photo]    # in Mars Vista ID order
    existing: int          # count of numbered files already in the folder
    next_index: int        # first free file number (max existing + 1, so nothing is ever renamed or reused)
    action: Action


def count_numbered(folder: str) -> tuple[int, int]:
    """(number of numbered files, next free number) for a camera folder. A missing folder is (0, 0)."""
    try:
        numbers = [int(e.name) for e in os.scandir(folder) if e.name.isdigit() and e.is_file()]
    except FileNotFoundError:
        return 0, 0
    return len(numbers), (max(numbers) + 1 if numbers else 0)


def sol_looks_complete(dest: str, rover: str, sol: int, total_photos: int, cameras: list[str]) -> bool:
    """Quick check from the manifest, with no API listing: every listed camera has a folder and the sol holds at
    least as many files as the API reports. Catches partial sols (which the old "folder exists" check missed).
    Fails safe: anything that doesn't look complete gets a full per-camera check."""
    sol_dir = os.path.join(dest, rover, str(sol))
    if not os.path.isdir(sol_dir):
        return False
    total = 0
    for camera in cameras:
        n, _ = count_numbered(os.path.join(sol_dir, camera))
        if n == 0:
            return False
        total += n
    return total >= total_photos


def group_by_camera(photos: list[Photo]) -> dict[str, list[Photo]]:
    groups: dict[str, list[Photo]] = {}
    for p in sorted(photos, key=lambda p: p.id):
        groups.setdefault(p.camera, []).append(p)
    return groups


def plan_sol(dest: str, rover: str, sol: int, photos: list[Photo]) -> list[CameraPlan]:
    plans = []
    for camera, cam_photos in sorted(group_by_camera(photos).items()):
        folder = os.path.join(dest, rover, str(sol), camera)
        existing, next_index = count_numbered(folder)
        if not os.path.isdir(folder):
            action = Action.NEW
        elif existing >= len(cam_photos):
            action = Action.COMPLETE
        else:
            action = Action.FILL
        plans.append(CameraPlan(rover, sol, camera, folder, cam_photos, existing, next_index, action))
    return plans
