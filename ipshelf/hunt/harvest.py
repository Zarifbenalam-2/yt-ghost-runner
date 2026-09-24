#!/usr/bin/env python3
# ipshelf/hunt — self-contained copy of the gold hunt machine (no dependency
# on FRee Gold IP). Adapted: this bridge (harvest.py) is NEW — it wraps the
# copied pipeline (refresh_pool.sh) in a venv-python-safe runner and merges
# the graded exits into the core shelf pool.
"""harvest.py — bridge entry point: run the hunt pipeline, feed the shelf.

Runs the self-contained gold hunt pipeline (HUNT -> RANK -> GRADE) as a
subprocess, then merges the graded exits into the core shelf pool so the
assigner can draw them. The pipeline lives entirely inside ipshelf/hunt/
(COPIED code, never a cross-folder reference).

Usage (from yt-channel-automation, with the venv python):
  venv/Scripts/python.exe -m ipshelf.hunt.harvest [--par 120] [--timecap 1800] [--no-prune]

Flow:
  1. Stream `bash refresh_pool.sh` (cwd=ipshelf/hunt) with env:
     PYBIN=<sys.executable>  PAR=<par>  HTTP_CAP=3000  SOCKS_CAP=1200
     SOCKS4_CAP=800  TIMEOUT=8  — output to stdout AND appended to
     <IPSHELF_DATA>/harvest.log.
  2. Load ipshelf/hunt/proxyhunt/pool.json (graded exits).
  3. Merge into the shelf pool via ipshelf.core.shelf.merge_exits()
     (existing channels/first_seen kept; new exits get channels=[] —
     gold supply never disturbs channel bindings).
  4. Print + log a per-region report; write harvest_status.json.

Exit code = pipeline rc (after a successful merge, the merge result is
reported but the pipeline rc stays the source of truth, per contract).

Honest limitation: free public proxy lists cannot be geo-targeted at
harvest time — ALL gold is harvested globally. Region selection happens
at DRAW time (the core assigner filters the merged pool by cc).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))          # .../ipshelf/hunt
HUNT = os.path.join(HERE, "proxyhunt")
POOL_PATH = os.path.join(HUNT, "pool.json")
REFRESH = os.path.join(HERE, "refresh_pool.sh")

# Data dir mirrors the core contract: IPSHELF_DATA env wins, else ipshelf/data.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))     # yt-channel-automation
_IPSHELF = os.path.join(_PROJECT_ROOT, "ipshelf")
DATA_DIR = os.environ.get("IPSHELF_DATA", os.path.join(_IPSHELF, "data"))
LOG_PATH = os.path.join(DATA_DIR, "harvest.log")
STATUS_PATH = os.path.join(DATA_DIR, "harvest_status.json")

T1 = {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO", "FI",
      "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW"}


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = "[%s] %s" % (now_ts(), msg)
    print(line, flush=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        print("[harvest] WARNING: cannot write %s: %s" % (LOG_PATH, exc),
              file=sys.stderr)


def write_status(status):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, STATUS_PATH)


def stream_pipeline(env, timecap):
    """Run `bash refresh_pool.sh`, streaming output to stdout + log. -> rc."""
    proc = subprocess.Popen(
        ["bash", REFRESH],
        cwd=HERE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    started = time.time()
    timed_out = False
    try:
        for line in proc.stdout:
            if timecap and (time.time() - started) > timecap:
                timed_out = True
                break
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
    finally:
        if timed_out:
            proc.kill()
            log("[harvest] TIMECAP %ss hit — pipeline killed mid-run; "
                "increase --timecap or rerun." % timecap)
        rc = proc.wait()
    return 1 if timed_out else rc


def load_graded():
    """Load graded exits from pool.json. Returns list; [] if missing/empty."""
    if not os.path.isfile(POOL_PATH):
        return None
    try:
        with open(POOL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print("[harvest] FATAL: %s unreadable: %s" % (POOL_PATH, exc),
              file=sys.stderr)
        return None
    exits = data.get("exits", [])
    return exits if isinstance(exits, list) else []


def merge_into_shelf(graded_exits):
    """Merge graded exits via ipshelf.core.shelf. Returns (added, refreshed).

    merge_exits keeps existing channels/first_seen and appends new exits with
    channels=[] — the gold supply never disturbs channel bindings. It never
    prunes; pruning/burning is the sweeper's job (--no-prune is accepted on
    the CLI only for call-site symmetry).
    """
    try:
        from ipshelf.core import shelf  # contract: load_pool/save_pool/now_ts/merge_exits
    except ImportError as exc:
        raise SystemExit(
            "[harvest] FATAL: cannot import ipshelf.core.shelf (%s).\n"
            "        The core store is required to merge graded exits into the\n"
            "        shelf pool. Ensure ipshelf/core/shelf.py exists (it may\n"
            "        still be under construction) and that you run this as:\n"
            "          venv/Scripts/python.exe -m ipshelf.hunt.harvest\n"
            "        from the yt-channel-automation directory." % exc)
    pool = shelf.load_pool()
    stats = shelf.merge_exits(pool, graded_exits)
    pool["updated"] = shelf.now_ts()
    shelf.save_pool(pool)
    return int(stats.get("added", 0)), int(stats.get("refreshed", 0))


def region_report(graded_exits):
    """Per-cc counts over the freshly graded exits (top 10) + T1 total."""
    regions = {}
    for e in graded_exits:
        cc = e.get("cc") or "??"
        regions[cc] = regions.get(cc, 0) + 1
    return regions


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m ipshelf.hunt.harvest",
        description="Run the gold hunt pipeline and merge exits into the shelf.")
    ap.add_argument("--par", type=int, default=120,
                    help="pipeline parallelism (default 120)")
    ap.add_argument("--timecap", type=int, default=1800,
                    help="kill the pipeline after N seconds (0 = no cap; default 1800)")
    ap.add_argument("--no-prune", action="store_true",
                    help="accepted for CLI symmetry; merge never prunes bindings")
    args = ap.parse_args(argv)

    started_at = now_ts()
    log("[harvest] START — PYBIN=%s PAR=%d timecap=%ds data=%s"
        % (sys.executable, args.par, args.timecap, DATA_DIR))
    write_status({"running": True, "started_at": started_at})

    env = dict(os.environ)
    env["PYBIN"] = sys.executable            # venv python for refresh_pool.sh
    env["PAR"] = str(args.par)
    # caps the pipeline already honors (raised: only ~7% of scraped proxies
    # were being tested — gold was hiding in the untested 93%)
    env.setdefault("HTTP_CAP", "12000")
    env.setdefault("SOCKS_CAP", "5000")
    env.setdefault("SOCKS4_CAP", "2000")
    env.setdefault("TIMEOUT", "20")

    rc = stream_pipeline(env, args.timecap)

    graded = load_graded()
    if not graded:
        msg = ("[harvest] FATAL: %s missing/empty — pipeline produced no "
               "graded exits (rc=%d). Not merging anything." % (POOL_PATH, rc))
        print(msg, file=sys.stderr)
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (now_ts(), msg))
        except OSError:
            pass
        write_status({"running": False, "started_at": started_at,
                      "finished_at": now_ts(), "added": 0, "refreshed": 0,
                      "total_gold": 0, "rc": 1 if rc == 0 else rc,
                      "regions": {}, "error": "no graded exits"})
        return 1 if rc == 0 else rc

    added, refreshed = merge_into_shelf(graded)

    regions = region_report(graded)
    t1 = sum(n for cc, n in regions.items() if cc in T1)
    top10 = dict(sorted(regions.items(), key=lambda kv: -kv[1])[:10])

    log("[harvest] GOLD REPORT — total graded: %d | T1: %d | shelf merge: "
        "+%d added, %d refreshed" % (len(graded), t1, added, refreshed))
    log("[harvest] regions (top 10): " +
        ", ".join("%s=%d" % (cc, n) for cc, n in top10.items()))

    write_status({
        "running": False,
        "started_at": started_at,
        "finished_at": now_ts(),
        "added": added,
        "refreshed": refreshed,
        "total_gold": len(graded),
        "rc": rc,
        "regions": regions,
    })
    log("[harvest] DONE rc=%d — status at %s" % (rc, STATUS_PATH))
    return rc


if __name__ == "__main__":
    sys.exit(main())
