"""Classify each camera folder of a test download against the dump by content. Opens the dump read-only.

    python tools/classify_folders.py <test_dir> <dump_dir> --sol-min 0 --sol-max 10 [--rovers ...] [--details]

Classes per <Rover>/<sol>/<CAMERA> (T = test files in numeric order, D = dump files in numeric order):
  identical      T and D are the same bytes in the same order.
  same_order     Leaving out the files only one side has, the shared photos are in the same order. Means the
                 numbering scheme agrees and the difference is extra/missing photos (e.g. Curiosity thumbnails).
  reordered      Same photos, but in a different order. The numbering would not match.
  new_folder     The camera folder doesn't exist in the dump.
  missing        The folder exists in the dump but not in the test download.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter


def hashes(folder: str) -> list[str]:
    if not os.path.isdir(folder):
        return []
    names = sorted((n for n in os.listdir(folder) if n.isdigit()), key=int)
    out = []
    for n in names:
        with open(os.path.join(folder, n), "rb") as f:
            out.append(hashlib.sha256(f.read()).hexdigest())
    return out


def dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for h in seq:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def classify(t: list[str], d: list[str]) -> str:
    if not d:
        return "new_folder"
    if not t:
        return "missing"
    if t == d:
        return "identical"
    shared = set(t) & set(d)
    # Duplicates (the same image listed twice) occur on both sides; compare first occurrences.
    tt = dedupe([h for h in t if h in shared])
    dd = dedupe([h for h in d if h in shared])
    return "same_order" if tt == dd else "reordered"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_dir")
    ap.add_argument("dump_dir")
    ap.add_argument("--rovers", default="Curiosity,Perseverance")
    ap.add_argument("--sol-min", type=int, default=0)
    ap.add_argument("--sol-max", type=int, required=True)
    ap.add_argument("--details", action="store_true", help="print one line per folder")
    a = ap.parse_args()

    for rover in a.rovers.split(","):
        totals: Counter = Counter()
        files = Counter()
        per_sol: dict[int, Counter] = {}
        for sol in range(a.sol_min, a.sol_max + 1):
            tdir, ddir = (os.path.join(r, rover, str(sol)) for r in (a.test_dir, a.dump_dir))
            cams = set(os.listdir(tdir) if os.path.isdir(tdir) else []) | \
                set(os.listdir(ddir) if os.path.isdir(ddir) else [])
            for cam in sorted(cams):
                t, d = hashes(os.path.join(tdir, cam)), hashes(os.path.join(ddir, cam))
                c = classify(t, d)
                totals[c] += 1
                per_sol.setdefault(sol, Counter())[c] += 1
                shared = Counter(t) & Counter(d)
                files["test"] += len(t)
                files["dump"] += len(d)
                files["shared"] += sum(shared.values())
                if a.details:
                    print(f"  {rover}/{sol}/{cam}: {c:11s} test={len(t):4d} dump={len(d):4d} "
                          f"shared={sum(shared.values()):4d} test-only={len(t) - sum(shared.values()):4d} "
                          f"dump-only={len(d) - sum(shared.values()):4d}")
        print(f"\n===== {rover}, sols {a.sol_min}..{a.sol_max}")
        print(f"camera folders: {dict(totals)}")
        print(f"files: test={files['test']} dump={files['dump']} shared(same bytes)={files['shared']}")
        print("per sol: " + "  ".join(f"{s}:{dict(c)}" for s, c in per_sol.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
