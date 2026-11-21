"""After a rehearsal run on a copy of the dump, check that no original file was changed or removed, and list
what was added. Opens the dump read-only.

    python tools/verify_untouched.py <copy_dir> <dump_dir> --sol-min 0 --sol-max 10 [--rovers ...]
"""
from __future__ import annotations

import argparse
import filecmp
import os
import sys
from collections import Counter


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("copy_dir")
    ap.add_argument("dump_dir")
    ap.add_argument("--rovers", default="Curiosity,Perseverance")
    ap.add_argument("--sol-min", type=int, default=0)
    ap.add_argument("--sol-max", type=int, required=True)
    a = ap.parse_args()

    ok = True
    for rover in a.rovers.split(","):
        checked, changed, removed = 0, [], []
        added: Counter = Counter()
        leftovers = []
        for sol in range(a.sol_min, a.sol_max + 1):
            ddir, cdir = os.path.join(a.dump_dir, rover, str(sol)), os.path.join(a.copy_dir, rover, str(sol))
            for root, _, names in os.walk(ddir):
                for n in names:
                    d = os.path.join(root, n)
                    c = os.path.join(cdir, os.path.relpath(d, ddir))
                    checked += 1
                    if not os.path.isfile(c):
                        removed.append(c)
                    elif not filecmp.cmp(d, c, shallow=False):
                        changed.append(c)
            for root, _, names in os.walk(cdir):
                for n in names:
                    c = os.path.join(root, n)
                    if n.endswith(".part"):
                        leftovers.append(c)
                    elif not os.path.exists(os.path.join(ddir, os.path.relpath(c, cdir))):
                        added[os.path.relpath(root, os.path.join(a.copy_dir, rover)).replace("\\", "/")] += 1
        print(f"\n===== {rover}, sols {a.sol_min}..{a.sol_max}")
        print(f"original files checked: {checked}   changed: {len(changed)}   removed: {len(removed)}   "
              f".part leftovers: {len(leftovers)}")
        print(f"files added: {sum(added.values())} in {len(added)} folder(s)")
        for folder, n in sorted(added.items()):
            print(f"    +{n:4d}  {folder}")
        for p in (changed + removed + leftovers)[:20]:
            print("    PROBLEM:", p)
        ok &= not (changed or removed or leftovers)
    print("\nRESULT:", "PASS (no original file changed or removed)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
