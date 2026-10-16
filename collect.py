"""collect.py — fleet collector: merge shard harvests into the rack + snapshot.

Runs ON A GITHUB RUNNER, as the final job of the factory fleet, after all
harvest shards have uploaded their gold artifacts. Replaces the dead-ended
factory_collect.py (which had no caller, so every fleet run's gold was
thrown away).

What it does:
  1. downloads every factory-gold-shard-* artifact of this run
  2. merges + dedupes them through the BROKER (final gate included)
  3. applies every shard's BURN REPORT to the rack (the feedback the
     runners could not previously give)
  4. writes the snapshot and commits it back, so the next watch rides
     gold that is minutes old — and the operator's PC does nothing

Environment:
  GITHUB_TOKEN            token with Actions:read + repo contents:write
  GITHUB_REPOSITORY       owner/repo (set automatically by Actions)
  COLLECT_RUN_ID          the fleet run id to collect (defaults to current)
  BROKER_GATE             'true' to run the live final gate (default true)
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from hub import config, db, broker          # noqa: E402
from hub.logging_setup import get_logger    # noqa: E402

log = get_logger("collect")


def api(url, token, timeout=40):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghost-collector"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def download_artifact_zip(repo, token, artifact_id, timeout=120):
    """Download an artifact zip WITH auth.

    GitHub redirects artifact downloads to a signed blob URL; urllib follows
    the redirect but drops the Authorization header on the cross-host hop,
    which the blob store answers with HTTP 401. Passing the token only to
    api.github.com and letting urllib follow is not enough, so we fetch the
    redirect target and re-request it WITHOUT the header (the signed URL does
    not want it).
    """
    api_url = (f"https://api.github.com/repos/{repo}/actions/artifacts/"
               f"{artifact_id}/zip")
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ghost-collector"})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()          # no redirect: direct body (rare)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        signed = e.headers.get("Location")
        if not signed:
            raise
    log.info("following signed artifact URL (no auth header)")
    with urllib.request.urlopen(signed, timeout=timeout) as r:
        return r.read()


def fetch_run_artifacts(repo, token, run_id=None):
    """Download all shard gold + burn artifacts. Returns (golds, burns)."""
    if not run_id:
        run_id = os.environ.get("GITHUB_RUN_ID")
    arts = api(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts",
               token)
    golds, burns = [], []
    # artifact names are `factory-shard-<n>` (gold + burn files inside).
    # Older runs used `factory-gold-shard-*` / `factory-burn-shard-*`; accept both.
    wanted = ("factory-shard-", "factory-gold-shard-", "factory-burn-shard-")
    for a in arts.get("artifacts", []):
        if not a["name"].startswith(wanted):
            continue
        log.info("downloading %s (%d bytes)", a["name"], a["size_in_bytes"])
        raw = download_artifact_zip(repo, token, a["id"])
        z = zipfile.ZipFile(io.BytesIO(raw))
        for name in z.namelist():
            try:
                payload = json.loads(z.read(name).decode("utf-8"))
            except Exception as exc:
                log.warning("%s/%s unreadable: %s", a["name"], name, exc)
                continue
            # route by the FILE name inside the artifact, not the artifact name
            if "burn_shard" in name or "burn-shard" in name:
                burns.extend(payload if isinstance(payload, list) else [])
            elif "gold_shard" in name or "gold-shard" in name:
                golds.extend(payload if isinstance(payload, list) else [])
    return golds, burns


def apply_burns(burns):
    """Runner feedback: mark dead exits in the rack. This is what makes the
    per-IP cap real — the rack finally learns what the runners saw."""
    n = 0
    for b in burns:
        addr = b.get("addr")
        if not addr:
            continue
        db.burn_ip(addr, b.get("proto"), reason=b.get("reason", "runner_reported"))
        n += 1
    if n:
        db.record_run("gate", "collector", "warning",
                      f"applied {n} runner burn reports", count=n)
    return n


def commit_snapshot(push=True):
    """Commit + push the freshly written snapshot so the next watch has it."""
    subprocess.run(["git", "add", str(config.SNAPSHOT_FILE)], cwd=str(ROOT), check=False)
    # include the per-runner slices (the global cap) in the same commit
    import glob as _glob
    for shard in _glob.glob(str(config.DATA_DIR / "snapshot_shard_*.json")):
        subprocess.run(["git", "add", shard], cwd=str(ROOT), check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(ROOT))
    if diff.returncode == 0:
        log.info("snapshot unchanged — nothing to commit")
        return False
    subprocess.run(["git", "config", "user.email", "ghost-collector@users.noreply.github.com"],
                   cwd=str(ROOT), check=False)
    subprocess.run(["git", "config", "user.name", "ghost-collector"], cwd=str(ROOT), check=False)
    subprocess.run(["git", "commit", "-m",
                    f"gold-fleet: collector merged shards -> snapshot [skip-ci]"],
                   cwd=str(ROOT), check=False)
    if push:
        r = subprocess.run(["git", "push"], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=120)
        if r.returncode == 0:
            log.info("snapshot pushed")
            return True
        log.error("push failed: %s", (r.stderr or "")[:300])
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", config.REPO))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the broker final gate (trust shard verdicts)")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.error("GITHUB_TOKEN not set — cannot collect artifacts")
        return 2

    db.init()

    # ── bootstrap the rack from the committed snapshot ──
    # hub.db is NOT committed (live mutable state), so a runner's registry
    # starts empty. Hydrate it from the existing snapshot first, so the
    # merge ADDS to the known-good shelf instead of replacing it. Without
    # this the collector would see an empty rack and (correctly, per the
    # guard below) refuse to write — or worse, write an empty snapshot.
    try:
        snap_path = Path(config.SNAPSHOT_FILE)
        if snap_path.is_file():
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            seeded = 0
            for e in snap.get("exits", []):
                if e.get("addr"):
                    db.upsert_ip(e["addr"], e.get("proto", "HTTP"), cc=e.get("cc"),
                                 tier=e.get("tier"), score=e.get("score"),
                                 egress_ip=e.get("egress_ip"),
                                 latency_ms=e.get("latency_ms"), source="seed")
                    seeded += 1
            log.info("bootstrapped rack from snapshot: %d exits", seeded)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("snapshot bootstrap failed: %s", exc)

    golds, burns = fetch_run_artifacts(args.repo, token, args.run_id)
    log.info("collected %d gold + %d burn reports from shards", len(golds), len(burns))

    applied = apply_burns(burns)

    shelved = 0
    if golds:
        if args.no_gate:
            for g in golds:
                if db.upsert_ip(g["addr"], g.get("proto", "HTTP"), source="fleet",
                                **{k: g.get(k) for k in ("cc", "tier", "score",
                                                         "egress_ip", "latency_ms")
                                   if g.get(k) is not None}):
                    shelved += 1
        else:
            res = broker.accept_gold(golds, source="fleet", par=250)
            shelved = res["shelved"]
            log.info("broker: %s", res)

    # ── SAFETY: never wipe a good snapshot because a harvest failed ──
    # (a fleet run that collected 0 gold must NOT overwrite the committed
    #  snapshot with an empty one — that silently destroys the shelf for
    #  every future watch)
    rack_gold = db.rack_stats()["gold"]
    if rack_gold == 0 and not golds:
        log.error("rack EMPTY and no shard gold — refusing to write/commit an "
                  "empty snapshot (keeps the previous snapshot intact)")
        summary = {"shards_gold": len(golds), "burn_reports": len(burns),
                   "burned": applied, "shelved": shelved, "snapshot": -1,
                   "pushed": False, "skipped": "empty_rack_guard"}
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.DATA_DIR / "collect_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        print("COLLECT_SUMMARY=" + json.dumps(summary), flush=True)
        return 0

    n = broker.write_snapshot()
    # ── build per-runner SLICES so the global per-IP cap holds ──
    # (proven necessary: watch run #43 put 13+ tabs on one IP because every
    #  runner independently picked the same top-scored address)
    try:
        from hub import allocator
        runners = int(os.environ.get("SLICE_RUNNERS", "4"))
        alloc = allocator.build_sharded_snapshot(runners)
        log.info("allocator (commit side): %s", alloc)
    except Exception as exc:
        log.warning("allocator failed: %s", exc)
    pushed = commit_snapshot(push=not args.no_push)

    summary = {"shards_gold": len(golds), "burn_reports": len(burns),
               "burned": applied, "shelved": shelved, "snapshot": n, "pushed": pushed}
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.DATA_DIR / "collect_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    log.info("collect summary: %s", summary)
    print("COLLECT_SUMMARY=" + json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())