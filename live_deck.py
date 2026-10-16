"""live_deck.py — mid-run live state, pushed by runners to the `live-deck` branch.

Why this exists: the GitHub API cannot serve a RUNNING job's logs
(BlobNotFound until the job ends — verified again on watch #59), so pulling
observation data mid-run is impossible. Push model instead: each watcher VM
commits its own data/live/live_r<id>.json (tab states, IPs, ok counts) to the
`live-deck` branch every LIVE_DECK_SEC seconds. The PC side then reads the
fleet WHILE the 500-tab run is in flight:

    python live_deck.py           # fetch + human-readable fleet summary
    python live_deck.py --json    # raw merged JSON

Every runner owns exactly one file path, so sibling pushes never conflict;
the only contention is the ref itself, handled by fetch -> rebuild -> push
retries. Plumbing commands + a private GIT_INDEX_FILE keep the checkout's
working tree and index untouched while tabs are running (the executor also
runs in this process; a corrupt checkout would kill the run).
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

try:
    from hub.logging_setup import get_logger
    log = get_logger("live_deck")
except Exception:                       # PC-side CLI use without hub deps
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("live_deck")

ROOT = Path(__file__).resolve().parent
BRANCH = "live-deck"
REL_PREFIX = "data/live"

_LOCK = threading.Lock()
_STATE = {}                             # tabId -> entry dict
_META = {}                              # run-level fields (url, tabs, ...)
_DIRTY = {"flag": False}
_STOP = threading.Event()
_THREAD = {"obj": None}
_PUSHED = {"count": 0, "last_ok": None, "last_err": None}


def enabled():
    return os.environ.get("LIVE_DECK", "") == "1"


def _runner_id():
    return (os.environ.get("RUNNER_ID")
            or os.environ.get("MATRIX_RUNNER_INDEX") or "local")


def mark(tab_id, status, **fields):
    """Record a tab state change. Cheap, thread-safe, never blocks."""
    with _LOCK:
        e = _STATE.setdefault(int(tab_id), {"tabId": int(tab_id)})
        if e.get("status") != status:
            e["since"] = time.strftime("%H:%M:%S", time.gmtime())
        e["status"] = status
        e["updated"] = time.strftime("%H:%M:%S", time.gmtime())
        e.update(fields)
        if enabled():
            _DIRTY["flag"] = True


def set_meta(**fields):
    with _LOCK:
        _META.update(fields)
        if enabled():
            _DIRTY["flag"] = True


def start():
    """Start the background push thread (no-op unless LIVE_DECK=1)."""
    if not enabled() or _THREAD["obj"] is not None:
        return
    _STOP.clear()
    t = threading.Thread(target=_thread_body, name="live-deck-push", daemon=True)
    _THREAD["obj"] = t
    t.start()
    log.info("live deck ON: pushing %s/live_r%s.json to branch '%s' every %ss",
             REL_PREFIX, _runner_id(), BRANCH,
             os.environ.get("LIVE_DECK_SEC", "120"))


def flush_final(timeout=25):
    """Synchronous final push (called before os._exit in the deadline path)."""
    if not enabled():
        return
    _STOP.set()
    t = _THREAD["obj"]
    if t and t.is_alive():
        t.join(timeout=5)
    try:
        if _push_once():
            log.info("live deck final push ok")
    except Exception as e:
        log.warning("live deck final push failed: %s", e)


# ------------------------------------------------------------------ git
def _git(args, timeout=60, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True,
                          text=True, timeout=timeout, env=env)


def _identity_env():
    return {
        "GIT_AUTHOR_NAME": "ghost-live",
        "GIT_AUTHOR_EMAIL": "ghost-live@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "ghost-live",
        "GIT_COMMITTER_EMAIL": "ghost-live@users.noreply.github.com",
    }


def _push_once():
    """Build a commit containing ONLY our live file on top of the branch and
    push it. Retry on ref races (siblings pushing concurrently)."""
    rid = _runner_id()
    rel = f"{REL_PREFIX}/live_r{rid}.json"
    with _LOCK:
        payload = {
            "runner_id": rid,
            "meta": dict(_META),
            "tabs": [dict(_STATE[k]) for k in sorted(_STATE)],
            "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _DIRTY["flag"] = False
    local = ROOT / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    ident = _identity_env()
    idx = str(ROOT / ".git" / f"live_index_{rid}")
    last_err = None
    for attempt in range(4):
        try:
            _git(["fetch", "origin", BRANCH], timeout=90)
            base = None
            r = _git(["rev-parse", "--verify", "--quiet",
                      f"origin/{BRANCH}"])
            if r.returncode == 0 and r.stdout.strip():
                base = r.stdout.strip()
            ge = dict(ident)
            ge["GIT_INDEX_FILE"] = idx
            if base:
                _git(["read-tree", base], env_extra=ge)
            else:
                _git(["read-tree", "--empty"], env_extra=ge)
            blob = _git(["hash-object", "-w", str(local)])
            if blob.returncode != 0:
                raise RuntimeError(f"hash-object: {blob.stderr[:120]}")
            up = _git(["update-index", "--add", "--cacheinfo",
                       f"100644,{blob.stdout.strip()},{rel}"], env_extra=ge)
            if up.returncode != 0:
                raise RuntimeError(f"update-index: {up.stderr[:120]}")
            tree = _git(["write-tree"], env_extra=ge)
            if tree.returncode != 0:
                raise RuntimeError(f"write-tree: {tree.stderr[:120]}")
            cargs = ["commit-tree", tree.stdout.strip(), "-m",
                     f"live-deck: runner {rid} ({payload['pushed_at']}) [skip-ci]"]
            if base:
                cargs += ["-p", base]
            commit = _git(cargs, env_extra=ge)
            if commit.returncode != 0:
                raise RuntimeError(f"commit-tree: {commit.stderr[:120]}")
            pr = _git(["push", "origin",
                       f"{commit.stdout.strip()}:refs/heads/{BRANCH}"],
                      timeout=120)
            if pr.returncode == 0:
                _PUSHED["count"] += 1
                _PUSHED["last_ok"] = payload["pushed_at"]
                return True
            last_err = (pr.stderr or "")[:200]      # ref moved -> refetch+retry
        except Exception as e:
            last_err = str(e)[:200]
        time.sleep(2 * (attempt + 1))
    _PUSHED["last_err"] = last_err
    log.warning("live deck push failed after retries: %s", last_err)
    return False


def _thread_body():
    interval = int(os.environ.get("LIVE_DECK_SEC", "120"))
    heartbeat = int(os.environ.get("LIVE_DECK_HEARTBEAT", "300"))
    last_push = 0.0
    while not _STOP.wait(interval):
        # A 12-minute watch changes no tab state, so a dirty-only push left
        # the deck silent for minutes at a time (#65: 14 min with the whole
        # fleet watching). Heartbeat at least every LIVE_DECK_HEARTBEAT so
        # "watching since ..." is always fresh and a failed push is retried.
        due = _DIRTY["flag"] or (time.time() - last_push) >= heartbeat
        if not due or not _STATE:
            continue
        try:
            if _push_once():
                last_push = time.time()
        except Exception as e:
            log.warning("live deck push error: %s", e)


# ------------------------------------------------------------- read side
def _show(path):
    r = subprocess.run(["git", "show", f"origin/{BRANCH}:{path}"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def read_deck(fetch=True):
    """PC side: fetch the live-deck branch and return {runner_id: payload}."""
    if fetch:
        subprocess.run(["git", "fetch", "origin", BRANCH], cwd=str(ROOT),
                       capture_output=True, timeout=90)
    deck = {}
    ls = subprocess.run(
        ["git", "ls-tree", "--name-only", f"origin/{BRANCH}:{REL_PREFIX}/"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    for line in (ls.stdout or "").split():
        if line.startswith("live_r") and line.endswith(".json"):
            data = _show(f"{REL_PREFIX}/{line}")
            if data:
                deck[data.get("runner_id", line)] = data
    return deck


def summarize(deck):
    lines = []
    tot_ok = tot_done = tot_tabs = 0
    for rid in sorted(deck, key=lambda x: int(x) if str(x).isdigit() else 999):
        d = deck[rid]
        tabs = d.get("tabs", [])
        ok = sum(1 for t in tabs if t.get("status") == "ok")
        done = sum(1 for t in tabs if t.get("status") in ("ok", "fail", "deadline"))
        watch = sum(1 for t in tabs if t.get("status") == "watching")
        parked = sum(1 for t in tabs if t.get("status") == "parked")
        since = min((t.get("since") for t in tabs
                     if t.get("status") in ("watching", "parked") and t.get("since")),
                    default="?")
        tot_ok += ok
        tot_done += done
        tot_tabs += len(tabs)
        lines.append(
            f"  r{rid:>2}: {ok:>2} ok / {done:>2} done of {len(tabs):>2}"
            f"  ({watch} watching, {parked} parked, oldest since {since})"
            f"  pushed {d.get('pushed_at', '?')}")
    lines.append(f"FLEET: {tot_ok} ok / {tot_done} done of {tot_tabs} tabs "
                 f"across {len(deck)} runners")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Read the live-deck branch")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    deck = read_deck(fetch=not a.no_fetch)
    if a.json:
        print(json.dumps(deck, indent=2))
    else:
        print(summarize(deck) if deck else "live-deck branch empty/missing")
