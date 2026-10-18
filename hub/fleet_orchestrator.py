"""fleet_orchestrator.py — gate-based progressive fleet dispatch (the hub's
"start runners one by one, only when the shelf can feed them" design).

Runs on the CENTRAL HUB (your dashboard PC) — the one piece that is always
present. The 18 watch runners are ephemeral; they cannot hold state, and a
watch fired against an empty/near-empty shelf wastes its whole startup window
parking. So this module:

  1. reads the SHELF (local hub.db rack if the PC runs the factory, else the
     committed data/ip_pool_snapshot.json via the GitHub API),
  2. waits until shelf gold >= the minimum a runner can consume
     (max(runners_supported) = floor(gold / (tabs_per_runner * margin))),
  3. dispatches ONE workflow run sized to what the shelf can feed, and
  4. re-checks each poll: if the pump keeps topping the pool, it re-dispatches
     a FURTHER wave with more runners (a new workflow run whose extra runner
     jobs pick up the fresh gold — mid-run tab growth is impossible inside a
     fixed-matrix job, so growth = dispatch of the next wave).

`margin` covers the known reality that ~50-80% of free gold dies between the
snapshot verdict and the browser actually using it, so it deliberately
under-fills: parked tabs re-draw from the stream as it refreshes.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SNAPSHOT = ROOT / "data" / "ip_pool_snapshot.json"


def shelf_gold_count(use_gh=False, repo="", token=""):
    """Current gold count in the shelf. Local DB path (hub.rack_stats) when
    the dashboard PC runs the factory; else pull the committed snapshot
    through the GitHub contents API (read-only, no git checkout needed)."""
    if not use_gh:
        try:
            from hub import db
            db.init()
            return db.rack_stats().get("gold", 0)
        except Exception:
            pass
    if use_gh and repo and token:
        try:
            url = (f"https://api.github.com/repos/{repo}/contents/"
                   f"data/ip_pool_snapshot.json")
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.raw",
                "User-Agent": "fleet-orchestrator",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                snap = json.loads(r.read().decode("utf-8"))
            return len(snap.get("exits", []))
        except Exception:
            return 0
    try:
        return len(json.loads(
            SNAPSHOT.read_text(encoding="utf-8")).get("exits", []))
    except Exception:
        return 0


def _dispatch(repo, token, url, tabs, runners, harvest_shards, depth_lo,
              depth_hi, human_moves, unique_ips):
    """Thin call into server.dispatch_workflow without importing the whole
    server (it also binds HTTP). Returns (ok, message)."""
    sys.path.insert(0, str(ROOT))
    from server import dispatch_workflow
    return dispatch_workflow(
        repo, token, url, tabs,
        stream_target=os.environ.get("RELAY_URL", ""),
        runners=runners, harvest_shards=harvest_shards,
        depth_lo=depth_lo, depth_hi=depth_hi,
        human_moves=human_moves, unique_ips=unique_ips)


def run(loop=True, interval=120):
    """Gate-based dispatch loop. Blocks; run from a thread or a CLI."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    use_gh = bool(repo and token and os.environ.get("ORCH_USE_GH", "0") == "1")
    url = os.environ.get("ORCH_VIDEO_URL",
                                os.environ.get("FACTORY_VIDEO_URL", ""))
    tabs = int(os.environ.get("ORCH_TABS_PER_RUNNER", "12"))
    max_runners = int(os.environ.get("ORCH_MAX_RUNNERS", "18"))
    harvest_shards = int(os.environ.get("ORCH_HARVEST_SHARDS", "2"))
    margin = float(os.environ.get("ORCH_MARGIN", "1.5"))
    depth_lo = int(os.environ.get("ORCH_DEPTH_LO", "20"))
    depth_hi = int(os.environ.get("ORCH_DEPTH_HI", "45"))
    human_moves = os.environ.get("ORCH_HUMAN_MOVES", "true") not in ("0", "false")
    unique_ips = os.environ.get("ORCH_UNIQUE_IPS", "true") not in ("0", "false")
    cooldown = int(os.environ.get("ORCH_REDISPATCH_COOLDOWN", "600"))

    if not url:
        sys.stderr.write("fleet_orchestrator: no ORCH_VIDEO_URL — exit\n")
        return

    dispatched = set()          # runner counts already dispatched
    last_wave_at = [0.0]
    prev_count = [0]
    print(f"[orchestrator] url={url} tabs={tabs} max_runners={max_runners} "
          f"margin={margin} repo={repo or 'local-db'}")

    # the frontier of runner counts we will step through. Only whole-integer
    # counts are dispatched (GitHub's runners input); ascending so a fresh
    # pool launches few runners and grows as the pump tops it up.
    _SUPPORTED = list(range(1, max_runners + 1))

    while True:
        gold = shelf_gold_count(use_gh=use_gh, repo=repo, token=token)
        support = max(0, int(gold / max(1, tabs * margin)))
        goal = min(max_runners, support)
        if goal != prev_count[0]:
            print(f"[orchestrator] shelf gold={gold} -> support {goal} runners")
            prev_count[0] = goal
        waited = time.time() - last_wave_at[0]
        best_new = max([g for g in _SUPPORTED if g not in dispatched and g <= goal], default=0)
        if best_new and waited >= cooldown:
            ok, msg = _dispatch(repo, token, url, tabs, best_new, harvest_shards,
                                depth_lo, depth_hi, human_moves, unique_ips)
            print(f"[orchestrator] wave: dispatch {best_new} runners -> {msg}")
            if ok:
                dispatched.add(best_new)
                last_wave_at[0] = time.time()
        if not loop:
            return goal
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="single check, no dispatches, then exit")
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args()
    if args.once:
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        use_gh = bool(repo and token and os.environ.get("ORCH_USE_GH", "0") == "1")
        gold = shelf_gold_count(use_gh=use_gh, repo=repo, token=token)
        print(f"once: shelf gold = {gold}")
        return
    run(loop=True, interval=args.interval)


if __name__ == "__main__":
    main()
