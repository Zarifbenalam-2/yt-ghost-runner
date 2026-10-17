"""git_ops.py — push helpers that survive origin/main moving mid-run.

Watch #59's post-collect proved the failure mode: it applied 240 burns,
rebuilt the snapshot (1722 -> 1551), committed... and the push failed
because origin/main advanced between the job's checkout and its push (a fix
push landed in that window). The cleaned snapshot died on the runner and the
next watch drew from the dirty catalog anyway.

EVERY push here builds its commit from the CURRENT remote tip and writes only
the named data paths (plumbing: private index, read-tree, update-index,
write-tree, commit-tree, verified push). That is not a style choice:
  * a plain `git push` is non-FF whenever origin moved since checkout;
  * the `reset --soft origin/main` + `git add` replay that used to recover
    from that re-committed the STALE workflow blobs from the runner's depth-1
    checkout, and GitHub refuses to let a runner/App token create or update
    `.github/workflows/*` — "refusing to allow a GitHub App to create or
    update workflow". The collector kept reporting its gold (harvest #8: 1,419
    harvested, 394 shelved) while the shelf sat at 0 exits for hours.
A tree built from the remote tip cannot carry anything but the files we name.
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


def push_files_plumbing(root, branch, files, message, merge_json=None,
                        log=print, attempts=4):
    """Commit one or more files on top of <branch>'s tip; push; verify.

    The tree is `read-tree <remote tip>` plus ONLY the named paths, so the
    commit can never carry unrelated changes. That matters: GitHub refuses
    to let a runner/App token create or update `.github/workflows/*`, and the
    old replay path (`reset --soft origin/main` + `git add`) kept the stale
    index from the depth-1 checkout — so a retry commit silently REVERTED the
    workflow files that had landed on main since checkout. The push was then
    rejected 4x with "refusing to allow a GitHub App to create or update
    workflow", the collector logged its gold and the shelf stayed empty for
    hours. Building the tree from the remote tip makes that impossible.

    files: {repo_relative_path: text}. Paths listed in `merge_json` are
    data-merged (exits union, freshest gated_at wins) against the remote copy
    before being written, so a sibling collector's gold is never lost and our
    burns still evict corpses.
    """
    root = Path(root)
    merge_json = set(merge_json or ())
    ident = {"GIT_AUTHOR_NAME": "ghost-collector", "GIT_AUTHOR_EMAIL":
             "ghost-collector@users.noreply.github.com",
             "GIT_COMMITTER_NAME": "ghost-collector", "GIT_COMMITTER_EMAIL":
             "ghost-collector@users.noreply.github.com"}
    idx = str(root / ".git" / f"plumb_index_{branch}")
    last_err = None
    for attempt in range(attempts):
        try:
            fetch_branch(root, branch)
            base = remote_tip(root, branch)
            ge = dict(ident)
            ge["GIT_INDEX_FILE"] = idx
            # tree starts EXACTLY at the remote tip — nothing else can ride along
            if base:
                subprocess.run(["git", "read-tree", base], cwd=str(root),
                               capture_output=True, env={**os.environ, **ge})
            else:
                subprocess.run(["git", "read-tree", "--empty"], cwd=str(root),
                               capture_output=True, env={**os.environ, **ge})
            for rel, content in files.items():
                if rel in merge_json and base:
                    show = _run(["show", f"refs/remotes/origin/{branch}:{rel}"], root)
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
                                      cwd=str(root), input=content,
                                      capture_output=True, text=True)
                if blob.returncode != 0:
                    raise RuntimeError(f"hash-object {rel}: {blob.stderr[:120]}")
                up = subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                                     f"100644,{blob.stdout.strip()},{rel}"],
                                    cwd=str(root), capture_output=True, text=True,
                                    env={**os.environ, **ge})
                if up.returncode != 0:
                    raise RuntimeError(f"update-index {rel}: {up.stderr[:120]}")
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
                      timeout=180)
            fetch_branch(root, branch)
            if remote_tip(root, branch) == sha:
                log(f"pushed {len(files)} data file(s) -> {branch} ({sha[:8]})")
                return True
            last_err = (pr.stderr or "push reported ok but remote did not move")[:200]
        except Exception as e:
            last_err = str(e)[:200]
        time.sleep(3 * (attempt + 1))
    log(f"push of {len(files)} data file(s) to {branch} failed after "
        f"{attempts} attempts: {last_err}")
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


def robust_snapshot_push(root, snapshot_rel, attempts=4, log=print,
                         branch="main"):
    """Push the committed snapshot + slices as DATA ONLY. Returns True on fix.

    Recovery is the plumbing path (see push_files_plumbing): the commit is
    built from the CURRENT remote tip and carries nothing but `data/` files.
    The previous implementation replayed the working tree with
    `git reset --soft origin/main` + `git add`, which re-committed the stale
    workflow blobs from the depth-1 checkout and was rejected by GitHub —
    the failure that left the shelf at 0 exits while the collector reported
    1,419 harvested and 394 gold.
    """
    root = Path(root)
    snap = root / snapshot_rel
    if not snap.is_file():
        log(f"no snapshot at {snapshot_rel} — nothing to push")
        return False
    files = {snapshot_rel: snap.read_text(encoding="utf-8")}
    for shard in sorted((root / "data").glob("snapshot_shard_*.json")):
        rel = shard.relative_to(root).as_posix()
        try:
            files[rel] = shard.read_text(encoding="utf-8")
        except OSError:
            pass
    ok = push_files_plumbing(
        root, branch, files,
        "gold-fleet: collector merged shards -> snapshot [skip-ci]",
        merge_json={snapshot_rel}, log=log, attempts=attempts)
    if ok:
        # keep the local working tree consistent with what we just published
        try:
            _run(["fetch", "origin"], cwd=root)
            _run(["reset", "--mixed", f"origin/{branch}"], cwd=root)
        except Exception:
            pass
    return ok
