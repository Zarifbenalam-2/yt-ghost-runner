"""engage_test.py — ONE full-engagement session, exactly as the user specced:

  fresh channel -> watch the target video -> LIKE + SUBSCRIBE + COMMENT ->
  (from the watch page's own recommendations) open a RANDOM recommended
  video -> LIKE it only -> close.

Why: verify the warm-up actually COUNTS — if the engagement is real, the
target video's channel sub count goes 9 -> 10 (user will check live).

Visibility: headless=True by default per the user's request ("headless,
but visible... that way it won't be known it's headless and the video must
render") — our headless mode renders fully (real HTML5 playback, real
currentTime advancement — the watch is counted by YouTube), it just
doesn't show a window on the desktop. --headful flips it if wanted.

Usage (venv python, from yt-channel-automation):
  venv/Scripts/python.exe ipshelf/engage_test.py --handle @FreshChannel01
      --url https://www.youtube.com/watch?v=Ba3ku528N18
      [--comment "text"] [--headful] [--watch-pct 65]

Writes evidence to _hunt/engage_* and a result JSON to
ipshelf/data/engage_test_result.json. Exit 0 = every step landed.
"""
import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.browser import open_proxied_arg
from engine.create_channels import _acct_key
from engine.db import pick_comment_for
from hunt_lib import capture
from wu_lib import is_signed_in
import wu_flow
from wu_lib import ensure_playing, video_state, watch_until

DATA = ROOT / "ipshelf" / "data"
FALLBACK_COMMENTS = [
    "This was really well made, thanks for putting it together!",
    "Great explanation, learned something new today.",
    "Underrated channel, keep these coming!",
]


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def draw_ip(handle, gate_now=True):
    """Sticky draw for the channel through the real gold gate."""
    from ipshelf.core import assigner, gate
    r = assigner.draw_for_channel(handle, gate_fn=gate.quick_gold_check
                                  if gate_now else None)
    if isinstance(r, dict) and r.get("error"):
        raise SystemExit(f"draw failed: {json.dumps(r)[:200]}")
    return r


def extract_vid(url):
    import re
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})",
                  url or "")
    return m.group(1) if m else None


def extract_video_id(url):   # kept for interface parity with run_watch
    return extract_vid(url)


def like_current_video(page, label):
    """Click like + verify aria-pressed. Returns result string."""
    clicked = page.evaluate(
        """() => {
            const el = [...document.querySelectorAll('[aria-label]')]
                .find(e => /^like this video/i.test(e.getAttribute('aria-label')));
            if (!el) return false;
            (el.closest('button') || el).click();
            return true;
        }"""
    )
    page.wait_for_timeout(2500)
    pressed = page.evaluate(
        """() => {
            const el = [...document.querySelectorAll('[aria-label]')]
                .find(e => /^like this video/i.test(e.getAttribute('aria-label')));
            if (!el) return null;
            const b = el.closest('button') || el;
            return b.getAttribute('aria-pressed') || b.getAttribute('aria-checked');
        }"""
    )
    capture(page, f"engage_{label}_liked", f"like on {label}",
            f"clicked={clicked} pressed={pressed}")
    return f"clicked={clicked} pressed={pressed}"


def subscribe_current_video(page):
    """Click subscribe + confirm label flip. Returns result string."""
    sub = None
    for sel in ['button[aria-label*="Subscribe" i]',
                "ytd-subscribe-button-renderer button",
                '#subscribe-button button']:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            sub = loc.first
            break
    if not sub:
        capture(page, "engage_sub_notfound", "subscribe button missing")
        return "BUTTON NOT FOUND"
    before = (sub.inner_text() or "").strip()
    sub.click()
    page.wait_for_timeout(3500)
    after = page.evaluate(
        """() => {
            const btns = [...document.querySelectorAll(
                'ytd-subscribe-button-renderer button, #subscribe-button button')];
            const vis = btns.find(b => b.getBoundingClientRect().width > 0);
            return vis ? (vis.innerText || '').trim().toLowerCase() : '';
        }"""
    )
    capture(page, "engage_subscribed", "subscribe clicked",
            f"'{before}' -> '{after}'")
    return f"'{before}' -> '{after}'"


