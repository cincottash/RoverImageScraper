"""Summarize a --dry-run log: what would be added to sols already on disk vs. brand-new sols.

    python tools/summarize_dryrun.py <dry-run log> <dump_dir>
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict

LINE = re.compile(r"^PLAN (\w+)/(\d+)/(\S+): (?:new folder, (\d+) file|(\d+) on disk, (\d+) listed)")
NEW_SOL = re.compile(r"^PLAN (\w+)/(\d+): new sol, (\d+) file")


def main() -> int:
    log, dump = sys.argv[1], sys.argv[2]
    last_sol = {}
    for rover in os.listdir(dump):
        sols = [int(s) for s in os.listdir(os.path.join(dump, rover)) if s.isdigit()] \
            if os.path.isdir(os.path.join(dump, rover)) else []
        if sols:
            last_sol[rover] = max(sols)

    groups = defaultdict(Counter)        # (rover, category) -> camera -> files
    folders = Counter()
    sols = defaultdict(set)
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            ns = NEW_SOL.match(line)
            if ns:
                rover, sol, n = ns.group(1), int(ns.group(2)), int(ns.group(3))
                cat = "new sols" if sol > last_sol.get(rover, -1) else "old sols: missing sol folder"
                groups[(rover, cat)]["(all cameras)"] += n
                folders[(rover, cat)] += 1
                sols[(rover, cat)].add(sol)
                continue
            m = LINE.match(line)
            if not m:
                continue
            rover, sol, cam = m.group(1), int(m.group(2)), m.group(3)
            if m.group(4):
                n, kind = int(m.group(4)), "new folder"
            else:
                n, kind = int(m.group(6)) - int(m.group(5)), "fill"
            if sol > last_sol.get(rover, -1):
                cat = "new sols"
            else:
                cat = f"old sols: {kind}"
            groups[(rover, cat)][cam] += n
            folders[(rover, cat)] += 1
            sols[(rover, cat)].add(sol)

    for (rover, cat), cams in sorted(groups.items()):
        s = sols[(rover, cat)]
        print(f"\n{rover} - {cat} (dump's last sol: {last_sol.get(rover)})")
        print(f"  {folders[(rover, cat)]} camera folders in {len(s)} sols ({min(s)}..{max(s)}), "
              f"up to {sum(cams.values()):,} files")
        for cam, n in cams.most_common():
            print(f"    {cam:22s} {n:8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
