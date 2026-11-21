"""Build the desktop app as a single Windows .exe with PyInstaller.

The .exe carries its own private copy of Python and the libraries, so it runs on a computer without Python and
downloads nothing to start. Each build goes into a new dated folder, so earlier builds are never overwritten:

  <out>/build-<date-time>/RoverImageScraper.exe

PyInstaller's scratch files are kept in a temporary folder inside <out> that is removed when the build ends, so
the build folder holds only the .exe.

Run it with a Python environment that has requirements.txt and PyInstaller installed:

  python packaging/build_exe.py --out D:/RoverImageScraper_build
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "rover_scraper")
NAME = "RoverImageScraper"

# Not used by the app; leaving them out keeps the .exe small.
EXCLUDES = ["tkinter", "unittest", "pydoc", "doctest", "pytest", "pyflakes", "setuptools", "pip"]


def build(out: str) -> str:
    with tempfile.TemporaryDirectory(prefix="pyinstaller-work-", dir=out) as work:
        args = [sys.executable, "-m", "PyInstaller", os.path.join(SRC, "gui.py"),
                "--name", NAME, "--onefile", "--windowed", "--noconfirm", "--clean",
                "--paths", SRC,
                "--add-data", f"{os.path.join(SRC, 'check.svg')}{os.pathsep}.",
                "--hidden-import", "PySide6.QtSvg",          # draws the tick in checkboxes (an SVG image)
                "--distpath", out, "--workpath", work, "--specpath", work]
        for module in EXCLUDES:
            args += ["--exclude-module", module]
        print("Building the single-file app...", flush=True)
        subprocess.run(args, check=True)
    return os.path.join(out, NAME + ".exe")


def main() -> int:
    p = argparse.ArgumentParser(description="Build RoverImageScraper.exe.")
    p.add_argument("--out", required=True, help="folder for builds; each build gets a new subfolder (~40 MB)")
    args = p.parse_args()
    out = os.path.join(os.path.abspath(args.out), time.strftime("build-%Y%m%d-%H%M%S"))
    os.makedirs(out)
    exe = build(out)
    print(f"Built: {exe} ({os.path.getsize(exe) / 1024 ** 2:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
