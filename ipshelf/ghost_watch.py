"""ghost_watch.py — signed-OUT gold-IP watcher for YOUR OWN unlisted test
videos. Built per user decision 2026-09-11: "watch video as
nobody, through gold IPs, for scale testing."

WHAT THIS IS (the agreed contract):
  A channel-less viewing session: a fresh throwaway browser profile (no
  Google account, no channel identity, never signed in) routed through a
  graded-gold IP from the shelf pool, that loads the given video
  URL and watches it to a randomized depth, exactly like the warm-up
  engine's watch step.

HARD RULES (enforced here, on purpose):
  1. Any links
  2. NEVER touches any account profile (no cloak-profile, no profiles/).
     Uses profiles/ghost-<n>/ throwaway dirs, wiped after the run.
  3. NO engagement of any kind — watch only. No like, no sub, no comment, no search. A viewer that just watches.
  4. IPs come from the shelf pool gold (status gold), re-gated at draw,
     burned on death — same laws as channel sessions. But ghost watchers
     do NOT consume the 5-channel cap: they are ephemeral viewers, not
     channel identities. (Cap math = channels; ghosts never bind.)

Usage (venv python, from yt-channel-automation):
  venv/Scripts/python.exe ipshelf/ghost_watch.py --url <your unlisted link> [--count 3]
      [--depth-lo 55] [--depth-hi 95] [--headful]

Writes ipshelf/data/ghost_status.json + evidence in _hunt/ghost_*.
Exit 0 when every ghost watched; 1 otherwise.
"""
import argparse
import json
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.browser import cloak_exe
from hunt_lib import capture
from wu_lib import ensure_playing, video_state, watch_until

DATA = ROOT / "ipshelf" / "data"
ALLOWED = DATA / "ghost_allowed.json"          # {"allowed": ["<11char-id>", ...]}
GHOST_PROFILE = ROOT / "profiles" / "ghost"     # per-run throwaway dir
T1 = {"US", "GB", "CA", "DE", "FR", "NL", "SE", "CH", "AT", "DK", "NO", "FI",
      "IE", "BE", "LU", "JP", "SG", "AU", "NZ", "KR", "HK", "TW"}


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(status):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / "ghost_status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(DATA / "ghost_status.json")


def extract_video_id(url):
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})",
                  url or "")
    return m.group(1) if m else None


def allowed_ids():
    try:
        return set(json.loads(ALLOWED.read_text(encoding="utf-8")).get("allowed", []))
    except Exception:
        return set()


def draw_ghost_ip(region="T1", gate_now=True):
    """Pick a gold exit for a ghost (no binding, no cap consumption).

    Re-gates at the moment of draw (LAW 6). Ties: highest score first.
    Ghosts may use any gold exit; preference order: requested region tier,
    then anything alive. Returns entry dict or None.
    """
    from ipshelf.core import gate, shelf
    pool = shelf.load_pool()
    golds = [e for e in pool.get("exits", []) if e.get("status") == "gold"]
    # prefer region matches, then T1, then everything alive
    golds.sort(key=lambda e: (
        0 if (region != "T1" and e.get("cc") == region) else
        1 if e.get("cc") in T1 else 2, -(e.get("score") or 0)))
    for e in golds:
        if gate_now:
            g = gate.quick_gold_check(e["addr"], e.get("proto"))
            if not g.get("ok"):
                shelf.append_log("skip", channel="@ghost", addr=e["addr"],
                                 reason="gate_fail")
                continue
        return e
    return None


