# RoverImageScraper

Downloads raw photos from NASA's Mars rovers, Curiosity and Perseverance, into a simple folder tree, using the
[Mars Vista API](https://github.com/james-langridge/mars-vista-api). Run it again later and it downloads only
what's new or missing.

```
<dest>/
  Curiosity/
    3786/
      MAST/
        0
        1
        ...
      NAVCAM/
        0
        ...
  Perseverance/
    ...
```

The layout is `<dest>/<Rover>/<sol>/<CAMERA>/<n>`. A *sol* is a Mars day counted from landing, and files are
numbered 0, 1, 2… within each camera folder. Files have no extension, but they are ordinary JPEG images.

## Setup

1. Install **Python 3.10 or newer**.
2. Clone the repo and create a virtual environment:
   ```bat
   git clone https://github.com/cincottash/RoverImageScraper
   cd RoverImageScraper
   py -3.10 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/pip`.
3. Get a free API key at [marsvista.dev](https://marsvista.dev) (sign-up with an email address).
   - **Desktop app:** paste the key into the API key box. It's saved in the system credential store (Windows
     Credential Manager) and used by the command line too.
   - **Command line only:** you can instead store it in an environment variable, then open a **new** terminal:
     ```bat
     setx MARSVISTA_API_KEY your_key_here
     ```
     On macOS or Linux, use `export MARSVISTA_API_KEY=...` in your shell profile.

   Keep the key out of git.

## Desktop app

From the project folder:

```bat
.venv\Scripts\python rover_scraper\gui.py
```

1. Choose a **photo folder** (any drive) and paste your **API key**. If the folder doesn't exist yet, the app
   offers to create it when you start, scan or open it.
2. Click **Quick scan** to see what's already in the folder and what's new on Mars Vista.
3. Optionally click **Preview** to see roughly how many photos and gigabytes a download would add.
4. Click **Start**.

Pausing and stopping:

- **Pause** finishes the photos in progress and remembers exactly where the download is. **Resume** continues
  from that sol without re-checking the sols before it. The paused position is kept if you close the app; closing
  during a download offers *Pause and quit*.
- **Stop** ends the run. Finished photos are kept, and the next Start begins from the beginning (sols that are
  already complete are skipped quickly). While paused, Stop discards the paused position.

What the window offers:

- **Rovers and cameras:** tick the rovers to download, and open *Cameras* to leave out cameras you don't want.
  Unticking a rover unticks its cameras (ticking it again brings your camera choice back), and ticking a camera
  ticks its rover. If Start is greyed out, the bar at the bottom says what's missing. Spirit and Opportunity are marked *experimental*: Mars Vista's image links for them are often broken, so the
  app warns you before including them.
- **Check existing sols for missing photos:** when ticked, the run starts at sol 0 and fills gaps in sols you
  already have (slower). Untick it to only download sols after the last one in the folder.
- **Sols from/to:** limit the run to a range of sols. Leave blank for all.
- **Library status:** sols, photos and size on disk, the latest sol available, and how many new sols there are.
  *Full check* also finds gaps in existing sols; it uses about one API request per sol on disk, so it asks first.
- **Progress:** photos and data downloaded, current speed (averaged over a few seconds), average speed, photo
  rate, elapsed time and an estimate of the time left.
- **API usage:** how many Mars Vista requests have been used this hour and today, counted by the app. When the
  limit is reached, an amber banner shows a countdown, and the download carries on by itself afterwards.
- **Activity:** the same log the command line prints.
- **Menus:**
  - *File*: choose or open the photo folder, quit.
  - *Settings*: retry photos NASA refused on the next run, and open the settings folder.
  - *View*: units for data (KB/MB/GB/TB) and speed (KB/s, MB/s, Mbit/s), how long the speed is averaged over,
    light/dark theme, and showing or hiding the activity log.
  - *Help*: the Mars Vista website and About.

Settings, scan results and the request counts are saved in `%LOCALAPPDATA%\RoverImageScraper\` (on macOS/Linux,
`~/.local/share/RoverImageScraper/`), never in the repo or the photo folder.

## Command line

Pass the folder you want the photos saved in. It can be on any drive, and it must already exist. In the examples
below, replace `C:\path\to\MarsPhotos` with your folder (on macOS or Linux, a path such as `~/MarsPhotos`).

Run the commands from the project folder (the one containing `rover_scraper\`).

```bat
:: First download into an empty folder (all of Curiosity and Perseverance: about 1.3 million photos, ~270 GB)
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos"

:: Preview what would be downloaded, without writing anything
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --dry-run

:: Update a folder that already has photos (adds missing files only)
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --allow-existing

:: Only one rover, only some sols
.venv\Scripts\python rover_scraper\main.py "C:\path\to\MarsPhotos" --allow-existing --rovers perseverance --sol-min 1000 --sol-max 1100
```

To run from any other folder, use full paths to both `python.exe` (inside `.venv\Scripts\`) and
`rover_scraper\main.py`. On macOS or Linux, use `.venv/bin/python rover_scraper/main.py ...`.

| Option | Meaning |
|---|---|
| `--rovers` | Comma-separated: `curiosity`, `perseverance` (default: both) |
| `--sol-min`, `--sol-max` | Sol range to check (default: all sols) |
| `--dry-run` | Show what would be downloaded; write nothing |
| `--allow-existing` | Required to add to a folder that already contains rover data |
| `--new-only` | Only download sols after the last one in the folder; don't check existing sols for gaps |
| `--cameras` | Comma-separated camera names to include, e.g. `NAVCAM,MAST` (default: all) |
| `--workers` | Parallel downloads (default 6) |
| `--report <file>` | Save a JSON summary of the run |
| `--retry-unavailable` | Retry photos NASA refused on earlier runs (see below) |

### How updates work

- The program never overwrites, renames or deletes existing files. It only adds new ones.
- For each sol it compares the number of files on disk with the number Mars Vista lists for each camera. Sols
  that match are skipped quickly. Short camera folders get only the photos that aren't already there, numbered
  after the highest existing file.
- A run interrupted with Ctrl+C keeps every finished file and leaves no partial files. Run the same command
  again to continue.
- Mars Vista allows 10,000 requests per hour and 100,000 per day. The program counts its own requests, slows
  down before reaching a limit, waits if the server asks it to, and then carries on. The command line prints a
  line when it starts waiting and shows the counts at the end. Photo downloads come from NASA and don't count.
- NASA no longer serves some older images (HTTP 403/404). These are remembered in
  `%LOCALAPPDATA%\RoverImageScraper\unavailable.json` (on macOS/Linux, `~/.local/share/RoverImageScraper/`) so
  they don't count as missing on every run.

### Limits

- **Spirit and Opportunity are experimental.** Mars Vista's image links for them are often broken, so many of
  their photos may fail to download. The command line leaves them out unless you pass `--include-unverified`;
  the desktop app asks before including them.
- **Curiosity thumbnails are not included.** Folders made with the old Mars Rover Photos API may contain
  thumbnail copies as extra numbered files. Mars Vista doesn't list them, so those folders look "complete" by
  count even if a full-size photo is missing.
- The order of photos within a camera folder follows Mars Vista's listing and may differ from folders made with
  the old API.

## Code layout

All code is in `rover_scraper/`:

- `main.py`: the command-line program.
- `gui.py`: the desktop app.
- `cli.py`: command-line options and the end-of-run summary.
- `api.py`: the Mars Vista client (the only code that knows the API).
- `ratelimit.py`: counts API requests per hour and day, and waits near the limits.
- `plan.py`: decides what's missing.
- `scraper.py`: runs the checks and downloads.
- `scan.py`: library status (quick scan and full check).
- `download.py`: downloads that never overwrite existing files.
- `state.py`: remembers photos NASA refuses to serve.
- `settings.py`: the desktop app's saved settings.
