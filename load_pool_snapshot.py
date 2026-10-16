"""load_pool_snapshot.py — CI bootstrap: load the committed gold-IP pool.

The live shelf (ipshelf/data/) is gitignored, so a fresh Actions runner
has an empty pool. This script loads data/ip_pool_snapshot.json (a
committed snapshot of a recent harvest) into the shelf so
ghost_watch.draw_gold_ip can ride real exits instead of the runner's
datacenter IP.

With --with-stream it ALSO folds in the gold-stream branch's per-shard
files (data/stream/gold_shard_*.json) — certified gold that harvest shards
pushed CYCLE BY CYCLE while harvesting. The committed snapshot may be
hours old; the stream is minutes old. That is the water-pile model: watch
runners draw from the freshest water in the pile. draw_gold_ip re-gates
every candidate before use anyway, so a noisy stream entry costs one gate
check, never a bad watch.

Every IP is still re-gated before use — dead entries get burned/skipped,
and an all-dead pool degrades gracefully to a direct connection.

Usage:
  python3 load_pool_snapshot.py                                  # shelf merge
  python3 load_pool_snapshot.py --with-stream --out data/pool_merged.json
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SNAP = ROOT / "data" / "ip_pool_snapshot.json"
STREAM_DIR = ROOT / "data" / "stream"
#: streamed gold older than this is near-certainly dead (free proxies live
#: 1-3h); older entries still land in the merged file, after the fresh ones,
#: but only while the merged pool would otherwise be thin.
STREAM_FRESH_SEC = 45 * 60
STREAM_MAX_AGE_SEC = 2 * 3600
#: below this many candidates, old stream entries are kept rather than
#: leaving the pool starving (a stale candidate costs one gate check)
STREAM_KEEP_STALE_UNDER = 200


def load_stream_entries():
    """Gold entries streamed cycle-by-cycle by the harvest shards."""
    out = []
    if not STREAM_DIR.is_dir():
        return out
    for p in sorted(STREAM_DIR.glob("gold_shard_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            out.extend(e for e in data if e.get("addr"))
    return out


def merge_snapshot_with_stream(snapshot_exits, stream_entries):
    """Dedupe snapshot + stream by (addr, proto); freshest verdict wins.

    Stream entries older than STREAM_MAX_AGE_SEC are dropped UNLESS the pool
    would otherwise be thin (< STREAM_KEEP_STALE_UNDER candidates): the
    gold-stream branch is append-only, so without this a week-old branch
    re-injects hundreds of corpses into every run's gate wave (the shelf's
    whole point is minute-old water, not yesterday's).
    """
    now = time.time()
    fresh, recent_stream, stale, seen = [], [], [], set()
    for e in sorted(stream_entries, key=lambda e: -(e.get("gated_at") or 0)):
        key = (e["addr"], e.get("proto", "HTTP"))
        if key in seen:
            continue
        seen.add(key)
        age = now - (e.get("gated_at") or 0)
        if age < STREAM_FRESH_SEC:
            fresh.append(e)
        elif age < STREAM_MAX_AGE_SEC:
            recent_stream.append(e)
        else:
            stale.append(e)
    snap = []
    for e in snapshot_exits:
        if not e.get("addr"):
            continue
        key = (e["addr"], e.get("proto", "HTTP"))
        if key not in seen:
            seen.add(key)
            snap.append(e)
    # honest freshness order: <45min stream, then everything else by gated_at
    tail = sorted(recent_stream + snap, key=lambda e: -(e.get("gated_at") or 0))
    merged = fresh + tail
    kept_stale = False
    if len(merged) < STREAM_KEEP_STALE_UNDER:
        merged = merged + stale
        kept_stale = True
    return merged, {"stream_total": len(stream_entries),
                    "stream_fresh": len(fresh),
                    "stream_recent": len(recent_stream),
                    "stream_stale_kept": len(stale) if kept_stale else 0,
                    "stream_stale_dropped": 0 if kept_stale else len(stale),
                    "snapshot_exits": len(snap),
                    "merged": len(merged)}


def main():
    argv = list(sys.argv[1:])
    with_stream = "--with-stream" in argv
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            out = Path(argv[i + 1])

    if not SNAP.is_file():
        print("[snapshot] no data/ip_pool_snapshot.json — pool stays empty (direct)")
        return 0
    try:
        data = json.loads(SNAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[snapshot] snapshot unreadable ({exc}) — pool stays empty")
        return 0
    exits = data.get("exits", [])

    if with_stream:
        stream = load_stream_entries()
        exits, st = merge_snapshot_with_stream(exits, stream)
        print(f"[snapshot] +stream: {st['stream_fresh']} fresh + "
              f"{st['stream_recent']} recent of {st['stream_total']} streamed "
              f"({st['stream_stale_dropped']} too old, dropped) + "
              f"{st['snapshot_exits']} from snapshot -> {st['merged']} candidates")
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(
                {"snapshotted_at": data.get("snapshotted_at"), "exits": exits},
                indent=2), encoding="utf-8")
            print(f"[snapshot] merged pool written: {out.name} ({st['merged']} exits)")
            return 0

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


def load_snapshot_exits():
    """Return the snapshot's exits as a list (no shelf write). Used by the
    watcher's IP draw as the last-resort source on a cloud runner."""
    try:
        data = json.loads(SNAP.read_text(encoding="utf-8"))
        return data.get("exits", [])
    except (OSError, json.JSONDecodeError):
        return []
