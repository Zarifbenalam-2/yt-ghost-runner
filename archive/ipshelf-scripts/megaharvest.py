#!/usr/bin/env python3
"""megaharvest.py — bulk supply run: loop harvest rounds until the live
shelf holds >= --target gold exits, then sweep ALL dead gold out and
deliver the cleaned pool.

Per round (default):
  python -m ipshelf.hunt.harvest --par 150 --timecap 2400
  with raised intake caps (HTTP 15000 / SOCKS5 8000 / SOCKS4 3000, TO 6s)
Rounds repeat every --gap minutes (public proxy lists refresh hourly;
re-running after 30-60 min surfaces new exits).
Stops when gold >= target or --deadline hours have passed, whichever
comes first. Then:
  1. sweeper.sweep()  — re-gate EVERY gold; burn dead, resolve ?? geo
  2. final count report
  3. copy pool into ipshelf-actions/data + git commit + push
     (push failure is logged, never loses the local pool)

Built to run DETACHED (survives session rollovers):
  cmd //c start //b venv\\Scripts\\python.exe ipshelf\\megaharvest.py
Progress: ipshelf/data/megaharvest.log + megaharvest_status.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent          # yt-channel-automation/
sys.path.insert(0, str(PROJECT))
from ipshelf.core import shelf, sweeper                    # noqa: E402

DATA = Path(os.environ.get("IPSHELF_DATA", str(PROJECT / "ipshelf" / "data")))
ACTIONS = PROJECT / "ipshelf-actions"
LOG = DATA / "megaharvest.log"
STATUS = DATA / "megaharvest_status.json"


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_status(state, **extra):
    doc = {"state": state, "ts": datetime.now(timezone.utc).isoformat(),
           "pid": os.getpid()}
    doc.update(extra)
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    except OSError:
        pass


def gold_count():
    pool = shelf.load_pool()
    exits = pool.get("exits", [])
    golds = [e for e in exits if e.get("status") == "gold"]
    t1 = sum(1 for e in golds if e.get("tier") == "T1")
    return len(golds), t1, len(exits)


def run_round(par, timecap):
    env = dict(os.environ)
    env["IPSHELF_DATA"] = str(DATA)
    env["HTTP_CAP"] = "15000"
    env["SOCKS_CAP"] = "8000"
    env["SOCKS4_CAP"] = "3000"
    env["TIMEOUT"] = "6"
    cmd = [sys.executable, "-m", "ipshelf.hunt.harvest",
           "--par", str(par), "--timecap", str(timecap)]
    log(f"round start: {' '.join(cmd[2:])} caps=15000/8000/3000")
    rc = subprocess.call(cmd, cwd=str(PROJECT), env=env)
    log(f"round done rc={rc} (rc=1 after a timecap kill is normal)")
    return rc


def deliver(n_gold):
    """Copy the cleaned pool into the actions repo and push it."""
    try:
        (ACTIONS / "data").mkdir(parents=True, exist_ok=True)
        pool = shelf.load_pool()
        (ACTIONS / "data" / "shelf_pool.json").write_text(
            json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8")
        hs = DATA / "harvest_status.json"
        if hs.is_file():
            (ACTIONS / "data" / "harvest_status.json").write_text(
                hs.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(["git", "add", "data"], cwd=str(ACTIONS), check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=str(ACTIONS)).returncode != 0
        if staged:
            subprocess.run(["git", "commit", "-m",
                            f"pool delivery: {n_gold} gold (swept)"],
                           cwd=str(ACTIONS), check=True)
            subprocess.run(["git", "push"], cwd=str(ACTIONS), check=True)
            log(f"delivered to GitHub: {n_gold} gold")
        else:
            log("deliver: no pool changes to commit")
    except Exception as e:  # push must never lose the local pool
        log(f"deliver FAILED (local pool is safe): {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--deadline", type=float, default=6.0, help="hours")
    ap.add_argument("--gap", type=int, default=10, help="minutes between rounds")
    ap.add_argument("--par", type=int, default=220)
    ap.add_argument("--timecap", type=int, default=4800,
                    help="sec per round (blast of ~26k candidates at par 220 "
                         "needs 50-80 min; a killed round merges NOTHING "
                         "because pool.json is written only at the end)")
    args = ap.parse_args()

    started = time.time()
    deadline = started + args.deadline * 3600
    g, t1, total = gold_count()
    log(f"megaharvest start: target={args.target} gold, deadline={args.deadline}h, "
        f"current pool: {g} gold ({t1} T1) / {total} exits")
    write_status("running", target=args.target, gold=g)

    round_no = 0
    tc = args.timecap          # adaptive: raised when a round times out empty
    while g < args.target and time.time() < deadline:
        round_no += 1
        write_status("running", round=round_no, gold=g, target=args.target)
        g_before = g
        run_round(args.par, tc)
        g, t1, total = gold_count()
        gained = g - g_before
        log(f"after round {round_no}: {g} gold ({t1} T1) / {total} exits "
            f"(+{gained} this round, timecap was {tc}s)")
        write_status("running", round=round_no, gold=g, t1=t1,
                     target=args.target, gained=gained)
        if gained == 0 and tc < 7200:
            tc = min(int(tc * 1.5), 7200)
            log(f"round gained 0 — next round timecap raised to {tc}s "
                f"(rounds must COMPLETE; a timecap kill merges nothing)")
        if g >= args.target or time.time() + args.gap * 60 >= deadline:
            break
        log(f"sleeping {args.gap} min (lists refresh hourly)")
        time.sleep(args.gap * 60)

    log(f"harvest loop finished at {g}/{args.target} gold — sweeping ALL dead now")
    write_status("sweeping", gold=g)
    try:
        report = sweeper.sweep(max_workers=12)
        log(f"sweep report: {report}")
    except Exception as e:
        log(f"sweep FAILED (pool untouched): {e}")
        report = {}

    g, t1, total = gold_count()
    log(f"FINAL after sweep: {g} gold ({t1} T1) / {total} exits, "
        f"wall time {(time.time() - started) / 3600:.1f}h")
    write_status("done", gold=g, t1=t1, exits=total, sweep=report)
    deliver(g)
    log("megaharvest complete")


if __name__ == "__main__":
    main()
