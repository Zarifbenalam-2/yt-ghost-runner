"""autopilot.py — Autonomous tab-ceiling finder.

Polls GitHub API for the latest workflow run. When the run it triggered
completes, it pushes the next tab count (on success) or records the
ceiling (on failure). Runs until ceiling found or TAB_LEVELS exhausted.

Usage: python autopilot.py [--once] [--interval 60]
  --once: single poll cycle, then exit (for Task Scheduler)
  Without --once: loops forever until done.

State: autopilot_state.json next to this file.
Log: autopilot.log
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOW_FILE = ROOT / ".github" / "workflows" / "cloud_ghost_watch.yml"
STATE_FILE = ROOT / "autopilot_state.json"
LOG_FILE = ROOT / "autopilot.log"
REPO = "Zarifbenalam-2/yt-ghost-runner"
TAB_LEVELS = [8, 10, 12]  # levels to test, in order
POLL_INTERVAL = 60


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_get(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ghost-Autopilot",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def recent_runs(n=10):
    data = api_get(f"https://api.github.com/repos/{REPO}/actions/runs?per_page={n}")
    return data.get("workflow_runs", [])


def latest_run():
    runs = recent_runs(3)
    return runs[0] if runs else None


def is_test_run(run):
    head_msg = ((run.get("head_commit") or {}).get("message", ""))
    return head_msg.startswith("test: push to") or head_msg.startswith("autopilot: tabs=")


def parse_tabs(head_msg, default=5):
    import re
    m = re.search(r"(\d+)\s*tabs", head_msg)
    return int(m.group(1)) if m else default


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"waiting_run_id": None, "tested_tabs": 5, "ceiling": None, "done": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def set_tabs(n):
    text = WORKFLOW_FILE.read_text(encoding="utf-8")
    import re
    new_text, count = re.subn(
        r"--tabs \$\{\{ github\.event\.inputs\.tabs_per_runner \|\| '\d+' \}\}",
        "--tabs ${{ github.event.inputs.tabs_per_runner || '%d' }}" % n,
        text,
    )
    if count == 0:
        raise RuntimeError("tabs pattern not found in workflow file")
    WORKFLOW_FILE.write_text(new_text, encoding="utf-8")


def git_push(msg):
    subprocess.run(["git", "add", str(WORKFLOW_FILE)], cwd=str(ROOT), check=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(ROOT), check=True)
    subprocess.run(["git", "push"], cwd=str(ROOT), check=True,
                   capture_output=True, text=True, timeout=60)


def cycle():
    """One poll-and-act cycle. Returns True if autopilot is done."""
    state = load_state()
    if state.get("done"):
        log("Autopilot already done. Ceiling: %s" % state.get("ceiling"))
        return True

    processed = set(state.get("processed_run_ids", []))
    # Find newest COMPLETED test run we haven't processed yet (scan recent,
    # so docs/config pushes in between never block the hunt)
    target = None
    for run in recent_runs(10):
        if run.get("status") == "completed" and is_test_run(run) \
                and run["id"] not in processed:
            target = run
            break

    if not target:
        latest = latest_run()
        if latest:
            log(f"No unprocessed test run. Latest #{latest.get('run_number')} "
                f"status={latest.get('status')}. Idle.")
        else:
            log("No runs found, waiting...")
        return False

    run_id = target["id"]
    conclusion = target.get("conclusion")
    head_msg = ((target.get("head_commit") or {}).get("message", ""))
    log(f"Processing run #{target.get('run_number')} id={run_id} conclusion={conclusion}")
    tabs_tested = parse_tabs(head_msg, state.get("tested_tabs", 5))

    processed.add(run_id)
    state["processed_run_ids"] = sorted(processed)

    if conclusion == "success":
        log(f"PASS at {tabs_tested} tabs/runner.")
        nxt = [t for t in TAB_LEVELS if t > tabs_tested]
        if not nxt:
            state["ceiling"] = f">={tabs_tested} (all levels passed)"
            state["done"] = True
            save_state(state)
            log(f"DONE. All levels passed. Ceiling >= {tabs_tested}.")
            return True
        next_tabs = nxt[0]
        set_tabs(next_tabs)
        msg = f"autopilot: tabs={next_tabs} (ceiling hunt, prev {tabs_tested} passed)"
        git_push(msg)
        log(f"Pushed {next_tabs} tabs/runner.")
        state["tested_tabs"] = next_tabs
        save_state(state)
        return False
    else:
        state["ceiling"] = f"{tabs_tested} FAILED, last pass below {tabs_tested}"
        state["done"] = True
        save_state(state)
        log(f"DONE. {tabs_tested} tabs failed. Ceiling is below {tabs_tested}.")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL)
    args = ap.parse_args()

    log("Autopilot cycle start.")
    try:
        done = cycle()
    except Exception as e:
        log(f"Cycle error: {type(e).__name__}: {e}")
        done = False

    if args.once or done:
        return
    log(f"Looping every {args.interval}s until ceiling found.")
    while True:
        time.sleep(args.interval)
        try:
            if cycle():
                break
        except Exception as e:
            log(f"Cycle error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
