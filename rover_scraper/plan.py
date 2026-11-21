"""Decide, for each rover/sol/camera, what is missing on disk. Pure logic: reads directory listings only.

Layout (unchanged from the original scraper): <dest>/<Rover>/<sol>/<CAMERA>/<n>, no extension, where n counts
from 0 in Mars Vista ID order within the camera.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from .api import Photo


class Action(str, Enum):
    NEW = "new"            # camera folder missing: download all photos as 0..n-1
    COMPLETE = "complete"  # same number of files as the API lists: nothing to do
    TOPUP = "topup"        # fewer files: verify the existing ones match the start of the list, then add the rest
    SKIP_EXTRA = "extra"   # more files than the API lists (e.g. old Curiosity thumbnails): leave alone, report
    CONFLICT = "conflict"  # folder contents aren't a clean 0..k-1 run of files: leave alone, report


@dataclass
class CameraPlan:
    rover: str
    sol: int
    camera: str
    folder: str
    photos: list[Photo]          # in output order: photos[i] is saved as file "i"
    existing: int                # count of numbered files already in the folder
    action: Action
    reason: str = ""
    sizes: dict[int, int] = field(default_factory=dict)  # sizes of existing numbered files

    @property
    def missing(self) -> list[tuple[int, Photo]]:
        if self.action in (Action.NEW, Action.TOPUP):
            return list(enumerate(self.photos))[self.existing:]
        return []


def group_by_camera(photos: list[Photo]) -> dict[str, list[Photo]]:
    groups: dict[str, list[Photo]] = {}
    for p in sorted(photos, key=lambda p: p.id):
        groups.setdefault(p.camera, []).append(p)
    return groups


def plan_sol(dest: str, rover: str, sol: int, photos: list[Photo]) -> list[CameraPlan]:
    plans = []
    for camera, cam_photos in sorted(group_by_camera(photos).items()):
        folder = os.path.join(dest, rover, str(sol), camera)
        plans.append(plan_camera(folder, rover, sol, camera, cam_photos))
    return plans


def plan_camera(folder: str, rover: str, sol: int, camera: str, photos: list[Photo]) -> CameraPlan:
    def make(action: Action, existing: int = 0, reason: str = "", sizes=None) -> CameraPlan:
        return CameraPlan(rover, sol, camera, folder, photos, existing, action, reason, sizes or {})

    if not os.path.isdir(folder):
        return make(Action.NEW)

    names = [n for n in os.listdir(folder) if not n.endswith(".part")]
    numbered = sorted(int(n) for n in names if n.isdigit())
    others = [n for n in names if not n.isdigit()]
    if others:
        return make(Action.CONFLICT, len(numbered), f"unexpected entries: {others[:5]}")
    if numbered != list(range(len(numbered))):
        return make(Action.CONFLICT, len(numbered), "numbered files are not a contiguous 0..k-1 run")
    if any(not os.path.isfile(os.path.join(folder, str(n))) for n in numbered):
        return make(Action.CONFLICT, len(numbered), "a numbered entry is not a file")

    k, n = len(numbered), len(photos)
    sizes = {i: os.path.getsize(os.path.join(folder, str(i))) for i in numbered}
    if k == n:
        return make(Action.COMPLETE, k, sizes=sizes)
    if k > n:
        return make(Action.SKIP_EXTRA, k, f"{k} files on disk, API lists {n}")
    return make(Action.TOPUP, k, f"{k} of {n} on disk", sizes=sizes)
