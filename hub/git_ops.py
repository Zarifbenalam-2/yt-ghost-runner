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
import os
import subprocess
import time
from pathlib import Path


def _run(args, cwd, timeout=120):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def fetch_branch(root, branch, remote="origin"):
    """Fetch `branch` into refs/remotes/<remote>/<branch> EXPLICITLY.

    Plain `git fetch origin <branch>` does NOT create the tracking ref in a
    shallow clone — and actions/checkout gives every runner a depth-1 clone.
    That single fact silently broke three subsystems: gold-stream pushes
    (no origin/gold-stream -> the shard committed onto main and its push was
    rejected as non-FF while still logging success), the workflow's stream
    fold-in step, and the watcher's mid-run pool refresh. An explicit
    src:dst refspec creates the ref in shallow clones.
    """
    r = _run(["fetch", remote,
              f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"],
             root, timeout=120)
    return r.returncode == 0


def remote_tip(root, branch, remote="origin"):
    r = _run(["rev-parse", "--verify", "--quiet",
              f"refs/remotes/{remote}/{branch}"], root)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def materialize_branch_dir(root, branch, subdir, dest, remote="origin"):
    """Write every file of <branch>:<subdir>/ into <dest>/ locally.

    Reads blobs through `git show` — no checkout, so the working tree/index
    of a live run are untouched (unlike `git checkout <remote-ref> -- path`,
    which also fails outright on the shallow clones runners get).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ref = f"refs/remotes/{remote}/{branch}:{subdir}/"
    ls = _run(["ls-tree", "--name-only", ref], root)
    written = 0
    for name in (ls.stdout or "").split():
        show = _run(["show", f"refs/remotes/{remote}/{branch}:{subdir}/{name}"],
                    root)
        if show.returncode != 0:
            continue
        (dest / name).write_text(show.stdout, encoding="utf-8")
        written += 1
    return written


def push_plumbing(root, branch, rel_path, content, message,
                  merge_snapshot=False, log=print, attempts=4):
    """Commit ONE file on top of <branch>'s current tip and push it.

    Plumbing (private index + commit-tree) instead of checkout/rebase: it
    works in shallow clones, never touches the working tree of a live run,
    and needs no merge base. Each retry re-reads the tip and re-applies, so
    concurrent sibling pushes (many runners, same branch) resolve instead of
    rejecting. Verifies the remote actually moved before reporting success —
    a rejected push used to be indistinguishable from a landed one.
    """
    root = Path(root)
    ident = {"GIT_AUTHOR_NAME": "ghost-live", "GIT_AUTHOR_EMAIL":
             "ghost-live@users.noreply.github.com",
             "GIT_COMMITTER_NAME": "ghost-live", "GIT_COMMITTER_EMAIL":
             "ghost-live@users.noreply.github.com"}
    idx = str(root / ".git" / f"plumb_index_{branch}")
    last_err = None
    for attempt in range(attempts):
        try:
            fetch_branch(root, branch)
            base = remote_tip(root, branch)
            ge = dict(ident)
            ge["GIT_INDEX_FILE"] = idx
            if base:
                subprocess.run(["git", "read-tree", base], cwd=str(root),
                               capture_output=True, env={**os.environ, **ge})
            else:
                subprocess.run(["git", "read-tree", "--empty"], cwd=str(root),
                               capture_output=True, env={**os.environ, **ge})
            # merge the remote copy of our file if it exists (data space)
            if base and merge_snapshot:
                show = _run(["show", f"refs/remotes/origin/{branch}:{rel_path}"],
                            root)
                if show.returncode == 0:
                    try:
                        remote = json.loads(show.stdout or "{}")
                        local = json.loads(content)
                        merged = merge_snapshot_exits(remote.get("exits"),
                                                      local.get("exits"))
                        content = json.dumps(
                            {"snapshotted_at": local.get("snapshotted_at"),
                             "exits": merged}, indent=2)
                    except (json.JSONDecodeError, TypeError):
                        pass
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"],
                                  cwd=str(root), input=content, capture_output=True,
                                  text=True)
            if blob.returncode != 0:
                raise RuntimeError(f"hash-object: {blob.stderr[:120]}")
            up = subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                                 f"100644,{blob.stdout.strip()},{rel_path}"],
                                cwd=str(root), capture_output=True, text=True,
                                env={**os.environ, **ge})
            if up.returncode != 0:
                raise RuntimeError(f"update-index: {up.stderr[:120]}")
            tree = subprocess.run(["git", "write-tree"], cwd=str(root),
                                  capture_output=True, text=True,
                                  env={**os.environ, **ge})
            if tree.returncode != 0:
                raise RuntimeError(f"write-tree: {tree.stderr[:120]}")
            cargs = ["commit-tree", tree.stdout.strip(), "-m", message]
            if base:
                cargs += ["-p", base]
            commit = subprocess.run(["git"] + cargs, cwd=str(root),
                                    capture_output=True, text=True,
                                    env={**os.environ, **ident})
            if commit.returncode != 0:
                raise RuntimeError(f"commit-tree: {commit.stderr[:120]}")
            sha = commit.stdout.strip()
            pr = _run(["push", "origin", f"{sha}:refs/heads/{branch}"], root,
                      timeout=120)
            # verify: the remote must actually point at our commit now
            fetch_branch(root, branch)
            if remote_tip(root, branch) == sha:
                log(f"pushed {rel_path} -> {branch} ({sha[:8]})")
                return True
            last_err = (pr.stderr or "push reported ok but remote did not move")[:200]
        except Exception as e:
            last_err = str(e)[:200]
        time.sleep(2 * (attempt + 1))
    log(f"push to {branch} failed after {attempts} attempts: {last_err}")
    return False


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
