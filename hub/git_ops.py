"""git_ops.py — push helpers that survive origin/main moving mid-run.

Watch #59's post-collect proved the failure mode: it applied 240 burns,
rebuilt the snapshot (1722 -> 1551), committed... and the push failed
because origin/main advanced between the job's checkout and its push (a fix
push landed in that window). The cleaned snapshot died on the runner and the
next watch drew from the dirty catalog anyway.

robust_snapshot_push() closes that: on push failure it fetches the new
origin, merges the two snapshots in DATA space (union by (addr, proto),
freshest gated_at wins — so another collector's new gold is never lost,
and our burns still evict corpses), replays the result as one commit on
top of origin/main, and retries.
"""
import json
import subprocess
import time
from pathlib import Path


def _run(args, cwd, timeout=120):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def merge_snapshot_exits(remote, local):
    """Union two snapshot exit lists; freshest gate verdict wins per addr."""
    merged = {}
    for e in remote or []:
        if e.get("addr"):
            merged[(e["addr"], e.get("proto", "HTTP"))] = e
    for e in local or []:
        if not e.get("addr"):
            continue
        k = (e["addr"], e.get("proto", "HTTP"))
        r = merged.get(k)
        if r is None or (e.get("gated_at") or 0) >= (r.get("gated_at") or 0):
            merged[k] = e
    order = {"T1": 0}
    out = sorted(merged.values(),
                 key=lambda e: (order.get(e.get("tier"), 1),
                                -(e.get("score") or 0)))
    return out


def robust_snapshot_push(root, snapshot_rel, attempts=4, log=print):
    """Push the committed snapshot; on a moved ref, data-merge and replay.

    `snapshot_rel` is repo-relative, e.g. "data/ip_pool_snapshot.json".
    The caller must already have its snapshot written + committed (a plain
    `git commit`); this handles the push, including the non-fast-forward
    recovery. Returns True if origin ended up with our snapshot.
    """
    root = Path(root)
    snap = root / snapshot_rel
    for attempt in range(attempts):
        r = _run(["push"], cwd=root)
        if r.returncode == 0:
            return True
        log(f"push failed (attempt {attempt + 1}): "
            f"{(r.stderr or '').strip()[:160]} — merging origin and retrying")
        try:
            _run(["fetch", "origin"], cwd=root)
            show = _run(["show", f"origin/main:{snapshot_rel}"], cwd=root)
            if show.returncode == 0 and snap.is_file():
                remote = json.loads(show.stdout or "{}")
                local = json.loads(snap.read_text(encoding="utf-8"))
                merged = merge_snapshot_exits(remote.get("exits"),
                                              local.get("exits"))
                snap.write_text(json.dumps(
                    {"snapshotted_at": local.get("snapshotted_at"),
                     "exits": merged}, indent=2), encoding="utf-8")
                log(f"data-merge: remote {len(remote.get('exits', []))} + "
                    f"local {len(local.get('exits', []))} -> {len(merged)} exits")
            # replay our content as ONE commit on top of the new origin/main
            _run(["reset", "--soft", "origin/main"], cwd=root)
            _run(["add", snapshot_rel], cwd=root)
            for extra in (root / "data").glob("snapshot_shard_*.json"):
                _run(["add", str(extra.relative_to(root))], cwd=root)
            diff = _run(["diff", "--cached", "--quiet"], cwd=root)
            if diff.returncode == 0:
                log("after merge nothing differs from origin/main — done")
                return True
            _run(["-c", "user.email=ghost-collector@users.noreply.github.com",
                  "-c", "user.name=ghost-collector", "commit", "-m",
                  "gold-fleet: collector merged shards -> snapshot [skip-ci]"],
                 cwd=root)
        except Exception as e:
            log(f"merge-recovery error: {e}")
        time.sleep(3 * (attempt + 1))
    log("robust push failed after all attempts")
    return False
