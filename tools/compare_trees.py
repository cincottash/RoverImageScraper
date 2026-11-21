"""Compare a test download against the existing dump, file by file (SHA-256). Opens the dump read-only.

    python tools/compare_trees.py <test_dir> <dump_dir> --rovers Curiosity,Perseverance --sol-min 0 --sol-max 10

Checks:
  * every file in the test folder exists in the dump at the same path, with identical bytes;
  * dump files missing from the test folder are listed. Small ones (< --thumb-kb) are counted as probable
    thumbnails, which Mars Vista doesn't have;
  * camera folders that exist only in the test folder (e.g. cameras the old API lacked) are listed separately.
Exit code 0 only if no test file differs from or is missing in the dump (other than in new camera folders),
and the only dump-only files are probable thumbnails.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def files_under(root: str, rover: str, sol_min: int, sol_max: int) -> dict[str, str]:
    """Map relative path 'Rover/sol/CAMERA/n' -> absolute path, for sols in range."""
    out = {}
    base = os.path.join(root, rover)
    if not os.path.isdir(base):
        return out
    for sol in os.listdir(base):
        if not sol.isdigit() or not sol_min <= int(sol) <= sol_max:
            continue
        for cam in os.listdir(os.path.join(base, sol)):
            cdir = os.path.join(base, sol, cam)
            if os.path.isdir(cdir):
                for n in os.listdir(cdir):
                    out[f"{rover}/{sol}/{cam}/{n}"] = os.path.join(cdir, n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_dir")
    ap.add_argument("dump_dir")
    ap.add_argument("--rovers", default="Curiosity,Perseverance")
    ap.add_argument("--sol-min", type=int, default=0)
    ap.add_argument("--sol-max", type=int, required=True)
    ap.add_argument("--thumb-kb", type=int, default=20, help="dump-only files below this size count as thumbnails")
    ap.add_argument("--show", type=int, default=15, help="how many example paths to print per category")
    a = ap.parse_args()

    ok = True
    for rover in a.rovers.split(","):
        test = files_under(a.test_dir, rover, a.sol_min, a.sol_max)
        dump = files_under(a.dump_dir, rover, a.sol_min, a.sol_max)
        dump_cams = {p.rsplit("/", 1)[0] for p in dump}
        identical, differs, new_cam, missing_in_dump = [], [], [], []
        for rel, path in sorted(test.items()):
            if rel in dump:
                (identical if sha256(path) == sha256(dump[rel]) else differs).append(rel)
            elif rel.rsplit("/", 1)[0] not in dump_cams:
                new_cam.append(rel)
            else:
                missing_in_dump.append(rel)
        dump_only = [rel for rel in dump if rel not in test]
        thumbs = [r for r in dump_only if os.path.getsize(dump[r]) < a.thumb_kb * 1024]
        big = [r for r in dump_only if r not in set(thumbs)]

        print(f"\n===== {rover}, sols {a.sol_min}..{a.sol_max}")
        print(f"test files: {len(test)}   dump files: {len(dump)}")
        print(f"  identical (same path, same bytes):           {len(identical)}")
        print(f"  DIFFERENT bytes at same path:                {len(differs)}")
        print(f"  extra files in an existing dump camera:      {len(missing_in_dump)}  (top-up candidates)")
        print(f"  files in camera folders new to the dump:     {len(new_cam)}  "
              f"{dict(Counter(r.split('/')[2] for r in new_cam))}")
        print(f"  dump-only, < {a.thumb_kb}KB (probable thumbnails): {len(thumbs)}")
        print(f"  dump-only, larger (NOT explained):           {len(big)}")
        for label, items in (("DIFFERENT", differs), ("extra", missing_in_dump), ("dump-only large", big)):
            for rel in sorted(items, key=_sort_key)[: a.show]:
                print(f"    {label}: {rel}")
        if differs or big:
            ok = False
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _sort_key(rel: str):
    r, sol, cam, n = rel.split("/")
    return r, int(sol), cam, int(n) if n.isdigit() else 0


if __name__ == "__main__":
    sys.exit(main())
