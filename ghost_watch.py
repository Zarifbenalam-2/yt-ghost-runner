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

import live_deck
from engine.cloud_browser import open_ghost_context
from engine.live_streamer import LiveStreamer
from wu_lib import ensure_playing, video_state, watch_with_live_stream

DATA = ROOT / "data"
GHOST_PROFILE_BASE = ROOT / "profiles" / "ghost_temp"

#: runner -> rack feedback, filled in during the run
_RIDING = {}
_BURNED = set()
_USED = {}          # addr -> tab count on THIS runner (cap enforcement)
_DEAD = set()       # addrs that failed a live gate on THIS runner
_FEEDBACK_LOCK = threading.Lock()


def extract_video_id(url):
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def load_candidates(region=None, limit=400):
    """Raw candidate exits for this runner.

    Source priority:
      1. local hub registry (SQLite) — cap-aware, central burn
      2. THIS runner's sharded snapshot slice (data/snapshot_shard_<i>_of_<n>.json)
         — the global cap fix: the pool is partitioned one IP per runner
         (reach=1), so policy cap_per_ip tabs on an address is a true GLOBAL
         cap rather than cap x tabs_per_runner
      3. the whole committed snapshot (fallback)
    """
    entries = None
    try:
        from hub import db
        db.init()
        t1_only = bool(db.get_policy("t1_only", 0))
        entries = db.fetch_gold(region=region, t1_only=t1_only, limit=limit)
    except Exception:
        entries = None

    if not entries:
        try:
            from hub import allocator
            idx = int(os.environ.get("MATRIX_RUNNER_INDEX",
                                     os.environ.get("RUNNER_ID", "1"))) - 1
            n = int(os.environ.get("TOTAL_RUNNERS", "1"))
            entries = allocator.load_my_slice(max(0, idx), max(1, n))
        except Exception:
            entries = None
    if not entries:
        try:
            import load_pool_snapshot
            entries = load_pool_snapshot.load_snapshot_exits()
        except Exception:
            entries = []
    return entries or []


def validate_candidates(entries, workers=32, limit=None):
    """Gate a slice in PARALLEL once; return the live exits.

    Why not gate per tab: every tab used to walk the same few top-scored
    candidates sequentially, so 25 tabs re-tested the same corpses and gave
    up on a rack that was stale but had live addresses further down. One
    parallel wave validates the whole slice (~100+ exits in a couple of
    waves) and every tab then draws from the survivors.
    """
    from ipshelf.core import gate
    cands = [e for e in entries if e.get("addr")][: limit or len(entries)]
    if not cands:
        return []
    live = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(gate.quick_gold_check, e["addr"], e.get("proto", "HTTP")): e
                for e in cands}
        for f in concurrent.futures.as_completed(futs):
            e = futs[f]
            try:
                r = f.result()
            except Exception:
                r = {}
            if r.get("ok"):
                live.append(e)
            else:
                with _FEEDBACK_LOCK:
                    _DEAD.add(e["addr"])
    live.sort(key=lambda e: -(e.get("score") or 0))
    log.info("validated %d/%d candidates live on this runner", len(live), len(cands))
    return live


