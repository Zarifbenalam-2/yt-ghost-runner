"""verify.py — BROWSER-LEVEL gold verification (the real test).

The curl gate (ipshelf/core/gate.py) only proves YouTube served the watch
page and reported playabilityStatus=OK. That is a proxy-reputation signal,
NOT proof that a real browser can load and PLAY the video through it.

This module answers the harder question: open a signed-out CloakBrowser
through the proxy, load the watch page, and confirm the <video> element
actually advances. Only exits that survive THIS are trustworthy gold.

Usage (on a Linux runner, under xvfb):
  python hub/verify.py --top 5                    # verify 5 gold from the registry
  python hub/verify.py --top 5 --from-snapshot    # verify from the committed snapshot
  python hub/verify.py --addrs 1.2.3.4:8080,...   # verify specific exits

Writes data/verify_report.json and prints a per-exit verdict table.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import config                        # noqa: E402
from hub.logging_setup import get_logger      # noqa: E402

log = get_logger("hub.verify")


def load_candidates(top, from_snapshot=False, addrs=None):
    """Return [{addr, proto, cc, score}] to verify."""
    if addrs:
        return [{"addr": a, "proto": "HTTP" if ":" in a and "socks" not in a.lower() else "SOCKS"}
                for a in addrs.split(",") if a.strip()]

    if from_snapshot or not (config.DATA_DIR / "hub.db").exists():
        try:
            snap = json.loads(Path(config.SNAPSHOT_FILE).read_text(encoding="utf-8"))
            exits = snap.get("exits", [])
        except (OSError, json.JSONDecodeError):
            exits = []
    else:
        from hub import db
        db.init()
        exits = db.fetch_gold(limit=top * 3)
    return exits[:top * 3]


def curl_gate(entry):
    """Stage 1: the existing curl oracle (cheap, no browser)."""
    from ipshelf.core import gate
    try:
        r = gate.quick_gold_check(entry["addr"], entry.get("proto", "HTTP"), timeout=35)
        return bool(r.get("ok")), r.get("playability")
    except Exception as e:
        return False, f"ERR({type(e).__name__})"


def browser_play_test(entry, seconds=12):
    """Stage 2: REAL browser playback through the proxy, signed-out.

    Returns (played: bool, detail: dict). 'played' is True only if the
    <video>.currentTime advanced, i.e. media actually decoded — not just
    that the page loaded.
    """
    from playwright.sync_api import sync_playwright
    from engine.cloud_browser import open_ghost_context
    from ipshelf.core import gate
    from wu_lib import ensure_playing, video_state

    proxy_url = gate.proxy_url(entry)
    detail = {"proxy": proxy_url, "cc": entry.get("cc")}
    t0 = time.time()
    with sync_playwright() as p:
        ctx = None
        try:
            ctx, page = open_ghost_context(p, proxy_server=proxy_url, headless=True)
            page.goto(config.WATCH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if "google.com/sorry" in (page.url or ""):
                detail["blocked"] = "captcha"
                return False, detail
            ensure_playing(page)
            first = None
            for _ in range(seconds):
                st = video_state(page) or {}
                ct = st.get("currentTime") or 0
                if first is None and ct > 0:
                    first = ct
                if first is not None and ct > first + 1.0:
                    detail.update({"played": True, "currentTime": round(ct, 1),
                                   "duration": round(st.get("duration") or 0, 1),
                                   "took_sec": round(time.time() - t0, 1),
                                   "signed_out": True})
                    return True, detail
                page.wait_for_timeout(1000)
            st = video_state(page) or {}
            detail.update({"played": False, "currentTime": round(st.get("currentTime") or 0, 1),
                           "duration": round(st.get("duration") or 0, 1),
                           "took_sec": round(time.time() - t0, 1)})
            return False, detail
        except Exception as e:
            detail["error"] = f"{type(e).__name__}: {str(e)[:140]}"
            return False, detail
        finally:
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5, help="how many exits to verify")
    ap.add_argument("--from-snapshot", action="store_true")
    ap.add_argument("--addrs", help="comma-separated addr list")
    ap.add_argument("--skip-curl", action="store_true", help="go straight to browser test")
    ap.add_argument("--out", default=str(config.DATA_DIR / "verify_report.json"))
    args = ap.parse_args()

    cands = load_candidates(args.top, args.from_snapshot, args.addrs)
    log.info("verifying %d candidates (curl gate + browser playback)", len(cands))

    report = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "results": []}
    curl_pass = browser_pass = 0

    for e in cands:
        row = {"addr": e.get("addr"), "proto": e.get("proto", "HTTP"),
               "cc": e.get("cc"), "score": e.get("score")}
        if not args.skip_curl:
            ok, play = curl_gate(e)
            row["curl_gold"] = ok
            row["curl_playability"] = play
            curl_pass += 1 if ok else 0
            if not ok:
                row["browser_played"] = False
                row["verdict"] = "dead_at_curl"
                report["results"].append(row)
                log.warning("DEAD(curl) %-24s %s", row["addr"], play)
                continue

        played, detail = browser_play_test(e)
        row["browser_played"] = played
        row.update(detail)
        row["verdict"] = "VERIFIED_GOLD" if played else "curl_only_FAILED_BROWSER"
        browser_pass += 1 if played else 0
        report["results"].append(row)
        log.info("%-22s curl=%s browser=%s %s", row["addr"], row.get("curl_gold"),
                 played, detail.get("error") or detail.get("currentTime"))

    report["summary"] = {
        "candidates": len(cands),
        "curl_gold": curl_pass,
        "browser_played": browser_pass,
        "verified_gold": sum(1 for r in report["results"]
                             if r.get("verdict") == "VERIFIED_GOLD"),
        "curl_but_no_browser": sum(1 for r in report["results"]
                                   if r.get("verdict") == "curl_only_FAILED_BROWSER"),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== VERIFY SUMMARY ===")
    print(json.dumps(report["summary"], indent=2))
    for r in report["results"]:
        print(f"  {r['addr']:24s} cc={str(r.get('cc')):3s} curl={str(r.get('curl_gold')):5s} "
              f"browser={str(r.get('browser_played')):5s} {r['verdict']}")
    print(f"report -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())