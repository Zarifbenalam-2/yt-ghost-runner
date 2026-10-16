"""Warm-up session adapter for ghost-runner.

This is a simplified version of the parent project's warmup.py. Instead of the
full wu_flow sequence (channel switch -> seed watch -> in-app chain -> engage),
it delegates to ghost_watch.run_tab_session which handles the core watch loop.

This keeps the ghost-runner self-contained without needing wu_flow.py,
engine.create_channels, or the full YAML config chain.
"""
import random

from playwright.sync_api import sync_playwright

from engine.browser import open_proxied_arg
from hunt_lib import capture
from wu_lib import (
    CaptchaWall,
    ensure_playing,
    is_signed_in,
    video_state,
    watch_until,
)


def run_session(acc, channel, payload, proxy, evidence_prefix="wu"):
    """Execute one warm-up session. Returns result dict (never raises for
    expected failure classes; those are handled by the caller)."""
    results = {"watch": [], "engagement": None}

    headful = bool(payload.get("headful"))
    vid_url = payload.get("seedVideo", "https://www.youtube.com/")

    # watch percentage
    watch_pct = payload.get("watchPct") or random.uniform(55, 95) / 100.0

    with sync_playwright() as p:
        try:
            ctx, page = open_proxied_arg(
                p, proxy, headless=not headful,
                account_id=acc.get("id", "default") if isinstance(acc, dict) else str(acc),
            )
        except Exception as e:
            results["error"] = f"browser launch failed: {e}"
            return results

        try:
            # navigate to YouTube
            page.goto(vid_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # captcha check
            url = page.url or ""
            if "google.com/sorry" in url:
                raise CaptchaWall(f"CAPTCHA wall: {url[:120]}")

            # ensure playback
            ensure_playing(page)

            # get video duration
            dur = None
            for _ in range(10):
                st = video_state(page) or {}
                if st.get("duration"):
                    dur = st["duration"]
                    break
                page.wait_for_timeout(2000)

            if not dur:
                results["error"] = "video never exposed duration"
                capture(page, f"{evidence_prefix}_no_duration", "no media")
                return results

            target = min(dur * watch_pct, dur - 5)
            r = watch_until(page, target, label=evidence_prefix,
                            max_wall_seconds=int(target * 3))
            results["watch"].append({
                "video": vid_url,
                "duration": dur,
                "reached": r.get("reached"),
                "currentTime": r.get("currentTime"),
            })
            capture(page, f"{evidence_prefix}_done", "session complete",
                    str(results)[:200])
            return results
        except CaptchaWall:
            results["error"] = "captcha wall"
            raise
        except Exception as e:
            results["error"] = str(e)[:200]
            return results
        finally:
            try:
                ctx.close()
            except Exception:
                pass