def watch_one(p, url, depth_pct, headful, idx):
    """One signed-out watch session through one gold IP. Returns result."""
    from ipshelf.core import gate
    entry = draw_ghost_ip()
    if entry is None:
        return {"ok": False, "error": "no gold IP available"}
    proxy_url = gate.proxy_url(entry)
    profile = GHOST_PROFILE.with_name(f"ghost-{idx}")
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.parent.mkdir(parents=True, exist_ok=True)

    res = {"ok": False, "ip": entry["addr"], "cc": entry.get("cc"),
           "proxy": proxy_url, "video": url, "watchPct": round(depth_pct, 3)}
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=str(cloak_exe()),
            headless=not headful,
            args=[f"--proxy-server={proxy_url}",
                  "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(45000)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            if "google.com/sorry" in (page.url or ""):
                res["error"] = "captcha wall on exit IP"
                capture(page, f"ghost_{idx}_captcha", "captcha", entry["addr"])
                return res
            # confirm we are NOT signed in (ghost contract)
            from wu_lib import is_signed_in
            res["signed_in"] = bool(is_signed_in(page))
            ensure_playing(page)
            dur = None
            for _ in range(10):
                st = video_state(page) or {}
                if st.get("duration"):
                    dur = st["duration"]
                    break
                page.wait_for_timeout(2000)
            if not dur:
                res["error"] = "video never exposed duration (private? link?)"
                capture(page, f"ghost_{idx}_dead", "no media", url)
                return res
            target = min(dur * depth_pct, dur - 5)
            r = watch_until(page, target, label=f"ghost_{idx}",
                            max_wall_seconds=int(target * 3))
            res["reached"] = r.get("reached")
            res["duration"] = dur
            res["currentTime"] = r.get("currentTime")
            res["ok"] = bool(r.get("reached"))
            capture(page, f"ghost_{idx}_done", "ghost watch complete",
                    f"ip={entry['addr']} pct={depth_pct:.0%}")
            return res
        finally:
            ctx.close()
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        return res
    finally:
        shutil.rmtree(profile, ignore_errors=True)  # throwaway, always wiped


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Signed-out gold-IP watch of any test video.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--count", type=int, default=3,
                    help="how many ghost sessions (default 3)")
    ap.add_argument("--depth-lo", type=int, default=55)
    ap.add_argument("--depth-hi", type=int, default=95)
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args(argv)

    vid = extract_video_id(args.url)
    if not vid:
        raise SystemExit("not a YouTube video link")
    allowed = allowed_ids()
    if vid not in allowed:
        raise SystemExit(
            f"REFUSED: video {vid} is not in {ALLOWED}\n"
            f"Recheck the Link.\n"
            f"Add its id like: {{\"allowed\": [\"{vid}\"]}}")

    status = {"running": True, "started_at": now_ts(), "url": args.url,
              "video_id": vid, "count": args.count, "results": []}
    write_status(status)
    from ipshelf.core import shelf
    shelf.append_log("ghost_run_start", url=args.url, count=args.count)

    ok = 0
    with sync_playwright() as p:
        for i in range(args.count):
            pct = random.uniform(args.depth_lo, args.depth_hi) / 100.0
            print(f"[{i+1}/{args.count}] ghost session (depth {pct:.0%}) ...")
            r = watch_one(p, args.url, pct, args.headful, i)
            status["results"].append(r)
            status["ok"] = sum(1 for x in status["results"] if x.get("ok"))
            write_status(status)
            print(f"   ip={r.get('ip')} ({r.get('cc')}) ok={r.get('ok')} "
                  f"reached={r.get('reached')} err={r.get('error')}")
            shelf.append_log("ghost_watch", **{k: r.get(k) for k in
                                               ("ip", "cc", "ok", "reached",
                                                "error")})
            if r.get("ok"):
                ok += 1
            if i < args.count - 1:
                pause = random.randint(20, 45)
                print(f"   (pacing {pause}s)")
                time.sleep(pause)

    status["running"] = False
    status["finished_at"] = now_ts()
    status["summary"] = {"ok": ok, "failed": args.count - ok,
                         "total": args.count}
    write_status(status)
    shelf.append_log("ghost_run_done", ok=ok, total=args.count)
    print(f"\nGHOST RUN DONE: {ok}/{args.count} watched through gold IPs")
    return 0 if ok == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
