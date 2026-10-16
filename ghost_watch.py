"""ghost_watch.py — High-performance multi-tab Ghost Watcher for GitHub Actions & Local.

Spawns N parallel guest watch sessions routed through Gold harvested IPs, streaming
live visual frames and playback progress directly to the dashboard.
"""
import argparse
import concurrent.futures
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.logging_setup import get_logger

log = get_logger("ghost_watch")

from engine.cloud_browser import open_ghost_context
from engine.live_streamer import LiveStreamer
from wu_lib import ensure_playing, video_state, watch_with_live_stream

DATA = ROOT / "data"
GHOST_PROFILE_BASE = ROOT / "profiles" / "ghost_temp"

#: runner -> rack feedback, filled in during the run
_RIDING = {}
_BURNED = set()
_FEEDBACK_LOCK = threading.Lock()


def extract_video_id(url):
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def draw_gold_ip(use_proxy=True, max_candidates=12, top_pick=8, region=None):
    """Select a gold exit, spread across tabs, honouring the per-IP cap.

    Source of truth: the hub registry (SQLite) when available — that gives
    cap enforcement, T1-first ordering and central burn. Cloud runners have
    no registry (it is not committed); they carry the snapshot therefore we
    fall back to the JSON shelf loaded from data/ip_pool_snapshot.json.

    Gates the top-scored golds (dead ones are burned, LAW 5), then picks
    RANDOMLY among the survivors so N concurrent tabs spread over several
    exits instead of piling onto #1.
    """
    if not use_proxy:
        return None

    entries = None
    registry = None
    try:
        from hub import db
        db.init()
        t1_only = bool(db.get_policy("t1_only", 0))
        entries = db.fetch_gold(region=region, t1_only=t1_only, limit=max_candidates * 3)
        registry = db
    except Exception:
        entries = None

    if not entries:
        # fallback: JSON shelf (also what cloud runners use)
        try:
            from ipshelf.core import gate, shelf
            if not shelf.load_pool().get("exits"):
                # cloud: bootstrap the committed snapshot if the shelf is empty
                try:
                    import load_pool_snapshot
                    load_pool_snapshot.main()
                except Exception:
                    pass
            pool = shelf.load_pool()
            entries = [e for e in pool.get("exits", []) if e.get("status") == "gold"]
            entries.sort(key=lambda e: -(e.get("score") or 0))
            entries = entries[:max_candidates * 3]
        except Exception:
            return None

    if not entries:
        return None

    try:
        from ipshelf.core import gate
        ok_entries = []
        for e in entries[:max_candidates]:
            g = gate.quick_gold_check(e["addr"], e.get("proto"))
            if g.get("ok"):
                ok_entries.append(e)
            elif registry is not None:
                registry.burn_ip(e["addr"], e.get("proto"), reason="ghost_gate_fail")
            else:
                try:
                    from ipshelf.core.assigner import burn_ip
                    burn_ip(e["addr"], "ghost_gate_fail")
                except Exception:
                    pass
            if len(ok_entries) >= top_pick:
                break
        if ok_entries:
            pick = random.choice(ok_entries[:top_pick])
            if registry is not None:
                try:      # record the tab as riding this exit (cap-aware)
                    registry.register_tabs_riding([pick["addr"]], 1)
                except Exception:
                    pass
            return pick
    except Exception:
        pass
    return None


