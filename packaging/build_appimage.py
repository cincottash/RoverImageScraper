"""Build the desktop app as a Linux AppImage: one file that runs on most Linux distributions without installing.

Like the Windows .exe it carries its own private copy of Python and the libraries. Run it on Linux (x86-64), with
a Python environment that has requirements.txt and packaging/requirements-build.txt installed, and appimagetool
(https://github.com/AppImage/appimagetool) on the PATH or given with --appimagetool:

  python packaging/build_appimage.py --out ~/builds

Each build goes into a new dated folder that ends up holding only the AppImage:

  <out>/build-<date-time>/RoverImageScraper-x86_64.AppImage

The AppImage runs on distributions at least as new as the one it was built on (the release build uses
Ubuntu 22.04, so Ubuntu 22.04+, Debian 12+, Fedora 36+ and similar).
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_exe import EXCLUDES, NAME, SRC  # noqa: E402  (shared with the Windows build)
from make_icon import write_png  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_NAME = "rover-image-scraper"

DESKTOP_ENTRY = f"""[Desktop Entry]
Type=Application
Name=Rover Image Scraper
Comment=Download raw photos from NASA's Mars rovers
Exec={NAME}
Icon={ICON_NAME}
Categories=Science;Education;
Terminal=false
"""

APP_RUN = f"""#!/bin/sh
# Starts the bundled program from wherever the AppImage is mounted.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/{NAME}/{NAME}" "$@"
"""


def write_file(path: str, text: str, executable: bool = False) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build(out: str, appimagetool: str) -> str:
    with tempfile.TemporaryDirectory(prefix="appimage-work-", dir=out) as work:
        dist = os.path.join(work, "dist")
        icon_svg = os.path.join(HERE, "icon.svg")
        args = [sys.executable, "-m", "PyInstaller", os.path.join(SRC, "gui.py"),
                "--name", NAME, "--onedir", "--windowed", "--noconfirm", "--clean",
                "--paths", SRC,
                "--add-data", f"{os.path.join(SRC, 'check.svg')}{os.pathsep}.",
                "--add-data", f"{os.path.join(SRC, 'check_dark.svg')}{os.pathsep}.",
                "--add-data", f"{icon_svg}{os.pathsep}.",
                "--hidden-import", "PySide6.QtSvg",
                "--distpath", dist, "--workpath", os.path.join(work, "build"), "--specpath", work]
        for module in EXCLUDES:
            args += ["--exclude-module", module]
        print("Building the program folder...", flush=True)
        subprocess.run(args, check=True)

        print("Assembling the AppImage...", flush=True)
        appdir = os.path.join(work, "AppDir")
        program = os.path.join(appdir, "usr", "lib", NAME)
        shutil.copytree(os.path.join(dist, NAME), program, symlinks=True)
        write_file(os.path.join(appdir, "AppRun"), APP_RUN, executable=True)
        write_file(os.path.join(appdir, f"{ICON_NAME}.desktop"), DESKTOP_ENTRY)
        write_png(os.path.join(appdir, f"{ICON_NAME}.png"), 256)
        apps = os.path.join(appdir, "usr", "share", "applications")
        icons = os.path.join(appdir, "usr", "share", "icons", "hicolor", "256x256", "apps")
        os.makedirs(apps)
        os.makedirs(icons)
        shutil.copy(os.path.join(appdir, f"{ICON_NAME}.desktop"), apps)
        shutil.copy(os.path.join(appdir, f"{ICON_NAME}.png"), icons)

        target = os.path.join(out, f"{NAME}-x86_64.AppImage")
        subprocess.run([appimagetool, appdir, target], check=True, env=dict(os.environ, ARCH="x86_64"))
    return target


def main() -> int:
    p = argparse.ArgumentParser(description="Build RoverImageScraper-x86_64.AppImage (Linux).")
    p.add_argument("--out", required=True, help="folder for builds; each build gets a new subfolder (~60 MB)")
    p.add_argument("--appimagetool", default=shutil.which("appimagetool") or "appimagetool",
                   help="path to appimagetool (default: the one on the PATH)")
    args = p.parse_args()
    if not sys.platform.startswith("linux"):
        print("An AppImage has to be built on Linux.", file=sys.stderr)
        return 2
    out = os.path.join(os.path.abspath(os.path.expanduser(args.out)), time.strftime("build-%Y%m%d-%H%M%S"))
    os.makedirs(out)
    target = build(out, args.appimagetool)
    print(f"Built: {target} ({os.path.getsize(target) / 1024 ** 2:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
