# RoverImageScraper: handoff notes (Nov 2026)

Summary of a review done in the Claude app, for continuing work in Claude Code / VS Code.

## Goals
1. Switch from the dead Mars Rover Photos API to **Mars Vista**.
2. Clean up the code.
3. Add a graphical user interface (GUI).

## What the project does today
Command-line tool: `python main.py <save_folder>`. It downloads every photo from the Mars rovers you pick into
`<save_folder>/<Rover>/<sol>/<CAMERA>/<n>`.

- `setup.py`: checks that the save folder exists, then fetches the rover list from
  `https://mars-photos.herokuapp.com/api/v1/rovers` and builds `Rover` objects (name, max_sol, total_photos,
  camera names, ID). It prints a numbered list, and you type the rovers you want as a run of digits (e.g. `013`).
  The input is checked for type, range and duplicates.
- `rover.py`: a small data class holding each rover's details.
- `main.py` (`getRoverPics`): for each rover, loops sol 0..max_sol. Each sol is requested once for the whole day and
  then once more per camera. Images are downloaded by shelling out to `curl`. Folders are made by shelling out to
  `mkdir`. A sol is skipped if its folder already exists, which is how resuming works.
- `Notable Pics`: the author's list of favourite photos (paths in the old layout), including several
  Spirit and Opportunity shots.

## The API problem
The Mars Rover Photos API (api.nasa.gov/mars-photos and its CORS mirror mars-photos.herokuapp.com) is archived.
The upstream project (corincerami/mars-photo-api) was archived on Oct 8, 2025. NASA has no direct replacement.

**Decision: use Mars Vista** (https://github.com/james-langridge/mars-vista-api). It covers Perseverance, Curiosity,
Opportunity and Spirit (around 1.5M photos).
- Base URL seen in its README: `https://api.marsvista.dev/api/v2/photos`
- It needs an API key, sent as the header `X-API-Key: <key>`. Keep the key out of git: load it from an environment
  variable or an untracked config file.
- Its parameters and response shape differ from the old API (it is v2, e.g. `include=rover,camera`, `per_page`).
  **Read its docs first** and confirm the filters for rover, sol and camera, the pagination, and the rate limits.
- It is run by one person (like the old API was), so keep it isolated behind its own module. The alternatives were
  NASA's undocumented raw-image JSON feeds (Curiosity and Perseverance only) and the PDS Imaging archive.

## Known issues to fix
1. **Windows breakage:** `sp.run(['mkdir', ...])` fails on Windows because `mkdir` is a cmd builtin, not a program.
   Use `os.makedirs(..., exist_ok=True)`. Replace the `curl` calls with streamed `requests` downloads.
2. **Resuming is broken for partial sols:** a sol is skipped once its folder exists, so an interrupted sol never
   finishes. Empty sols are requested again on every run. Track completion per photo (skip the file if it exists)
   or keep a manifest.
3. **Too many requests:** each sol is fetched once whole and then again per camera. Fetch once and group by camera.
4. Saved images have no file extension (`0`, `1`, ...). Keep the original extension, and consider keeping the
   original filename too.
5. Network calls have no timeouts, retries or error handling. Add rate-limit / backoff handling.
6. Running with no argument crashes with an `IndexError` on `sys.argv[1]`. `exit(0)` is used on errors; use a
   non-zero code or argparse.
7. `from setup import *` is how `main.py` gets `requests`. Use explicit imports.
8. The module name `setup.py` clashes with Python packaging conventions. Rename it.
9. `getDownloadedPictureCount` is an empty stub.
10. `__pycache__/` is committed and there is no `.gitignore`.
11. The working tree shows `requirements.txt`, `rover.py` and `setup.py` as modified, but the only difference is
    CRLF line endings (verified with `git diff --ignore-cr-at-eol`). Fix this with `.gitattributes` or
    `core.autocrlf` rather than committing the noise.
12. `requests==2.26.0` is old. Update the pinned version.

## Suggested structure
Split the code into layers so the GUI and the command-line version share one core:
- `api/marsvista.py`: the only code that knows Mars Vista's URLs and JSON. It returns plain `Photo` records
  (rover, sol, camera, earth_date, img_url, id).
- `downloader.py`: works out paths, skips files that already exist, downloads with streaming, and reports progress
  through callbacks or events (so a GUI can show progress bars), with support for cancelling.
- `cli.py`: argparse version of today's behaviour.
- `gui/`: toolkit still to be decided (Tkinter = no extra dependencies, PySide6 = nicer, or a small local web UI).
  Run downloads in a background thread so the window doesn't freeze.
- Settings (save folder, API key, chosen rovers, sol range, cameras) saved to a config file.

## HARD CONSTRAINT: output must be identical (added Nov 20, 2026; overrides items above)
There is an existing dump of about 200GB at `D:\Space\RoverPics` made with this program. The program will be run again
to add new photos to it. The internal logic can change, but **the output must not**:
- Layout `<Rover>/<sol>/<CAMERA>/<n>`, **no extension**, and `n` counting from 0 within each rover/sol/camera.
- The old API sorted results by `camera_id, id`, so `n` is the photo's position in the old database's ID order.
- This means known issue #4 (extensions, original filenames) is **dropped**, and so is naming files by nasa_id.
- Every version of the code in the git history used this same naming scheme.

Dump status: Curiosity sols 0-3784 (written May 2023), Perseverance 0-733 (Jan 2024), Spirit 1-2208,
Opportunity 1-5111. Curiosity has only the 7 legacy camera names. Perseverance has 16, including EDL_*.

**The dump is read-only** until compatibility has been shown by comparing file contents (hashes).

### Compatibility test plan
A. Read-only inventory of the dump: counts per rover/sol/camera, sizes, and hashes for sample sols.
B. Probe sample sols (early, middle, late, very large, Notable Pics) through Mars Vista v1 and v2. Download to
   scratch, hash, and compare with the dump. Find the photo set, sort order, image size and camera-name mapping
   that give a 100% match.
C. Count comparison across all sols, without downloading images. This finds partial sols and sols NASA changed later.
D. New code: golden tests from recorded responses, `--dry-run`, create-only writes, and a refusal to write into a
   folder that already holds data unless an explicit flag is given.
E. Rehearsal: download about 20 old sols into staging and compare byte for byte with the dump.
F. Download only new sols into staging, check them, then merge into `D:` with a script that never overwrites
   (dry run first, and the user approves).

Old partial or changed sols are left alone (the old behaviour) unless steps B and E show that numbering matches.

### Dev setup
Work on branch `refactor/marsvista`. Use a `.venv` made with Python 3.10 (`py -3.10 -m venv .venv`), then
`.venv\Scripts\pip install -r requirements-dev.txt`. The API key goes in the `MARSVISTA_API_KEY` environment variable.

## Next steps
1. Get a Mars Vista API key. Read its docs and confirm the endpoints, filters, pagination and rate limits.
2. Housekeeping: `.gitignore`, line-ending fix, remove `__pycache__` from git, update requirements. Work on a branch.
3. Write the Mars Vista client and the `Photo` model. Test it on one sol of one rover.
4. Refactor the downloader (fixes 1-6 above), keeping a working command-line version.
5. Pick a GUI toolkit and build it on top of the downloader: rover and camera checkboxes, sol range, folder picker,
   progress bar, start and cancel buttons, and perhaps a thumbnail preview.
6. Optional: update `Notable Pics` to the new folder layout, or add a favourites feature in the GUI.
