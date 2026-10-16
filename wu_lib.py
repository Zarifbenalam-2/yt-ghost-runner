"""Warm-up browser helpers: launch Cloak signed-in profile THROUGH a chosen proxy,
plus realistic HTML5-video playback helpers (read duration, watch to a target).

Merged from yt-channel-automation wu_lib.py (CaptchaWall, exit_ip_country,
is_signed_in, watch_until) and yt-ghost-runner wu_lib.py (watch_with_live_stream,
get_video_title).
"""
import json
import random
import time
from pathlib import Path

from hunt_lib import CLOAK_EXE, PROFILE, capture  # reuse profile + evidence capture

ROOT = Path(__file__).resolve().parent
PROXIES_US = ROOT / "proxies_us.json"


import os as _os

_DEMO_MOVES = _os.environ.get("HUMAN_MOVES_DEMO", "") == "1"
#: on by default so human behavior is exercised (and logged) on every watch;
#: set HUMAN_MOVES=false in the workflow to disable entirely.
_HUMAN_ON = _os.environ.get("HUMAN_MOVES", "true").lower() not in ("0", "false", "no")

from hub.logging_setup import get_logger as _get_logger
_hlog = _get_logger("human_moves")


def _human_moves_cfg():
    """policy.yaml warmup.humanMoves (best-effort; defaults live in the module).

    Demo mode (--demo-moves): every probability becomes 1.0 so each watch
    fires every move type — for the user's live demo/verification runs."""
    if not _HUMAN_ON:
        return {"pauseProbability": 0.0, "seekProbability": 0.0,
                "volumeProbability": 0.0, "fullscreenProbability": 0.0,
                "qualityProbability": 0.0}
    if _DEMO_MOVES:
        return {"pauseProbability": 1.0, "seekProbability": 1.0,
                "volumeProbability": 1.0, "fullscreenProbability": 1.0,
                "qualityProbability": 1.0}
    try:
        from engine.config import policy_cfg
        return (policy_cfg().get("policy", {}).get("warmup", {})
                .get("humanMoves", {})) or {}
    except Exception:
        return {}


def pick_us_proxy(index=0):
    data = json.loads(PROXIES_US.read_text(encoding="utf-8"))
    m = data.get("matching", [])
    if not m:
        raise RuntimeError("No US proxy in proxies_us.json — run wu_proxies.py first")
    return m[min(index, len(m) - 1)]


def open_proxied(p, proxy_url, headless=True):
    """Cloak + logged-in profile, all traffic through proxy_url (socks5://host:port)."""
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        executable_path=str(CLOAK_EXE),
        headless=headless,
        proxy={"server": proxy_url},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page


def open_proxied_arg(p, proxy_server, headless=True):
    """Route via Chromium's own --proxy-server (supports socks4://, socks5://, http://).

    Many free 'socks5' lists are actually socks4; Playwright's proxy option only
    speaks socks5/http, so for those we pass the scheme straight to Chromium.
    """
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        executable_path=str(CLOAK_EXE),
        headless=headless,
        args=[
            f"--proxy-server={proxy_server}",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(45000)
    return ctx, page


def exit_ip_country(page):
    """Confirm the browser's real egress IP + country (proves proxy is in effect)."""
    page.goto("http://ip-api.com/json/?fields=status,countryCode,query,isp",
              wait_until="domcontentloaded", timeout=45000)
    try:
        txt = page.locator("body").inner_text()
        return json.loads(txt)
    except Exception:
        return {}


def is_signed_in(page):
    """Positive proof of login (avatar), not absence of a form."""
    for sel in ["img#avatar", "#avatar-btn img", "button#avatar-btn"]:
        if page.locator(sel).count() > 0:
            return True
    return False


class CaptchaWall(Exception):
    """Google redirected us to google.com/sorry — the exit IP is flagged."""


def check_captcha(page):
    """Raise CaptchaWall if we're on Google's anti-abuse interstitial."""
    url = page.url or ""
    if "google.com/sorry" in url:
        raise CaptchaWall(f"CAPTCHA wall hit: {url[:120]}")


# ── Video helpers ────────────────────────────────────────────────────────────

def video_state(page):
    """Return {duration, currentTime, paused, readyState, muted} of the main <video>, or None."""
    try:
        return page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (!v) return null;
                return { duration: v.duration, currentTime: v.currentTime,
                         paused: v.paused, readyState: v.readyState, muted: v.muted };
            }"""
        )
    except Exception:
        return None


def get_video_title(page):
    """Extract YouTube video title."""
    try:
        title = page.evaluate("() => document.querySelector('h1.ytd-watch-metadata, #title h1')?.innerText || document.title")
        return str(title).strip()
    except Exception:
        return ""


def ensure_playing(page):
    """Nudge the YouTube player into playback like a user would (click play)."""
    try:
        page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (v) { v.muted = false; v.volume = 0.5; v.play().catch(()=>{}); }
                const b = document.querySelector('.ytp-large-play-button, .ytp-play-button');
                if (b && document.querySelector('video')?.paused) b.click();
            }"""
        )
    except Exception:
        pass