def run_tab_session(tab_idx, url, depth_lo, depth_hi, stream_target, headful=False, direct=False):
    """Run an isolated ghost watcher tab with live frame broadcasting."""
    streamer = LiveStreamer(tab_id=tab_idx, stream_target_url=stream_target)
    entry = draw_gold_ip(use_proxy=not direct)
    if entry:
        with _FEEDBACK_LOCK:
            _RIDING[entry["addr"]] = _RIDING.get(entry["addr"], 0) + 1
    elif not direct:
        # proxy wanted but none usable -> nothing to ride (not a burn per se)
        pass
    
    proxy_url = None
    if entry:
        from ipshelf.core import gate
        proxy_url = gate.proxy_url(entry)

    profile_dir = GHOST_PROFILE_BASE / f"tab_{tab_idx}_{int(time.time()*1000)}"
    profile_dir.parent.mkdir(parents=True, exist_ok=True)

    res = {
        "tabId": tab_idx,
        "ok": False,
        "ip": entry.get("addr") if entry else "direct",
        "cc": entry.get("cc") if entry else "LOCAL",
        "video": url,
    }

    with sync_playwright() as p:
        ctx = None
        try:
            ctx, page = open_ghost_context(
                p,
                proxy_server=proxy_url,
                user_data_dir=profile_dir,
                headless=not headful,
            )
            
            # Initial status broadcast
            streamer.push_frame(page, {
                "tabId": tab_idx,
                "status": "connecting",
                "ip": res["ip"],
                "cc": res["cc"],
                "url": url,
            }, min_interval=0)

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Check if captcha wall hit
            if "google.com/sorry" in (page.url or ""):
                res["error"] = "Captcha on IP"
                streamer.push_frame(page, {"tabId": tab_idx, "status": "captcha_error"}, min_interval=0)
                return res

            ensure_playing(page)
            dur = 0
            for _ in range(8):
                st = video_state(page) or {}
                if st.get("duration"):
                    dur = st["duration"]
                    break
                page.wait_for_timeout(1500)

            if not dur:
                dur = 120  # fallback target duration

            depth_pct = random.uniform(depth_lo, depth_hi) / 100.0
            target_seconds = min(dur * depth_pct, dur - 3)

            r = watch_with_live_stream(
                page,
                target_seconds=target_seconds,
                streamer=streamer,
                tab_id=tab_idx,
                ip_info=entry,
            )

            res["ok"] = bool(r.get("reached"))
            res["currentTime"] = r.get("currentTime")
            res["duration"] = r.get("duration")
            return res
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            # runner -> rack feedback: this proxy failed for a real browser
            if entry and not direct:
                with _FEEDBACK_LOCK:
                    _BURNED.add(entry["addr"])
                    _RIDING.pop(entry["addr"], None)
            return res
        finally:
            try:
                if hasattr(ctx, 'close'):
                    ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Multi-tab Ghost Watcher with live screen feed.")
    ap.add_argument("--url", required=True, help="YouTube video URL to watch")
    ap.add_argument("--tabs", type=int, default=2, help="Number of concurrent tabs on this runner")
    ap.add_argument("--depth-lo", type=int, default=50, help="Min watch percentage (0-100)")
    ap.add_argument("--depth-hi", type=int, default=90, help="Max watch percentage (0-100)")
    ap.add_argument("--stream-target", help="Dashboard URL to stream live frames to (e.g. http://ip:8766)")
    ap.add_argument("--headful", action="store_true", help="Launch visible browser")
    ap.add_argument("--direct", action="store_true", help="Bypass IP shelf proxies and connect direct")
    args = ap.parse_args()

    log.info("Starting Ghost Watcher: %d concurrent tabs on %s", args.tabs, args.url)

    # Track which IPs each tab rode + which died, so the runner can report back.
    _RIDING.clear()
    _BURNED.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.tabs) as executor:
        futures = [
            executor.submit(
                run_tab_session,
                i + 1,
                args.url,
                args.depth_lo,
                args.depth_hi,
                args.stream_target,
                args.headful,
                args.direct,
            )
            for i in range(args.tabs)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    ok_count = sum(1 for r in results if r.get("ok"))
    log.info("Ghost Run Completed: %d/%d tabs reached watch target.", ok_count, args.tabs)
    print(f"[+] Ghost Run Completed: {ok_count}/{args.tabs} tabs reached watch target.", flush=True)

    # Write run report for CI artifact upload (hands-off info back to dashboard)
    try:
        report = {
            "runner_id": os.environ.get("RUNNER_ID", os.environ.get("MATRIX_RUNNER_ID", "local")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": args.url,
            "tabs_requested": args.tabs,
            "tabs_ok": ok_count,
            "results": results,
            "resources": snapshot_resources(),
            # ── runner -> rack feedback ──
            "ip_usage": {addr: n for addr, n in _RIDING.items()},
            "burned_ips": sorted(_BURNED),
        }
        DATA.mkdir(parents=True, exist_ok=True)
        rid = report["runner_id"]
        report_path = DATA / f"run_report_{rid}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        log.info("Report written: %s", report_path)

        # standalone burn report the collector can consume directly
        (DATA / f"burn_report_{rid}.json").write_text(
            json.dumps({"runner_id": rid, "burns": [
                {"addr": a, "reason": "watch_gate_fail"} for a in sorted(_BURNED)],
                "ip_usage": report["ip_usage"]}, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("Report write failed: %s", e)


def snapshot_resources():
    """Capture RAM/CPU snapshot (Linux /proc, fallback gracefully)."""
    info = {"platform": sys.platform}
    try:
        import os as _os
        info["cpu_count"] = _os.cpu_count()
        # Load average (Linux/macOS)
        try:
            info["loadavg_1m"] = round(_os.getloadavg()[0], 2)
        except Exception:
            pass
        # Memory from /proc/meminfo (Linux)
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        info["mem_total_mb"] = int(line.split()[1]) // 1024
                    elif line.startswith("MemAvailable:"):
                        info["mem_avail_mb"] = int(line.split()[1]) // 1024
        except Exception:
            pass
    except Exception:
        pass
    return info


if __name__ == "__main__":
    main()