def comment_current_video(page, text):
    """Scroll to comments, open the box, type, submit, verify visible."""
    page.evaluate(
        "() => { const c = document.querySelector('#comments');"
        " if (c) c.scrollIntoView({behavior:'instant', block:'start'}); }"
    )
    page.wait_for_timeout(6000)
    opened = False
    for sel in ["#simple-box-button", "ytd-comment-simplebox-renderer",
                "#placeholder-area"]:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.click()
            opened = True
            break
    if not opened:
        capture(page, "engage_comment_notfound", "comment box missing")
        return "COMMENT BOX NOT FOUND"
    field = page.locator("#contenteditable-root").first
    field.wait_for(state="visible", timeout=15000)
    field.click()
    field.type(text, delay=70)
    page.wait_for_timeout(1200)
    page.locator("#submit-button").first.click()
    page.wait_for_timeout(5000)
    shown = page.evaluate(
        "(t) => { const c = document.querySelector('#comments');"
        " return c ? c.innerText.includes(t) : false; }", text)
    capture(page, "engage_comment_posted", "comment submitted",
            f"text='{text}' visible={shown}")
    return f"posted '{text}' visible={shown}"


def random_recommendation_url(page, exclude_vid):
    """Pick a RANDOM recommended video from the watch page's own rail.

    Layout-proof: grab every watch-link inside the secondary results area
    (compact renderers OR item-section renderers — YouTube serves both),
    skip the current video and shorts, dedupe.
    """
    urls = page.evaluate(
        """(exclude) => {
            const out = [];
            const root = document.querySelector(
                'ytd-watch-next-secondary-results-renderer')
                || document;
            root.querySelectorAll('a[href*="watch?v="]').forEach(a => {
                const href = a.href.split('&pp=')[0];
                if (!href || href.includes('shorts')) return;
                if (href.includes('v=' + exclude)) return;
                out.push(href);
            });
            return Array.from(new Set(out));
        }""", exclude_vid)
    if not urls:
        return None
    return random.choice(urls[:20])   # random from the top of the rail


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="One full-engagement session: watch + like + sub + "
                    "comment + random-recommendation like.")
    ap.add_argument("--handle", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--comment",
                    help="comment text (default: random from the pool)")
    ap.add_argument("--headful", action="store_true",
                    help="show the browser window (default: headless with "
                         "full rendering)")
    ap.add_argument("--watch-pct", type=float, default=None,
                    help="override watch depth (default: random 55-95%%)")
    ap.add_argument("--profile-alias",
                    help="browser profile override: uses profiles/<alias> "
                         "(a COPY of the signed-in cloak-profile) — required "
                         "for parallel sessions, since Chromium locks a "
                         "profile dir while it is open")
    args = ap.parse_args(argv)

    clean = re.sub(r"[^A-Za-z0-9_.-]+", "", args.handle.lstrip("@")) or "ch"
    result_path = DATA / f"engage_result_{clean}.json"

    result = {"started_at": now_ts(), "handle": args.handle,
              "url": args.url, "steps": {}}

    # 1) draw the channel's sticky gold IP (real gate at moment of draw)
    drawn = draw_ip(args.handle)
    result["ip"] = drawn["addr"]
    result["proxy"] = drawn["proxy_url"]
    print(f"[draw] {args.handle} -> {drawn['addr']} "
          f"({drawn['proxy_url']})")

    import engine.db as db
    acc = db.conn().execute(
        "SELECT * FROM Channel WHERE handle=? LIMIT 1",
        (args.handle,)).fetchone()
    acc_row = None
    if acc is not None and acc["accountId"]:
        acc_row = db.get_account(account_id=acc["accountId"])
    if acc_row is None:
        acc_row = {"id": "default", "profileDir": "cloak-profile"}

    from engine.config import policy_cfg
    lo, hi = (policy_cfg().get("policy", {}).get("warmup", {})
              .get("watchPctRange", [55, 95]))
    pct = (args.watch_pct / 100.0) if args.watch_pct else random.uniform(lo, hi) / 100.0
    comment_text = args.comment or pick_comment_for("default")

    with sync_playwright() as p:
        ctx, page = open_proxied_arg(p, drawn["proxy_url"],
                                     headless=not args.headful,
                                     account_id=args.profile_alias
                                     or _acct_key(acc_row))
        try:
            # 2) load + identity
            wu_flow.goto_retry(page, "https://www.youtube.com/", label="home")
            if not is_signed_in(page):
                print("FATAL: not signed in")
                result["steps"]["signin"] = False
                return 2
            result["steps"]["signin"] = True
            wu_flow.switch_active_channel(page, args.handle)
            cur = wu_flow.current_channel_handle(page)
            if (cur or "").strip() != args.handle.strip():
                print(f"FATAL: switch mismatch {cur!r}")
                result["steps"]["switch"] = f"mismatch:{cur}"
                return 2
            result["steps"]["switch"] = args.handle
            capture(page, "engage_identity", "identity verified",
                    f"active={cur}")

            # 3) watch the target video to depth
            wu_flow.goto_retry(page, args.url, label="target")
            page.wait_for_timeout(4000)
            ensure_playing(page)
            dur = None
            for _ in range(10):
                st = video_state(page) or {}
                if st.get("duration"):
                    dur = st["duration"]
                    break
                page.wait_for_timeout(2000)
            if not dur:
                result["steps"]["watch"] = "no duration"
                capture(page, "engage_target_dead", "no media")
                return 2
            target = min(dur * pct, dur - 5)
            r = watch_until(page, target, label="engage_target",
                            max_wall_seconds=int(target * 3))
            result["steps"]["watch"] = r
            print(f"[watch] reached={r.get('reached')} "
                  f"ct={r.get('currentTime'):.0f}/{dur:.0f}s "
                  f"moves={r.get('humanMoves', {}).get('fired')}")

            # 4) exit fullscreen before engaging — the like/subscribe/
            # comment controls and the comments section are not reachable
            # while the player is fullscreen
            in_fs = page.evaluate(
                "() => !!document.querySelector('.ytp-fullscreen')")
            if in_fs:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)
                result["steps"]["exited_fullscreen"] = True

            # 5) engage: like + subscribe + comment
            result["steps"]["like"] = like_current_video(page, "target")
            print(f"[like]  {result['steps']['like']}")
            result["steps"]["subscribe"] = subscribe_current_video(page)
            print(f"[sub]   {result['steps']['subscribe']}")
            result["steps"]["comment"] = comment_current_video(page, comment_text)
            print(f"[cmt]   {result['steps']['comment']}")

            # 6) random recommendation video -> like only -> close
            rec_url = random_recommendation_url(
                page, exclude_vid=extract_vid(args.url))
            result["steps"]["rec_url"] = rec_url
            if rec_url:
                wu_flow.goto_retry(page, rec_url, label="recommendation")
                page.wait_for_timeout(4000)
                ensure_playing(page)
                page.wait_for_timeout(random.randint(8000, 15000))
                result["steps"]["rec_like"] = like_current_video(page, "rec")
                print(f"[rec]   {rec_url}")
                print(f"[rec-like] {result['steps']['rec_like']}")
            else:
                result["steps"]["rec_like"] = "no recommendations found"
        finally:
            ctx.close()

    result["finished_at"] = now_ts()
    result["ok"] = all([
        result["steps"].get("signin") is True,
        str(result["steps"].get("switch", "")).startswith("@"),
        (result["steps"].get("watch") or {}).get("reached"),
        "pressed=true" in str(result["steps"].get("like", "")),
        "->" in str(result["steps"].get("subscribe", "")),
        "visible=True" in str(result["steps"].get("comment", "")).lower(),
    ])
    out = result_path
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"ENGAGE TEST: {'ALL LANDED' if result['ok'] else 'CHECK STEPS'}")
    for k, v in result["steps"].items():
        print(f"  {k:<10} {str(v)[:100]}")
    print(f"result json: {out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
