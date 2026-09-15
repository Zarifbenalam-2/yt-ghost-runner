"""seed_pool.py — one-time bootstrap of the shelf from the hunt machine's
graded standing pool.

Reads goldbank/standing_pool.json (graded exits with geo/score/first_seen)
and builds ipshelf/data/shelf_pool.json via shelf.merge_exits into an empty
pool. After this seed the runtime NEVER touches the FRee Gold IP folder
again — this is the ONLY module that knows that path exists (zip-portable
isolation rule).

Usage (from yt-channel-automation cwd):
    venv/Scripts/python.exe -m ipshelf.core.seed_pool [--source <path>]

Prints a per-country + per-tier distribution table and totals, so the
seeder can immediately see which shelves got stocked.
"""

import json
import sys
from pathlib import Path

from . import shelf

DEFAULT_SOURCE = (r"D:\New folder (3)\streamstress-full-project"
                  r"\FRee Gold IP\extracted\proxy-machine"
                  r"\goldbank\standing_pool.json")

# fields copied from a graded hunt entry onto a shelf entry
GRADE_FIELDS = ("addr", "proto", "egress_ip", "geo", "cc", "tier", "type",
                "score", "latency_ms", "bandwidth_mbps", "playability",
                "page_ok", "github", "first_seen")


def load_standing(path) -> list:
    """Read the standing pool file, return shelf-ready exit dicts."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for e in data.get("exits", []):
        if not e.get("addr"):
            continue
        out.append({k: e[k] for k in GRADE_FIELDS if k in e})
    return out


def distribution_table(pool) -> str:
    """Render per-cc + per-tier distribution + totals as text."""
    exits = pool.get("exits", [])
    by_cc = {}
    by_tier = {}
    for e in exits:
        cc = e.get("cc") or "??"
        row = by_cc.setdefault(cc, {"count": 0, "score": 0})
        row["count"] += 1
        row["score"] += e.get("score", 0)
        by_tier[e.get("tier", "?")] = by_tier.get(e.get("tier", "?"), 0) + 1

    lines = []
    lines.append(" CC  COUNT   AVG_SCORE   TIER")
    lines.append("-" * 40)
    for cc, row in sorted(by_cc.items(),
                          key=lambda kv: (-kv[1]["count"], kv[0])):
        avg = row["score"] / row["count"] if row["count"] else 0
        tier = "T1" if cc in shelf.T1_SET else "T2/T3"
        lines.append(f" {cc:3s} {row['count']:5d}   {avg:6.1f}     {tier}")
    lines.append("-" * 40)
    for tier in ("T1", "T2", "T3"):
        lines.append(f" tier {tier}: {by_tier.get(tier, 0)}")
    lines.append(f" TOTAL: {len(exits)} gold exits seeded")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    source = DEFAULT_SOURCE
    if "--source" in argv:
        i = argv.index("--source")
        if i + 1 >= len(argv):
            print("FATAL: --source needs a path", file=sys.stderr)
            return 2
        source = argv[i + 1]

    src = Path(source)
    if not src.is_file():
        print(f"FATAL: source pool not found: {src}", file=sys.stderr)
        return 2

    exits = load_standing(src)
    if not exits:
        print(f"FATAL: no graded exits found in {src}", file=sys.stderr)
        return 2

    print(f"[*] seeding shelf from {src}")
    print(f"[*] {len(exits)} graded exits -> {shelf.pool_path()}")

    pool = {"updated": shelf.now_ts(), "exits": []}
    res = shelf.merge_exits(pool, exits)
    shelf.save_pool(pool)

    print(f"[*] merge: {res['added']} added, {res['refreshed']} refreshed")
    print(distribution_table(pool))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
