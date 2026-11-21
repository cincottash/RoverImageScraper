"""Command line: python rover_scraper/main.py <dest> [--allow-existing] [--new-only] [--dry-run] [options]"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from api import VERIFIED_ROVERS, ApiError, MarsVistaClient, load_api_key
from ratelimit import RequestTracker
from scraper import Report, Scraper
from state import Unavailable, default_state_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Download Mars rover photos from Mars Vista into "
                                            "<dest>/<Rover>/<sol>/<CAMERA>/<n>, adding only what's missing.")
    p.add_argument("dest", help="folder to save photos into (must exist)")
    p.add_argument("--rovers", default=",".join(VERIFIED_ROVERS),
                   help=f"comma-separated rover names (default: {','.join(VERIFIED_ROVERS)})")
    p.add_argument("--sol-min", type=int, default=0)
    p.add_argument("--sol-max", type=int, help="last sol to fetch (default: the rover's latest sol)")
    p.add_argument("--dry-run", action="store_true", help="show what would be downloaded; write nothing")
    p.add_argument("--allow-existing", action="store_true",
                   help="allow writing into a folder that already holds rover data (adds missing files only)")
    p.add_argument("--include-unverified", action="store_true",
                   help="allow rovers whose Mars Vista data has not been checked against the old output")
    p.add_argument("--report", help="write a JSON report to this path")
    p.add_argument("--state-dir", default=default_state_dir(),
                   help="where to remember photos NASA refuses to serve (default: %(default)s)")
    p.add_argument("--workers", type=int, default=6, help="parallel downloads (default: %(default)s)")
    p.add_argument("--retry-unavailable", action="store_true",
                   help="forget remembered 403/404 photos and try them again")
    p.add_argument("--new-only", action="store_true",
                   help="only download sols after the last one on disk; don't check existing sols for gaps")
    p.add_argument("--cameras", help="comma-separated camera names to include (default: all)")
    return p.parse_args(argv)


def _print_wait(seconds: float, reason: str) -> None:
    resume = time.strftime("%H:%M", time.localtime(time.time() + seconds))
    m, s = divmod(int(seconds), 60)
    print(f"Rate limited by Mars Vista ({reason}): waiting {m}:{s:02d}, resumes about {resume}. "
          f"No action needed.", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.path.isdir(args.dest):
        print(f"Error: folder {args.dest} does not exist", file=sys.stderr)
        return 2

    tracker = RequestTracker(os.path.join(args.state_dir, "requests.json"))
    try:
        client = MarsVistaClient(load_api_key(), tracker=tracker, on_wait=_print_wait)
        known = {r.name.lower(): r for r in client.rovers()}
    except ApiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    rovers = []
    for name in (n.strip().lower() for n in args.rovers.split(",") if n.strip()):
        if name not in known:
            print(f"Error: unknown rover {name!r}; choose from {', '.join(r.name for r in known.values())}",
                  file=sys.stderr)
            return 2
        if known[name].name not in VERIFIED_ROVERS and not args.include_unverified:
            print(f"Error: {known[name].name} has not been verified against the old output "
                  f"(pass --include-unverified to fetch it anyway)", file=sys.stderr)
            return 2
        rovers.append(known[name])

    existing = [r.name for r in rovers if os.path.isdir(os.path.join(args.dest, r.name))]
    if existing and not args.allow_existing and not args.dry_run:
        print(f"Error: {args.dest} already has data for {', '.join(existing)}. Re-run with --dry-run to see "
              f"what would change, and --allow-existing to add missing files.", file=sys.stderr)
        return 2

    unavailable = Unavailable(os.path.join(args.state_dir, "unavailable.json"),
                              ignore_existing=args.retry_unavailable)
    wanted = {c.strip().upper() for c in args.cameras.split(",") if c.strip()} if args.cameras else None
    cameras = {r.name: wanted for r in rovers} if wanted else None
    scraper = Scraper(client, args.dest, dry_run=args.dry_run, unavailable=unavailable, workers=args.workers,
                      cameras=cameras)
    report = Report()
    try:
        for rover in rovers:
            sol_max = rover.max_sol if args.sol_max is None else min(args.sol_max, rover.max_sol)
            print(f"== {rover.name}: sols {args.sol_min}..{sol_max}" + (" (new sols only)" if args.new_only else ""))
            scraper.run(rover.name, args.sol_min, sol_max, report, new_only=args.new_only)
    except KeyboardInterrupt:
        report.cancelled = True
        print("\nInterrupted. Finished files are kept; re-run to continue.")
    finally:
        tracker.save()
    usage = tracker.usage()
    print(f"\nMars Vista requests (estimated): {usage.hour:,} of {usage.hourly_limit:,} this hour, "
          f"{usage.day:,} of {usage.daily_limit:,} today")

    print(f"\nSols with photos checked: {report.sols_checked} "
          f"({report.sols_skipped_quick} complete by manifest match, "
          f"{report.sols_skipped_counts} by per-camera counts)")
    print(f"Camera folders looked at: {dict(report.actions)}")
    if args.dry_run:
        print(f"Would download: up to {report.would_download} file(s)")
    else:
        print(f"Downloaded: {report.downloaded} file(s); already on disk (same bytes): {report.already_present}")
        print(f"NASA refused (403/404): {report.newly_unavailable} new, {report.known_unavailable} known "
              f"(remembered in {unavailable.path})")
    print(f"Problems/skips: {len(report.problems)}")
    for line in report.problems[:50]:
        print("  " + line)
    if len(report.problems) > 50:
        print(f"  ... {len(report.problems) - 50} more (see --report)")
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"sols_checked": report.sols_checked, "sols_skipped_quick": report.sols_skipped_quick,
                       "sols_skipped_counts": report.sols_skipped_counts,
                       "actions": report.actions, "downloaded": report.downloaded,
                       "already_present": report.already_present,
                       "would_download": report.would_download, "problems": report.problems,
                       "cancelled": report.cancelled}, f, indent=1)
    return 130 if report.cancelled else 0
