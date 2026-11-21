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
3. Get a free API key at [marsvista.dev](https://marsvista.dev) (sign-up with an email address), then store it in
   an environment variable:
   ```bat
   setx MARSVISTA_API_KEY your_key_here
   ```
   Open a **new** terminal afterwards so it sees the variable. On macOS or Linux, use
   `export MARSVISTA_API_KEY=...` in your shell profile. Keep the key out of git.

## Usage

The destination folder must already exist.

```bat
:: First download into an empty folder (all of Curiosity and Perseverance: about 1.3 million photos, ~270 GB)
.venv\Scripts\python main.py D:\RoverPics

:: Preview what would be downloaded, without writing anything
.venv\Scripts\python main.py D:\RoverPics --dry-run

:: Update a folder that already has photos (adds missing files only)
.venv\Scripts\python main.py D:\RoverPics --allow-existing

:: Only one rover, only some sols
.venv\Scripts\python main.py D:\RoverPics --allow-existing --rovers perseverance --sol-min 1000 --sol-max 1100
```

| Option | Meaning |
|---|---|
| `--rovers` | Comma-separated: `curiosity`, `perseverance` (default: both) |
| `--sol-min`, `--sol-max` | Sol range to check (default: all sols) |
| `--dry-run` | Show what would be downloaded; write nothing |
| `--allow-existing` | Required to add to a folder that already contains rover data |
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
- If Mars Vista's rate limit (10,000 requests per hour) is reached, the program waits for it to reset and then
  carries on.
- NASA no longer serves some older images (HTTP 403/404). These are remembered in
  `%LOCALAPPDATA%\RoverImageScraper\unavailable.json` (on macOS/Linux, `~/.local/share/RoverImageScraper/`) so
  they don't count as missing on every run.

### Limits

- **Spirit and Opportunity are not downloaded.** Mars Vista's image links for them are often broken.
  `--include-unverified` overrides this, but expect failures.
- **Curiosity thumbnails are not included.** Folders made with the old Mars Rover Photos API may contain
  thumbnail copies as extra numbered files. Mars Vista doesn't list them, so those folders look "complete" by
  count even if a full-size photo is missing.
- The order of photos within a camera folder follows Mars Vista's listing and may differ from folders made with
  the old API.

## Code layout

- `rover_scraper/api.py`: the Mars Vista client (the only code that knows the API).
- `rover_scraper/plan.py`: decides what's missing.
- `rover_scraper/scraper.py`: runs downloads.
- `rover_scraper/cli.py`: the command line.
