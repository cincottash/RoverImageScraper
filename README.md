# Rover Image Scraper

Downloads the raw photos taken by NASA's four Mars rovers (**Curiosity**, **Perseverance**, **Spirit** and
**Opportunity**) into a tidy folder on your computer. Run it again later and it fetches only what's new or still
missing, so it can keep a growing collection up to date.

![The Rover Image Scraper desktop app after downloading a few sols of Perseverance and Spirit](.github/screenshot.png)

It's free and open source (see [License](#license)).

## Where the photos come from

- **Curiosity and Perseverance** (both still exploring Mars): the free
  [Mars Vista API](https://github.com/james-langridge/mars-vista-api), which lists every raw photo NASA has
  published for them. The photos themselves download straight from NASA's image servers. Mars Vista needs a free
  API key (see [Getting an API key](#getting-an-api-key)).
- **Spirit and Opportunity** (missions ended in 2010 and 2018): NASA's
  [Planetary Data System (PDS) Imaging Node](https://pds-imaging.jpl.nasa.gov/), NASA's permanent archive of
  mission data. No key is needed for these two.

A *sol* is a day on Mars, counted from the day the rover landed. Photos are organised by rover, sol and camera.

## Download and run

The easiest way is the ready-made app from the
[latest release](https://github.com/cincottash/RoverImageScraper/releases/latest). It needs no Python and installs
nothing.

- **Windows:** download **RoverImageScraper.exe** and double-click it. Windows may show *"Windows protected your
  PC"* the first time, because the program isn't code-signed: click *More info*, then *Run anyway*.
- **Linux:** download **RoverImageScraper-x86_64.AppImage**, make it executable, and run it:
  ```sh
  chmod +x RoverImageScraper-x86_64.AppImage
  ./RoverImageScraper-x86_64.AppImage
  ```
  (Or right-click it, tick *Allow executing file as program* in *Properties*, and double-click.)
- **macOS:** run it from the source code (see [Running from the source code](#running-from-the-source-code)).

More about the two app files: [Standalone app](#standalone-app).

## Getting an API key

Curiosity and Perseverance need a free Mars Vista API key. Sign up with an email address at
[marsvista.dev](https://marsvista.dev), then paste the key into the app's *API key* box. The app keeps it in your
system's secure credential store (Windows Credential Manager, the macOS Keychain, or your Linux desktop's
keyring), not in a file. Keep the key private: anyone with it can use your Mars Vista quota.

## Using the desktop app

**First run:** the app asks where to keep its own files (the *app data folder*: settings, scan results and a few
counters, a few megabytes) and, optionally, where to save photos. Each can be on any drive. See
[The app data folder](#the-app-data-folder).

Then:

1. Choose a **photo folder** and paste your **API key**. If the folder doesn't exist yet, the app offers to
   create it.
2. Tick the **rovers** you want. Open *Cameras* under a rover to leave out cameras you don't want.
3. Click **Quick scan** to see what's already in the folder and how much is available online.
4. Optionally click **Preview** for an estimate of how many photos and gigabytes a download would add, and where
   they'll go (with the free space on that drive).
5. Click **Start**. If Start is greyed out, the line next to it says what's missing.

### The window

- **Options**
  - **Check existing sols for missing photos:** when ticked, the run goes through every sol, filling in anything
    missing from sols you already have (slower). Untick it to download only sols newer than the newest one in your
    folder.
  - **Sols from / to:** limit the run to a range of sols. Leave both blank for everything.
  - **Limit downloads to N photos per minute, hour or day:** an optional cap, for example to go easy on a shared
    connection. With 500 per hour, no 60-minute stretch has more than 500 photo downloads; when the cap is
    reached an amber banner shows when downloading resumes, and it carries on by itself. Typing a number or
    choosing a period turns the limit on; untick the box to turn it off.
  - **Advanced > Parallel downloads:** how many photos download at once (1–16, default 6).
- **Library status:** for each rover, what's in your folder (sols, photos, size) and what's available online (the
  latest sol, and how many sols and photos you don't have yet). For Spirit and Opportunity the number of photos
  isn't known until each sol is looked at, so only sols are shown. *Full check* also looks for gaps inside sols you
  already have; it makes about one Mars Vista request per sol, so it asks first. If you've downloaded since the
  last scan, a note says the numbers are out of date until you scan again.
- **Progress:**
  - *Photos downloaded* and *Data downloaded* in this run.
  - *Already had*: photos that were already in your folder (in sols that were already complete, or found while
    filling in a sol).
  - *Unavailable at NASA*: photos NASA's image servers refuse to send (HTTP 403 or 404), usually older images
    NASA no longer serves. They can't be downloaded, so the app remembers them and skips them on later runs
    instead of counting them as missing. *Download > Retry photos NASA refused* tries them all again.
  - *Speed*, *Photo rate* and *Average speed*, *Errors*, *Elapsed*, and *Time left*: an estimate based on how
    fast sols went by over the last two minutes. It can change noticeably when the run moves from sols you
    already have (fast) to new ones (slower, since they're downloaded).
- **Mars Vista API usage:** requests used this hour and today, counted by the app (Mars Vista allows 10,000 per
  hour and 100,000 per day). Near a limit, an amber banner shows a countdown and the download carries on by
  itself afterwards. *NASA archive* counts requests for Spirit and Opportunity, which have no limit.
- **Activity:** a running log of what the app is doing. *Hide* removes it from the window; *Show activity* at the
  bottom right brings it back. *View > Detailed activity log* adds a line for every sol checked and every photo
  saved or skipped.

### Pausing, stopping and changing settings

- **Pause** finishes the photos in progress and remembers where it was while the app stays open. **Resume**
  carries on from that sol without re-checking the ones before it.
- **Stop** (or closing the app) ends the run. Finished photos are always kept, and the next Start begins from the
  beginning, skipping complete sols quickly.
- **During a download** you can change *Parallel downloads* and the *download limit*; they take effect right away
  (parallel downloads from the next camera folder). Rovers, cameras and the sol range are locked while
  downloading. If you change them while paused, Resume asks whether to carry on with the original choices or
  start a new download with the new ones.

### Menus

- **File:** choose or open the photo folder; **Settings** (move the app data folder, reset the download settings
  to their defaults); quit.
- **Download:** Start, Pause and Stop, Quick scan, Full check and Preview (the same as the buttons), and *Retry
  photos NASA refused* on the next run.
- **View:** the theme (dark by default; also match the operating system, light, high contrast, and about sixty
  more: space-themed ones made for this app, plus light and dark classics from popular editor color schemes),
  units for data and speed and how long *Speed* is averaged over, show or hide the activity log, the detailed
  activity log, and *Reset view options*.
- **Help:** the Mars Vista website, open the app data folder, report a problem (opens the project's GitHub
  issues), and *About*.

## Where your photos go

```
<photo folder>/
  Curiosity/
    3786/
      MAST/
        0.jpg
        1.jpg
        ...
      NAVCAM/
        0.jpg
        ...
  Perseverance/
    ...
```

The layout is `<photo folder>/<Rover>/<sol>/<CAMERA>/<n>.<type>`. Within each camera folder, photos are numbered
0, 1, 2… in the order the source lists them. The extension (almost always `.jpg`) comes from the file's own
contents, never guessed from its name or web address, so your file manager can show previews. A file whose type
isn't recognized is saved as just `<n>`. Files named just `<n>`, as older versions saved them, count toward a
folder's total too, and the app never renames them.

## How it decides what to download

- It never overwrites, renames or deletes files in your photo folder. It only adds new ones.
- For each sol it compares the number of photos on disk with the number the source lists, camera by camera.
  Sols that match are skipped quickly. A camera folder with too few photos gets only the ones it's missing,
  numbered after the highest existing file.
- A run that's stopped keeps every finished photo and leaves no half-written files. Start again to continue.
- Mars Vista requests are counted so the app slows down before a limit and waits if the server asks it to. Photo
  downloads and everything for Spirit and Opportunity don't count toward Mars Vista's limits.
- For Spirit and Opportunity, each sol's photos are read from NASA's archive folders: the full-frame,
  downsampled and sub-frame images (thumbnails and calibration images are left out). Sols without photos are
  answered in a fraction of a second.
- Photos NASA refuses to serve are remembered in the app data folder (`unavailable.json`), so they aren't counted
  as missing on every run.

### Good to know

- **Spirit and Opportunity folders that came from somewhere else are left as they are.** If a Spirit or
  Opportunity camera folder holds photos that weren't downloaded from NASA's archive (for example copies from
  NASA's public image gallery, which are brightened and a slightly different size), the app can't tell which
  archive photos it already contains. Rather than add copies of photos you have, it leaves such folders alone
  (logged as `KEEP`). Sols and cameras missing entirely are still downloaded, and folders the app started itself
  are completed as usual.
- **Extra files in a folder count as photos.** The app counts every numbered file in a camera folder. If a folder
  contains extra numbered files from another tool (thumbnails, for example), it can look complete even though a
  photo is missing.

## The app data folder

The app keeps a few small files of its own (settings, scan results, Mars Vista request counts and the list of
photos NASA refused) in its *app data folder*, never in your photo folder. You choose it on first run; it can be
on any drive. The suggested place is `%LOCALAPPDATA%\RoverImageScraper\` on Windows and
`~/.local/share/RoverImageScraper/` on macOS and Linux. Which folder you chose is remembered for your user account
(in the Windows registry under `HKCU\Software\RoverImageScraper`, or in `~/.config/RoverImageScraper/data_folder`
on macOS and Linux), and the command line uses the same folder.

To move it later, use *File > Settings > Change app data folder*: the files are copied (the originals are left
where they were) and the app restarts.

To keep nothing between launches, tick *Don't keep app data* in that window. Settings, scan results and counters
then live in a temporary folder that's deleted when the app closes, and every launch starts fresh. The
trade-off: request counts restart at zero each time, so the app can't keep you under Mars Vista's limits across
restarts, and refused photos are tried again on each run. Downloaded photos are always kept.

## Command line

The same downloads can run from a terminal, which is handy for scheduled updates. It runs from the source code
(see [Running from the source code](#running-from-the-source-code)). Commands are run from the project folder; in
the examples, replace `C:\path\to\MarsPhotos` with your photo folder (on macOS or Linux, a path such as
`~/MarsPhotos`). The folder must already exist.

```bat
:: First download into an empty folder (everything for all four rovers: well over a million photos and
:: several hundred gigabytes)
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos"

:: Preview what would be downloaded, without writing anything
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --dry-run

:: Update a folder that already has photos (adds missing photos only)
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --allow-existing

:: Only one rover, only some sols
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --allow-existing --rovers perseverance --sol-min 1000 --sol-max 1100
```

On macOS or Linux, use `.venv/bin/python rover_scraper/main.py ...`.

| Option | Meaning |
|---|---|
| `--rovers` | Comma-separated: `curiosity`, `perseverance`, `spirit`, `opportunity` (default: all four) |
| `--sol-min`, `--sol-max` | Sol range to check (default: all sols) |
| `--dry-run` | Show what would be downloaded; write nothing |
| `--allow-existing` | Required to add to a folder that already contains rover photos |
| `--new-only` | Only download sols newer than the newest one in the folder; don't check existing sols for gaps |
| `--cameras` | Comma-separated camera names to include, e.g. `NAVCAM,MAST` (default: all) |
| `--max-files N` | Download at most N photos per period (a rolling window); waits, then continues |
| `--per` | The period for `--max-files`: `minute`, `hour` (default) or `day` |
| `--workers` | Parallel downloads (default 6) |
| `--verbose` | Also list every sol checked and every photo saved or skipped |
| `--report <file>` | Save a JSON summary of the run |
| `--retry-unavailable` | Try again the photos NASA refused on earlier runs |

The command line uses the key saved by the desktop app. Without the app, set it as an environment variable
instead, then open a new terminal: `setx MARSVISTA_API_KEY your_key_here` on Windows, or
`export MARSVISTA_API_KEY=your_key_here` in your shell profile on macOS and Linux.

## Running from the source code

Needed for the command line, and on macOS. You need **Python 3.10 or newer**.

```bat
git clone https://github.com/cincottash/RoverImageScraper
cd RoverImageScraper
py -3.10 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python rover_scraper\gui.py
```

On macOS or Linux, use `python3 -m venv .venv`, `.venv/bin/pip` and `.venv/bin/python`.

## Standalone app

The desktop app is packaged with [PyInstaller](https://pyinstaller.org) into one file per system, attached to
each [release](https://github.com/cincottash/RoverImageScraper/releases). Each carries its own private copy of
Python and the libraries it needs, so it runs without Python and downloads nothing to start. The standalone app is
the desktop app only; the command line runs from the source code.

- **Windows: `RoverImageScraper.exe`** (about 35 MB). Each time it starts it unpacks itself into Windows'
  temporary folder (about 90 MB); that copy is removed when it closes.
- **Linux: `RoverImageScraper-x86_64.AppImage`** (an [AppImage](https://appimage.org), about 60 MB). Runs on
  64-bit Intel/AMD Linux from about 2022 on: Ubuntu 22.04 or newer, Debian 12, Fedora 36 and similar. If it says
  FUSE is missing, install it (`sudo apt install libfuse2` on Ubuntu/Debian) or run it with
  `--appimage-extract-and-run`. The API key is kept in your desktop's keyring (GNOME Keyring or KWallet); without
  one, the key has to be entered each session.

### Building them yourself

The Windows `.exe` (on Windows, with Python 3.10):

```bat
py -3.10 -m venv build-venv
build-venv\Scripts\pip install -r requirements.txt -r packaging\requirements-build.txt
build-venv\Scripts\python packaging\build_exe.py --out C:\path\to\builds
```

The Linux AppImage (on 64-bit Linux, with Python 3.10 and
[appimagetool](https://github.com/AppImage/appimagetool/releases)). It runs on distributions at least as new as
the one it's built on:

```sh
python3.10 -m venv build-venv
build-venv/bin/pip install -r requirements.txt -r packaging/requirements-build.txt
build-venv/bin/python packaging/build_appimage.py --out ~/builds --appimagetool /path/to/appimagetool
```

Each build goes into a new dated subfolder of `--out`.

### Automatic builds and releases

`.github/workflows/build-apps.yml` builds both apps on GitHub (the `.exe` on Windows, the AppImage on
Ubuntu 22.04) and checks that each starts:

- **Pull requests:** builds are kept for 14 days as downloads on the run's page (*Actions* tab), so changes can
  be tried before merging. Nothing is published.
- **Every merge into `main`:** the same, then a release is published with both apps, tagged with the next
  version (v2.0.0 → v2.0.1 → v2.0.2, and so on). For a bigger step, put `[minor]` or `[major]` in the pull
  request's title.
- **A version tag pushed by hand** (`git tag v3.0.0` then `git push origin v3.0.0`) publishes exactly that
  version.

## Code layout

All program code is in `rover_scraper/`:

- `gui.py`: the desktop app. `main.py` and `cli.py`: the command line.
- `api.py`: the Mars Vista client. `pds.py`: NASA's archive, for Spirit and Opportunity.
- `scraper.py`: checks sols and downloads what's missing. `plan.py`: works out what a sol is missing.
- `download.py`: downloads that never overwrite existing files.
- `scan.py`: Library status (quick scan and full check).
- `ratelimit.py`: counts Mars Vista requests, and the optional download limit.
- `state.py`: the app data folder, and the photos NASA refuses to serve. `settings.py`: the app's saved settings.
- `check.svg`: the tick drawn in checkboxes.

Packaging: `packaging/build_exe.py` (Windows), `packaging/build_appimage.py` (Linux), `packaging/make_icon.py` and
`packaging/icon.svg` (the app icon), and `packaging/requirements-build.txt` (the build tools, pinned).

## License

Copyright (C) 2026 cincottash

Rover Image Scraper is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License version 3](LICENSE) (GPL-3.0). Anyone may use, copy, modify and share it; if you
distribute a modified version, it must also be released under the GPL-3.0 with its source code.

It uses [PySide6 / Qt](https://www.qt.io/qt-for-python) (LGPL-3.0), [requests](https://requests.readthedocs.io)
(Apache-2.0) and [keyring](https://github.com/jaraco/keyring) (MIT). The photos themselves are NASA's (image
credit: NASA/JPL-Caltech).

The *Classic* themes use the colors of these open-source color schemes, by their authors: Nord, Dracula,
Solarized, Gruvbox, One Dark / One Light, Tokyo Night, Catppuccin, Rosé Pine, Everforest, Kanagawa, Ayu,
Palenight, Night Owl, Cobalt2, Synthwave '84, Tomorrow, Monokai and Zenburn.