def draw_gold_ip(use_proxy=True, max_candidates=12, top_pick=8, region=None,
                 used=None, pool=None):
    """Select a live exit for one tab, honouring the per-IP cap.

    `pool` (from validate_candidates) is already gated: pick from it with no
    further network check. Without a pool, fall back to the old per-call
    sequential gate. `used` is this runner's {addr: count}; an IP at cap is
    skipped and the least-used survivors are preferred.
    """
    if not use_proxy:
        return None
    used = used if used is not None else {}
    cap = 5
    try:
        from hub import db
        cap = int(db.get_policy("cap_per_ip", cap))
    except Exception:
        pass

    # ---- fast path: pre-validated pool ------------------------------------
    if pool:
        avail = [e for e in pool
                 if used.get(e["addr"], 0) < cap and e["addr"] not in _DEAD]
        if avail:
            avail.sort(key=lambda e: used.get(e["addr"], 0))
            pick = avail[0]
            used[pick["addr"]] = used.get(pick["addr"], 0) + 1
            try:
                from hub import db
                db.register_tabs_riding([pick["addr"]], 1)
            except Exception:
                pass
            return pick
        return None

    entries = load_candidates(region=region, limit=max_candidates * 3)
    if not entries:
        return None
    entries = sorted(entries, key=lambda e: -(e.get("score") or 0))

    try:
        from ipshelf.core import gate
        ok_entries = []
        checked = 0
        for e in entries:
            if len(ok_entries) >= top_pick or checked >= max_candidates:
                break
            if e["addr"] in _DEAD:
                continue
            if used.get(e["addr"], 0) >= cap:
                continue                    # this runner is at cap for this IP
            checked += 1
            g = gate.quick_gold_check(e["addr"], e.get("proto"))
            if g.get("ok"):
                ok_entries.append(e)
            else:
                with _FEEDBACK_LOCK:
                    _DEAD.add(e["addr"])
                _burn(e)
        if ok_entries:
            # prefer the least-used survivors so the cap spreads evenly
            ok_entries.sort(key=lambda e: used.get(e["addr"], 0))
            pick = random.choice(ok_entries[:max(1, top_pick // 2)])
            used[pick["addr"]] = used.get(pick["addr"], 0) + 1
            _register(pick)
            return pick
    except Exception:
        pass
    return None


def _burn(entry):
    """Report a dead exit to the rack (registry locally, else the shelf)."""
    try:
        from hub import db
        db.burn_ip(entry["addr"], entry.get("proto"), reason="ghost_gate_fail")
        return
    except Exception:
        pass
    try:
        from ipshelf.core.assigner import burn_ip
        burn_ip(entry["addr"], "ghost_gate_fail")
    except Exception:
        pass


def _register(entry):
    try:
        from hub import db
        db.register_tabs_riding([entry["addr"]], 1)
    except Exception:
        pass


def run_tab_session(tab_idx, url, depth_lo, depth_hi, stream_target, headful=False, direct=False, used=None, pool=None):
    """Run an isolated ghost watcher tab; re-draw a fresh IP on failure.

    The dead-IP fallback: free proxies die between validation and use, so a
    drawn IP can fail page load mid-run. Instead of wasting the tab, this
    retries up to TAB_RETRIES times, each time drawing a DIFFERENT live IP
    (dead ones are marked and skipped, and released from the runner's per-IP
    counter). Only after exhausting retries does the tab report its failure.
    """
    attempts = int(os.environ.get("TAB_RETRIES", "3")) if not direct else 1
    live_deck.mark(tab_idx, "starting", attempts=attempts)
    last_res = None
    for attempt in range(attempts):
        last_res = _tab_attempt(tab_idx, url, depth_lo, depth_hi, stream_target,
                                headful, direct, used, pool)
        if last_res.get("ok"):
            live_deck.mark(tab_idx, "ok", ip=last_res.get("ip"),
                           cc=last_res.get("cc"),
                           currentTime=last_res.get("currentTime"))
            return last_res
        if attempt + 1 < attempts:
            log.info("tab %d attempt %d/%d failed (%s) — re-drawing IP",
                     tab_idx, attempt + 1, attempts, last_res.get("error")) 
    live_deck.mark(tab_idx, "fail", ip=last_res.get("ip"),
                   error=last_res.get("error"))
    return last_res


def _tab_attempt(tab_idx, url, depth_lo, depth_hi, stream_target, headful, direct, used, pool):
    """One attempt of a tab session on one freshly drawn IP."""
    streamer = LiveStreamer(tab_id=tab_idx, stream_target_url=stream_target)
    entry = draw_gold_ip(use_proxy=not direct, used=used, pool=pool)
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
    live_deck.mark(tab_idx, "watching", ip=res["ip"], cc=res["cc"])

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
                    _DEAD.add(entry["addr"])      # re-draw skips it
                    _RIDING.pop(entry["addr"], None)
            return res
        finally:
            try:
                if hasattr(ctx, 'close'):
                    ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)


def _pool_refresher(pool, stop_evt):
    """Water-pile tail: fold gold-stream gold into the LIVE pool mid-run.

    The start-of-run fold-in only sees gold harvested BEFORE the run. Harvest
    shards keep pushing to gold-stream cycle-by-cycle DURING the run; this
    refresher picks those up every REFRESH_SEC and appends only genuinely NEW
    addresses (validated first), so tabs keep drinking minutes-old IP instead
    of draining the start-of-run pile. New entries are appended (draw_gold_ip
    reads the list under the GIL and prefers least-used IPs, so fresh live
    exits win the next draw). Any failure just leaves the current pool —
    never kills the run.
    """
    import subprocess as sp
    interval = int(os.environ.get("REFRESH_SEC", "600"))
    known = {e["addr"] for e in pool}
    while not stop_evt.wait(interval):
        try:
            sp.run(["git", "fetch", "origin", "gold-stream"], cwd=str(ROOT),
                   capture_output=True, timeout=90)
            if sp.run(["git", "checkout", "origin/gold-stream", "--", "data/stream"],
                      cwd=str(ROOT), capture_output=True, timeout=60).returncode != 0:
                continue
            merged = ROOT / "data" / "pool_refresh.json"
            sp.run([sys.executable, "load_pool_snapshot.py", "--with-stream",
                    "--out", str(merged)], cwd=str(ROOT), capture_output=True,
                   timeout=120)
            if not merged.is_file():
                continue
            entries = json.loads(merged.read_text(encoding="utf-8")).get("exits", [])
            with _FEEDBACK_LOCK:
                fresh = [e for e in entries
                         if e.get("addr") and e["addr"] not in known
                         and e["addr"] not in _DEAD]
            if not fresh:
                continue
            live = validate_candidates(
                fresh, workers=int(os.environ.get("GATE_WORKERS", "32")))
            added = 0
            for e in live:
                if e["addr"] not in known:
                    pool.append(e)
                    known.add(e["addr"])
                    added += 1
            if added:
                log.info("pool refresh: +%d live of %d new stream candidates "
                         "(pool now %d)", added, len(fresh), len(pool))
        except Exception as e:
            log.warning("pool refresh failed (continuing on current pool): %s", e)


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
    _USED.clear()
    _DEAD.clear()

    # ── validate this runner's slice ONCE, in parallel ──
    # 25 tabs each gating the top of the same list re-tested the same corpses
    # and gave up on a stale-but-usable rack. One wave finds every live exit
    # in the slice; the tabs then just draw from the survivors.
    pool = []
    pool_stats = {"candidates": 0, "live": 0}
    if not args.direct:
        cands = load_candidates(limit=int(os.environ.get("POOL_SCAN", "160")))
        pool_stats["candidates"] = len(cands)
        if cands:
            pool = validate_candidates(
                cands, workers=int(os.environ.get("GATE_WORKERS", "32")))
            pool_stats["live"] = len(pool)
            log.info("IP pool ready: %d live of %d candidates", len(pool), len(cands))

    live_deck.set_meta(url=args.url, tabs=args.tabs,
                       pool_live=pool_stats.get("live"),
                       pool_candidates=pool_stats.get("candidates"))
    live_deck.start()

    # water-pile tail: keep folding gold-stream additions into the live pool
    refresh_stop = threading.Event()
    if not args.direct:
        threading.Thread(target=_pool_refresher, args=(pool, refresh_stop),
                         name="pool-refresh", daemon=True).start()

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=args.tabs, thread_name_prefix="tab")
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
            _USED,
            pool,
        )
        for i in range(args.tabs)
    ]
    # Wall-clock deadline: a single stuck tab must NEVER hold the whole
    # runner for hours (watch #52: 15 of 20 jobs hung ~90min with zero logs).
    # We do NOT use `with ThreadPoolExecutor(...)` here — its __exit__ calls
    # shutdown(wait=True), which blocks until every hung tab thread finishes,
    # so the deadline would still never fire. shutdown(wait=False) + a hard
    # os._exit after the report is written is what actually ends the run.
    deadline = int(os.environ.get("RUN_MAX_SEC", "3300"))
    deadline_hit = False
    finished = {}
    try:
        for f in concurrent.futures.as_completed(futures, timeout=deadline):
            finished[f.result()["tabId"]] = f.result()
    except concurrent.futures.TimeoutError:
        deadline_hit = True
        log.warning("run hit %ss deadline — terminating remaining tabs", deadline)
        for f in futures:
            f.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    results = [finished.get(i + 1, {
        "tabId": i + 1, "ok": False, "ip": "direct", "cc": "LOCAL",
        "video": args.url, "error": "run_deadline"})
        for i in range(args.tabs)]
    log.info("run deadline enforced: %d tabs finished, %d cut at %ss",
             len(finished), args.tabs - len(finished), deadline)
    run_deadline_hit = deadline_hit

    # final live-deck sync so the deck shows true end-of-run state
    refresh_stop.set()
    for r in results:
        st = "ok" if r.get("ok") else (
            "deadline" if r.get("error") == "run_deadline" else "fail")
        live_deck.mark(r["tabId"], st, ip=r.get("ip"), error=r.get("error"))

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
            "pool": pool_stats,
            "dead_in_slice": sorted(_DEAD),
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

    # force-exit on deadline so hung tab threads cannot outlive the run
    # (see the executor note above: shutdown(wait=False) is not enough —
    # non-daemon worker threads keep the process alive until they die)
    live_deck.flush_final()      # last deck push must beat the exit below
    if run_deadline_hit:
        log.warning("deadline hit — os._exit(0) to shed hung tab threads")
        os._exit(0)


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
