"""factory_collect.py — merge the Gold Factory Fleet's shard gold.

Pulls the latest `Gold Factory Fleet` run's factory-gold-shard-* artifacts,
merges every certified exit into the shelf, rebuilds the snapshot and
(optionally, --ship) commits + pushes it so cloud watchers ride minutes-old
gold.

Usage (token with Actions:read on the repo):
  GITHUB_TOKEN=ghp_xxx python factory_collect.py [--repo owner/name] [--ship] [--run-id N]
"""
import argparse
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ipshelf.core import shelf                       # noqa: E402
from gold_factory import rebuild_snapshot, T1        # noqa: E402


def api(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "factory-collect"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY",
                                                     "Zarifbenalam-2/yt-ghost-runner"))
    ap.add_argument("--run-id", type=int, default=0, help="fleet run id (default: latest)")
    ap.add_argument("--ship", action="store_true", help="commit+push the refreshed snapshot")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("FATAL: set GITHUB_TOKEN", file=sys.stderr)
        return 2

    if args.run_id:
        run = api(f"https://api.github.com/repos/{args.repo}/actions/runs/{args.run_id}", token)
    else:
        runs = api(f"https://api.github.com/repos/{args.repo}/actions/workflows/"
                   f"gold_factory.yml/runs?per_page=1", token)
        if not runs.get("workflow_runs"):
            print("No fleet runs found", file=sys.stderr)
            return 1
        run = runs["workflow_runs"][0]
    print(f"collecting from run #{run['run_number']} ({run['status']}/{run['conclusion']})")

    arts = api(f"https://api.github.com/repos/{args.repo}/actions/runs/{run['id']}/artifacts", token)
    gold_all = []
    for a in arts.get("artifacts", []):
        if not a["name"].startswith("factory-gold-shard-"):
            continue
        raw = urllib.request.urlopen(urllib.request.Request(
            f"https://api.github.com/repos/{args.repo}/actions/artifacts/{a['id']}/zip",
            headers={"Authorization": f"Bearer {token}"}), timeout=60).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        for name in z.namelist():
            try:
                gold_all.extend(json.loads(z.read(name).decode("utf-8")))
            except Exception as exc:
                print(f"[warn] {a['name']}/{name}: {exc}")

    t1 = sum(1 for g in gold_all if g.get("cc") in T1)
    print(f"collected {len(gold_all)} gold from shards (T1: {t1})")
    if not gold_all:
        return 1

    pool = shelf.load_pool()
    res = shelf.merge_exits(pool, gold_all)
    shelf.save_pool(pool)
    total = sum(1 for e in pool.get("exits", []) if e.get("status") == "gold")
    print(f"shelf merge: +{res['added']} added, {res['refreshed']} refreshed (total gold {total})")

    n = rebuild_snapshot()
    print(f"snapshot rebuilt: {n} exits")
    if args.ship:
        changed = subprocess.run(
            ["git", "status", "--porcelain", "data/ip_pool_snapshot.json"],
            cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
        if changed:
            subprocess.run(["git", "add", "data/ip_pool_snapshot.json"], cwd=str(ROOT), check=True)
            subprocess.run(["git", "commit", "-m",
                            f"gold-fleet: +{res['added']} gold from fleet run #{run['run_number']} [skip-ci]"],
                           cwd=str(ROOT), check=True)
            r = subprocess.run(["git", "push"], cwd=str(ROOT), capture_output=True, text=True, timeout=90)
            print("pushed" if r.returncode == 0 else f"push failed: {r.stderr[:200]}")
        else:
            print("snapshot unchanged — nothing to push")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
