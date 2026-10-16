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
    # Older runs used `factory-gold-shard-*` / `factory-burn-shard-*`; accept
    # those, plus `harvest-shard-*` (the watch workflow's concurrent harvest,
    # so a harvest cycle is salvageable even when streaming to gold-stream
    # failed mid-run).
    wanted = ("factory-shard-", "factory-gold-shard-", "factory-burn-shard-",
              "harvest-shard-")
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


def fetch_watch_artifacts(repo, token, run_id=None):
    """Download every ghost-runner REPORT of a WATCH run.

    This is the missing half of the runner->rack loop: each watcher VM
    ships a burn_report_<id>.json listing the IPs that died in a real
    browser during the watch. Before this, they were uploaded, verified,
    and then FORGOTTEN — the rack kept serving the same corpses. Returns
    (burn_lists, ip_usage).
    """
    if not run_id:
        run_id = os.environ.get("GITHUB_RUN_ID")
    arts = api(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts",
               token)
    burns, usage = [], {}
    for a in arts.get("artifacts", []):
        if not a["name"].startswith("ghost-report-runner-"):
            continue
        log.info("downloading watch report %s (%d bytes)", a["name"], a["size_in_bytes"])
        raw = download_artifact_zip(repo, token, a["id"])
        z = zipfile.ZipFile(io.BytesIO(raw))
        for name in z.namelist():
            if "burn_report" not in name and "run_report" not in name:
                continue
            try:
                payload = json.loads(z.read(name).decode("utf-8"))
            except Exception:
                continue
            if "burn_report" in name:
                bl = payload.get("burns", []) if isinstance(payload, dict) else payload
                burns.extend(bl if isinstance(bl, list) else [])
            if "run_report" in name and isinstance(payload, dict):
                usage.update(payload.get("ip_usage", {}) or {})
    return burns, usage


def apply_burns(burns, exclude=None):
    """Runner feedback: mark dead exits in the rack. This is what makes the
    per-IP cap real — the rack finally learns what the runners saw.

    `exclude` = addresses this collector just certified itself. A shard tests
    candidates at high parallelism, so a live proxy can time out for one shard
    and be reported dead; without this guard that report would evict gold the
    collector's own final gate has just proven.
    """
    exclude = exclude or set()
    n = 0
    for b in burns:
        addr = b.get("addr")
        if not addr or addr in exclude:
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
        # a plain push dies whenever origin/main moved since checkout (watch
        # #59: 240 burns applied, snapshot rebuilt, push non-FF -> all lost).
        # robust push data-merges the moved origin and replays our commit.
        from hub.git_ops import robust_snapshot_push
        rel = str(config.SNAPSHOT_FILE.relative_to(ROOT)).replace("\\", "/")
        if robust_snapshot_push(ROOT, rel, log=lambda m: log.info("%s", m)):
            log.info("snapshot pushed")
            return True
        log.error("push failed even after merge retries")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", config.REPO))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the broker final gate (trust shard verdicts)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="mode: ingest a WATCH run's burn reports (the "
                         "runner->rack feedback loop) instead of a fleet run")
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

    if args.watch:
        # ── WATCH feedback loop ──
        # Every watcher VM uploads burn_report_<id>.json (IPs that died in a
        # real browser). Ingest them so the rack stops serving the same
        # corpses on the next watch. The runner-side cap/feedback stays
        # correct because the burn is keyed by (addr, proto) as before.
        watch_burns, ip_usage = fetch_watch_artifacts(args.repo, token, args.run_id)
        log.info("watch feedback: %d burn reports + %d ip_usage maps",
                 len(watch_burns), len(ip_usage))
        applied = apply_burns(watch_burns, exclude=set())
        pruned = db.prune_old()      # no exit is stored past its useful life
        watch_burns = [b for b in watch_burns]      # (kept for the summary)
        n = broker.write_snapshot()
        try:
            from hub import allocator
            runners = int(os.environ.get("SLICE_RUNNERS", "20"))
            alloc = allocator.build_sharded_snapshot(runners)
            log.info("allocator (watch side): %s", alloc)
        except Exception as exc:
            log.warning("allocator failed: %s", exc)
        pushed = commit_snapshot(push=not args.no_push)
        summary = {"burn_reports": len(watch_burns), "burned": applied,
                   "pruned_old": pruned,
                   "watch_ips_used": len(ip_usage), "snapshot": n, "pushed": pushed}
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.DATA_DIR / "collect_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        log.info("collect summary: %s", summary)
        print("COLLECT_SUMMARY=" + json.dumps(summary), flush=True)
        return 0

    golds, burns = fetch_run_artifacts(args.repo, token, args.run_id)
    log.info("collected %d gold + %d burn reports from shards", len(golds), len(burns))

    shelved = 0
    shelved_addrs = set()
    if golds:
        if args.no_gate:
            for g in golds:
                if db.upsert_ip(g["addr"], g.get("proto", "HTTP"), source="fleet",
                                **{k: g.get(k) for k in ("cc", "tier", "score",
                                                         "egress_ip", "latency_ms")
                                   if g.get(k) is not None}):
                    shelved += 1
                shelved_addrs.add(g["addr"])
        else:
            # Final-gate parallelism: this gate runs while nothing else is
            # hammering the proxies, so it can be far wider than a shard's.
            # It also has to be: gold dies while it sits in the queue, and a
            # par of 250 for ~1900 candidates took 597s.
            par = int(os.environ.get("BROKER_PAR", "600"))
            res = broker.accept_gold(golds, source="fleet", par=par)
            shelved = res["shelved"]
            shelved_addrs = res.get("shelved_addrs") or set()
            log.info("broker: %s", {k: v for k, v in res.items()
                                    if k != "shelved_addrs"})

    # Burns come AFTER the gold, and never over an address this collector just
    # certified itself: a shard under high parallelism reports live proxies as
    # dead, and applying that blindly would evict gold proven seconds earlier.
    applied = apply_burns(burns, exclude=shelved_addrs)
    pruned = db.prune_old()          # nothing is kept past its useful life

    # ── SAFETY: never wipe a good snapshot because a harvest failed ──
    # (a fleet run that collected 0 gold must NOT overwrite the committed
    #  snapshot with an empty one — that silently destroys the shelf for
    #  every future watch)
    rack_gold = db.rack_stats()["gold"]
    if rack_gold == 0 and not golds:
        log.error("rack EMPTY and no shard gold — refusing to write/commit an "
                  "empty snapshot (keeps the previous snapshot intact)")
        summary = {"shards_gold": len(golds), "burn_reports": len(burns),
                   "burned": applied, "shelved": shelved,
                   "pruned_old": pruned, "snapshot": -1,
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
        # The watch workflow now rebuilds its own slices for its actual runner
        # count; these are committed for inspection/offline slots, so they
        # default to the account's job ceiling rather than a stale guess.
        runners = int(os.environ.get("SLICE_RUNNERS", "20"))
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