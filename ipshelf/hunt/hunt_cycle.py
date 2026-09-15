#!/usr/bin/env python3
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: header comment only; all paths HERE-relative
# (standing pool at proxyhunt/goldbank/standing_pool.json kept as-is).
"""hunt_cycle.py — ONE full 30-minute-cycle of the standing-gold-pool machine.

This is the "cron" heart. Each cycle:
  1. HUNT   — refresh_pool.sh: 30+ sources -> youtube 200 winners -> GOLD grade
              (env caps: HTTP_CAP / SOCKS_CAP / SOCKS4_CAP / PAR / TIMEOUT)
  2. MERGE  — fresh GOLD pool -> goldbank/standing_pool.json
              (first_seen kept, last_ok=now, score/geo/speed refreshed)
  3. PRUNE  — entries whose last_ok is older than PRUNE_MINUTES get dropped
              (measured half-life ~1-1.5h, so 180min default is generous)
  4. REPORT — standing count + fresh adds + top-10 champions

Run standalone (venv python, from yt-channel-automation):
  venv/Scripts/python.exe -m ipshelf.hunt.hunt_cycle
Cron:  */30 * * * * cd /path/to/yt-channel-automation && venv/Scripts/python.exe -m ipshelf.hunt.hunt_cycle >> hunt.log 2>&1
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STANDING_PATH = os.path.join(HERE, "proxyhunt", "goldbank", "standing_pool.json")
POOL_PATH = os.path.join(HERE, "proxyhunt", "pool.json")
REFRESH = os.path.join(HERE, "refresh_pool.sh")

PRUNE_MINUTES = 180


def utcnow():
    return datetime.now(timezone.utc)


def run_refresh(env):
    print("[hunt_cycle] === cycle start %s ===" % utcnow().strftime("%H:%M:%SZ"))
    t0 = time.time()
    r = subprocess.run(["bash", REFRESH], env=env, cwd=HERE)
    dt = time.time() - t0
    print("[hunt_cycle] refresh took %.0fs (rc=%d)" % (dt, r.returncode))
    return r.returncode


def merge_and_prune():
    if not os.path.exists(POOL_PATH):
        print("[hunt_cycle] no pool.json — hunt failed?")
        return None
    with open(POOL_PATH) as f:
        fresh = json.load(f)
    exits = fresh.get("exits", [])

    standing = {}
    if os.path.exists(STANDING_PATH):
        try:
            with open(STANDING_PATH) as f:
                standing = json.load(f)
        except Exception:
            standing = {}

    now = utcnow()
    fresh_adds = 0
    for e in exits:
        addr = e.get("addr")
        if not addr:
            continue
        old = standing.get(addr, {})
        if not old:
            fresh_adds += 1
        merged = dict(e)
        merged["first_seen"] = old.get("first_seen", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        merged["last_ok"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        merged["ok_cycles"] = old.get("ok_cycles", 0) + 1
        standing[addr] = merged

    # prune: drop entries not seen OK within PRUNE_MINUTES
    cutoff = now - timedelta(minutes=PRUNE_MINUTES)
    kept, dropped = {}, 0
    for addr, e in standing.items():
        try:
            lo = datetime.strptime(e.get("last_ok", "2000-01-01T00:00:00Z"),
                                   "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            lo = cutoff
        if lo >= cutoff:
            kept[addr] = e
        else:
            dropped += 1

    os.makedirs(os.path.dirname(STANDING_PATH), exist_ok=True)
    with open(STANDING_PATH, "w") as f:
        json.dump({"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "alive": len(kept),
                   "cycles_seen": fresh.get("graded_at", "?"),
                   "exits": list(kept.values())}, f, indent=1)

    # plain-text copy for humans
    txt = [f"# STANDING GOLD POOL — {now.strftime('%Y-%m-%d %H:%M UTC')} — {len(kept)} alive",
           f"# (fresh this cycle: {fresh_adds}, pruned: {dropped})", ""]
    top = sorted(kept.values(), key=lambda x: -x.get("score", 0))[:60]
    for e in top:
        geo = e.get("geo", {}) or {}
        txt.append("%-22s %-5s %-12s score=%-5s %-6s %-14s ok_cycles=%s" % (
            e.get("addr", "?"), e.get("proto", "?"), e.get("cc", "??"),
            e.get("score", 0), ("%0.1fMbps" % e.get("bandwidth_mbps", 0)) if e.get("bandwidth_mbps") else "-",
            (e.get("playability", "-") or "-")[:14], e.get("ok_cycles", 1)))
    with open(os.path.join(HERE, "proxyhunt", "goldbank", "standing_pool.txt"), "w") as f:
        f.write("\n".join(txt) + "\n")

    print("[hunt_cycle] STANDING ALIVE: %d  (fresh adds: %d, pruned: %d)" %
          (len(kept), fresh_adds, dropped))
    if kept:
        best = sorted(kept.values(), key=lambda x: -x.get("score", 0))[:5]
        for e in best:
            print("          top: %-22s %-3s score=%s ok_cycles=%s" %
                  (e.get("addr"), e.get("cc", "??"), e.get("score"), e.get("ok_cycles")))
    return len(kept)


def main():
    env = dict(os.environ)
    # modest default caps for a 30-min cadence box (override before calling)
    env.setdefault("HTTP_CAP", "3000")
    env.setdefault("SOCKS_CAP", "1200")
    env.setdefault("SOCKS4_CAP", "800")
    env.setdefault("PAR", "200")
    env.setdefault("TIMEOUT", "8")
    rc = run_refresh(env)
    alive = merge_and_prune()
    print("[hunt_cycle] === cycle end — standing %s ===" % alive)
    return 0 if alive is not None else rc


if __name__ == "__main__":
    sys.exit(main())
