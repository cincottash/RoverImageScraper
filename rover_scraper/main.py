"""Download Mars rover photos into <dest>/<Rover>/<sol>/<CAMERA>/<n>.

Run:  python rover_scraper/main.py <dest> [options]     (see README.md, or --help)
"""
import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
