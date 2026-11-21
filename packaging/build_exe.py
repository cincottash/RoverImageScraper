"""Build the standalone desktop app with PyInstaller.

The result carries its own private copy of Python and the libraries, so it runs on a computer without Python and
downloads nothing to start. Each build goes into a new dated folder, so earlier builds are never overwritten or deleted:

  <out>/build-<date-time>/RoverImageScraper/RoverImageScraper.exe   folder version (fast start)
  <out>/build-<date-time>/RoverImageScraper-windows.zip             the folder version zipped, for sharing
  <out>/build-<date-time>/RoverImageScraper.exe                     single file (unpacks to a temp folder at launch)

Run it with a Python environment that has requirements.txt and PyInstaller installed:

  python packaging/build_exe.py --out D:/RoverImageScraper_build
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "rover_scraper")
NAME = "RoverImageScraper"

# Not used by the app; leaving them out keeps the bundle small.
EXCLUDES = ["tkinter", "unittest", "pydoc", "doctest", "pytest", "pyflakes", "setuptools", "pip"]


def build(onefile: bool, out: str) -> None:
    work = os.path.join(out, "work", "onefile" if onefile else "onedir")     # inside this build's own folder
    args = [sys.executable, "-m", "PyInstaller", os.path.join(SRC, "gui.py"),
            "--name", NAME, "--windowed", "--noconfirm", "--clean",
            "--onefile" if onefile else "--onedir",
            "--paths", SRC,
            "--add-data", f"{os.path.join(SRC, 'check.svg')}{os.pathsep}.",
            "--hidden-import", "PySide6.QtSvg",          # draws the tick in checkboxes (an SVG image)
            "--distpath", out, "--workpath", work, "--specpath", work]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    print(f"Building the {'single-file' if onefile else 'folder'} version...", flush=True)
    subprocess.run(args, check=True)


def zip_folder(out: str) -> str:
    folder = os.path.join(out, NAME)
    target = os.path.join(out, f"{NAME}-windows.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, files in os.walk(folder):
            for f in files:
                path = os.path.join(base, f)
                z.write(path, os.path.relpath(path, os.path.dirname(folder)))
    return target


def size_mb(path: str) -> float:
    if os.path.isfile(path):
        return os.path.getsize(path) / 1024 ** 2
    return sum(os.path.getsize(os.path.join(b, f)) for b, _, fs in os.walk(path) for f in fs) / 1024 ** 2


def main() -> int:
    p = argparse.ArgumentParser(description="Build the standalone RoverImageScraper app.")
    p.add_argument("--out", required=True, help="folder for builds; each build gets a new subfolder "
                                                "(about 300 MB each)")
    p.add_argument("--only", choices=["folder", "single"], help="build just one of the two forms")
    args = p.parse_args()
    out = os.path.join(os.path.abspath(args.out), time.strftime("build-%Y%m%d-%H%M%S"))
    os.makedirs(out)
    if args.only != "single":
        build(False, out)
        z = zip_folder(out)
        print(f"Folder version: {os.path.join(out, NAME)} ({size_mb(os.path.join(out, NAME)):.0f} MB)")
        print(f"Zip for sharing: {z} ({size_mb(z):.0f} MB)")
    if args.only != "folder":
        build(True, out)
        exe = os.path.join(out, NAME + ".exe")
        print(f"Single-file version: {exe} ({size_mb(exe):.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
