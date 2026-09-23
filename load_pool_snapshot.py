"""load_pool_snapshot.py — CI bootstrap: load the committed gold-IP pool.

The live shelf (ipshelf/data/) is gitignored, so a fresh Actions runner
has an empty pool. This script loads data/ip_pool_snapshot.json (a
committed snapshot of a recent local harvest) into the shelf so
ghost_watch.draw_gold_ip can ride real exits instead of the runner's
datacenter IP.

Every IP is still re-gated by draw_gold_ip before use — dead snapshot
entries get burned/skipped, and an all-dead snapshot degrades gracefully
to a direct connection.

Usage: python3 load_pool_snapshot.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SNAP = ROOT / "data" / "ip_pool_snapshot.json"


def main():
    if not SNAP.is_file():
        print("[snapshot] no data/ip_pool_snapshot.json — pool stays empty (direct)")
        return 0
    try:
        data = json.loads(SNAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[snapshot] snapshot unreadable ({exc}) — pool stays empty")
        return 0
    exits = data.get("exits", [])
    if not exits:
        print("[snapshot] snapshot has no exits — pool stays empty")
        return 0

    from ipshelf.core import shelf
    pool = shelf.load_pool()
    res = shelf.merge_exits(pool, exits)
    shelf.save_pool(pool)
    gold = sum(1 for e in pool.get("exits", []) if e.get("status") == "gold")
    print(f"[snapshot] merged: +{res['added']} added, {res['refreshed']} refreshed "
          f"(pool now {len(pool.get('exits', []))} exits, {gold} gold)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
