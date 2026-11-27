"""Make sure a build runs in a Python environment that has everything it needs, setting one up if not.

If the Python running the build script already has the app's libraries and PyInstaller (as on the release
builds, which install them first), the build just runs. Otherwise a private environment is created once in
<out>/build-venv, requirements.txt and packaging/requirements-build.txt are installed into it, and the build script
restarts itself inside it. Later builds into the same <out> reuse that environment, and update it when the
requirements change.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = [os.path.join(ROOT, "requirements.txt"), os.path.join(ROOT, "packaging", "requirements-build.txt")]
NEEDED = ("PyInstaller", "PySide6", "requests", "keyring", "PIL")
MIN_PYTHON = (3, 10)
_MARK = "ROVER_BUILD_ENV"          # set while the build reruns inside the private environment, to avoid a loop


def _missing() -> list[str]:
    return [m for m in NEEDED if importlib.util.find_spec(m) is None]


def _requirements_hash() -> str:
    h = hashlib.sha256()
    for path in REQUIREMENTS:
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def ensure(out: str) -> None:
    """Return if this Python can build; otherwise set up <out>/build-venv and rerun the script there (not
    returning). Exits with a message if this Python is too old or the setup fails."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(f"The build needs Python {'.'.join(map(str, MIN_PYTHON))} or newer; this is "
                 f"{sys.version.split()[0]}.")
    missing = _missing()
    if not missing:
        return
    if os.environ.get(_MARK):
        sys.exit(f"Still missing after setting up the build environment: {', '.join(missing)}")
    venv = os.path.join(out, "build-venv")
    python = os.path.join(venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv, "bin", "python")
    stamp = os.path.join(venv, "requirements.sha256")
    wanted = _requirements_hash()
    try:
        if not os.path.exists(python):
            print(f"This Python is missing {', '.join(missing)}. Setting up a build environment in {venv} "
                  f"(once; a few minutes)...", flush=True)
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        current = open(stamp, encoding="utf-8").read().strip() if os.path.exists(stamp) else ""
        if current != wanted:
            print("Installing the build's libraries...", flush=True)
            subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"],
                           check=True)
            args = [python, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
            for path in REQUIREMENTS:
                args += ["-r", path]
            subprocess.run(args, check=True)
            with open(stamp, "w", encoding="utf-8") as f:
                f.write(wanted + "\n")
    except (subprocess.CalledProcessError, OSError) as e:
        sys.exit(f"Couldn't set up the build environment in {venv}: {e}\n"
                 f"Delete that folder and try again, or install the requirements yourself (see the README).")
    print(f"Running the build with {python}", flush=True)
    env = dict(os.environ, **{_MARK: "1"})
    sys.exit(subprocess.run([python, os.path.abspath(sys.argv[0]), *sys.argv[1:]], env=env).returncode)