def watch_until(page, target_seconds, label="watch", max_wall_seconds=None, capture_every=60):
    """Watch until video.currentTime >= target_seconds, with human-ish jitter.

    Returns dict with reached/currentTime/duration and humanMoves summary.
    Polls the real <video> element so 'watched' means the media actually
    advanced, not a blind timer. Real-viewer micro-movements (pause/seek/
    volume/fullscreen/quality via the player's own controls) are planned
    once per watch by human_moves.HumanMoves and fired here as their time
    comes — so every watcher (channel sessions AND ghosts) inherits them.
    """
    from human_moves import HumanMoves
    max_wall = max_wall_seconds or (target_seconds * 3 + 120)
    start = time.time()
    last_cap = 0
    last_ct = -1.0
    stalls = 0
    moves = HumanMoves.plan((video_state(page) or {}).get("duration") or 0,
                            target_seconds, _human_moves_cfg())
    _hlog.info("human-move plan: planned=%s (target %.0fs)",
               moves.summary().get("planned"), target_seconds)
    ensure_playing(page)
    while True:
        st = video_state(page)
        now = time.time()
        if st and st.get("currentTime") is not None:
            ct = st["currentTime"] or 0
            dur = st.get("duration") or 0
            if ct <= last_ct + 0.2:  # not advancing -> stalled, nudge
                stalls += 1
                if stalls % 3 == 0:
                    ensure_playing(page)
            else:
                stalls = 0
            last_ct = ct
            moved = moves.tick(page, ct)   # fire due human-move events
            if moved:
                _hlog.info("human move(s) fired at %.0fs: %s", ct, "+".join(moved))
                capture(page, f"{label}_move_{'_'.join(moved)}",
                        "human move", f"at {ct:.0f}s: {'+'.join(moved)}")
            if now - last_cap >= capture_every:
                capture(page, f"{label}_at_{int(ct)}s", f"{label} progress",
                        f"ct={ct:.0f}/{dur:.0f}")
                last_cap = now
            if ct >= target_seconds:
                capture(page, f"{label}_done", f"{label} reached target",
                        f"ct={ct:.0f}/{dur:.0f}")
                _hlog.info("watch done: fired human moves %s", moves.summary().get("fired"))
                return {"reached": True, "currentTime": ct, "duration": dur,
                        "humanMoves": moves.summary()}
        if now - start > max_wall:
            capture(page, f"{label}_timeout", f"{label} wall-clock timeout")
            return {"reached": False, "currentTime": last_ct,
                    "duration": (st or {}).get("duration"),
                    "humanMoves": moves.summary()}
        # human-ish poll cadence
        page.wait_for_timeout(random.randint(1500, 3000))


def watch_with_live_stream(page, target_seconds, streamer=None, tab_id=0, ip_info=None, max_wall_seconds=None):
    """Watch video until currentTime >= target_seconds while pushing real-time screen frames.

    This is the ghost-runner's live-streaming variant of watch_until().
    Human-ish moves (pause/seek/volume/quality via the player's own controls)
    are planned once per watch and fired as their time comes — the same
    behaviour watch_until has; the streaming variant had silently dropped it.
    """
    from human_moves import HumanMoves
    max_wall = max_wall_seconds or (target_seconds * 3 + 120)
    start_time = time.time()
    last_ct = -1.0
    stalls = 0

    moves = HumanMoves.plan((video_state(page) or {}).get("duration") or 0,
                            target_seconds, _human_moves_cfg())
    _hlog.info("tab %s human-move plan: planned=%s (target %.0fs)",
               tab_id, moves.summary().get("planned"), target_seconds)

    ensure_playing(page)

    while True:
        st = video_state(page)
        now = time.time()
        title = get_video_title(page)

        if st and st.get("currentTime") is not None:
            ct = st["currentTime"] or 0.0
            dur = st.get("duration") or 0.0
            progress_pct = round((ct / dur * 100) if dur > 0 else 0, 1)

            # Detect stall
            if ct <= last_ct + 0.1:
                stalls += 1
                if stalls % 4 == 0:
                    ensure_playing(page)
            else:
                stalls = 0
            last_ct = ct

            moved = moves.tick(page, ct)   # fire due human-move events
            if moved:
                _hlog.info("tab %s human move(s) fired at %.0fs: %s",
                           tab_id, ct, "+".join(moved))

            telemetry = {
                "tabId": tab_id,
                "title": title,
                "status": "watching",
                "currentTime": round(ct, 1),
                "duration": round(dur, 1),
                "progressPct": progress_pct,
                "targetSeconds": round(target_seconds, 1),
                "ip": (ip_info or {}).get("addr", "direct"),
                "country": (ip_info or {}).get("cc", "US"),
                "stalled": stalls > 3,
            }
            if moved:
                telemetry["humanMove"] = "+".join(moved)

            if streamer:
                streamer.push_frame(page, telemetry, min_interval=1.2)

            if ct >= target_seconds:
                _hlog.info("tab %s watch done: fired human moves %s",
                           tab_id, moves.summary().get("fired"))
                telemetry["status"] = "completed"
                if streamer:
                    streamer.push_frame(page, telemetry, min_interval=0.1)
                return {"reached": True, "currentTime": ct, "duration": dur,
                        "humanMoves": moves.summary()}

        if now - start_time > max_wall:
            _hlog.info("tab %s watch wall-timeout: fired human moves %s",
                       tab_id, moves.summary().get("fired"))
            telemetry = {
                "tabId": tab_id,
                "title": title,
                "status": "timeout",
                "currentTime": last_ct,
                "duration": (st or {}).get("duration", 0),
                "ip": (ip_info or {}).get("addr", "direct"),
                "country": (ip_info or {}).get("cc", "US"),
                "humanMoves": moves.summary(),
            }
            if streamer:
                streamer.push_frame(page, telemetry, min_interval=0.1)
            return {"reached": False, "currentTime": last_ct,
                    "duration": (st or {}).get("duration", 0),
                    "humanMoves": moves.summary()}

        time.sleep(random.uniform(1.2, 2.0))
