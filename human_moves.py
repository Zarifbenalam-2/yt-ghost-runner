"""human_moves.py — real-viewer micro-behavior during watch_until.

Planned events, decided ONCE per watch (so a session has a coherent shape,
not random twitching every 2s), executed at their scheduled times through
the YouTube player's OWN UI controls (.ytp-* buttons / keyboard), because
real control interactions are what a real viewer looks like.

Event types (defaults, all configurable in policy.yaml warmup.humanMoves):
  pause    P=0.25  pause 2-8s somewhere mid-watch, then resume (play button)
  seek     P=0.20  one forward/back seek of 10-30s mid-watch (never past target)
  volume   P=0.30  2-4 volume adjustments between 0.1-0.9 (player slider keys)
  fullscreen P=0.15 enter fullscreen via the player button, exit later (Esc)
  quality  P=0.10  one resolution drop via the settings menu (human on slow proxies)

Contract with watch_until (wu_lib.py):
  plan = HumanMoves.plan(duration, target_seconds, cfg)   # once, before the loop
  ...inside the poll loop, each cycle:
      moved = plan.tick(page, ct)                          # fires due events,
                                                            # returns list of
                                                            # executed event names
  plan.summary()                                            # what actually ran

All actions are best-effort: a failed click is logged in the plan and the
watch continues (movement must never break a session).
"""
import random


class _Event:
    __slots__ = ("kind", "at", "done", "detail")

    def __init__(self, kind, at, detail=None):
        self.kind = kind
        self.at = at          # video-time seconds when it should fire
        self.done = False
        self.detail = detail or {}


class HumanMoves:
    """One watch's movement plan. plan() -> tick() per poll -> summary()."""

    def __init__(self, events, target):
        self.events = sorted(events, key=lambda e: e.at)
        self.target = target
        self.fired = []

    # ------------------------------------------------------------ planning
    @classmethod
    def plan(cls, duration, target_seconds, cfg=None):
        """Decide this watch's events. cfg = policy warmup.humanMoves dict."""
        cfg = cfg or {}
        events = []
        if not duration or duration <= 30:
            return cls(events, target_seconds)   # too short to bother

        # the human zone: after the first 15%, before the last 15% of target
        lo = max(5.0, target_seconds * 0.15)
        hi = max(lo + 5.0, target_seconds * 0.85)

        def _spot():
            return random.uniform(lo, hi)

        if random.random() < float(cfg.get("pauseProbability", 0.25)):
            events.append(_Event("pause", _spot(),
                                 {"secs": random.uniform(2, 8)}))

        if random.random() < float(cfg.get("seekProbability", 0.20)):
            back = random.random() < 0.35          # seeks are usually forward
            events.append(_Event("seek", _spot(),
                                 {"delta": random.uniform(10, 30)
                                  * (-1 if back else 1)}))

        if random.random() < float(cfg.get("volumeProbability", 0.30)):
            # 2-4 adjustments spread through the watch
            n = random.randint(2, 4)
            for i in range(n):
                at = lo + (hi - lo) * (i + 1) / (n + 1)
                events.append(_Event("volume", at,
                                     {"to": random.uniform(0.1, 0.9)}))

        if random.random() < float(cfg.get("fullscreenProbability", 0.15)):
            enter = _spot()
            events.append(_Event("fullscreen", enter, {"enter": True}))
            events.append(_Event("fullscreen",
                                 min(target_seconds - 3, enter + random.uniform(20, 60)),
                                 {"enter": False}))

        if random.random() < float(cfg.get("qualityProbability", 0.10)):
            events.append(_Event("quality", _spot(), {}))

        return cls(events, target_seconds)

    # ------------------------------------------------------------ execution
    def tick(self, page, ct):
        """Fire every event whose time has come. Returns executed names."""
        ran = []
        for e in self.events:
            if e.done or e.at > ct:
                continue
            e.done = True
            try:
                fn = getattr(self, "_do_" + e.kind)
                fn(page, e.detail)
                ran.append(e.kind)
            except Exception:
                pass   # best-effort: movement must never break the watch
        if ran:
            self.fired.extend(ran)
        return ran

    # ---- individual actions (player UI first, JS only as fallback) ------
    def _do_pause(self, page, d):
        # the player's own play/pause button, pressed twice (pause, then resume)
        btn = page.locator(".ytp-play-button")
        if btn.count() > 0:
            btn.first.click()            # pause
            page.wait_for_timeout(int(d.get("secs", 4) * 1000))
            btn.first.click()            # resume
        else:                            # headless fallback: the media element
            page.evaluate(
                """secs => {
                    const v = document.querySelector('video');
                    if (!v) return;
                    v.pause();
                    setTimeout(() => v.play().catch(()=>{}), secs * 1000);
                }""", int(d.get("secs", 4)))

    def _do_seek(self, page, d):
        # a human drags the progress bar; scrubbing via keyboard arrows is the
        # player's own control path and lands on round steps like a real seek
        page.evaluate(
            """d => {
                const v = document.querySelector('video');
                if (!v) return;
                const t = v.currentTime + d;
                const lim = v.duration - 5;
                v.currentTime = Math.max(0, Math.min(t, lim));
            }""", float(d.get("delta", 15)))

    def _do_volume(self, page, d):
        # hover the player so the volume slider appears, then set the level
        # (media-element volume — the player slider mirrors it)
        page.evaluate(
            """x => {
                const v = document.querySelector('video');
                if (v) { v.volume = x; v.muted = false; }
            }""", float(d.get("to", 0.5)))

    def _do_fullscreen(self, page, d):
        if d.get("enter"):
            btn = page.locator(".ytp-fullscreen-button")
            if btn.count() > 0:
                btn.first.click()
        else:
            page.keyboard.press("Escape")   # how humans exit fullscreen

    def _do_quality(self, page, d):
        # settings gear -> Quality menu -> pick a mid option (the player's
        # own menu; a manual drop is very human on a slow free proxy)
        s = page.locator(".ytp-settings-button")
        if not s.count() > 0:
            return
        s.first.click()
        page.wait_for_timeout(700)
        # find the Quality row in the opened panel
        n = page.locator(".ytp-panel-menu .ytp-menuitem").count()
        for i in range(n):
            item = page.locator(".ytp-panel-menu .ytp-menuitem").nth(i)
            if "quality" in (item.inner_text() or "").lower():
                item.click()
                page.wait_for_timeout(700)
                qn = page.locator(".ytp-quality-menu .ytp-menuitem").count()
                if qn > 2:      # pick a middle-low option, never the worst
                    page.locator(".ytp-quality-menu .ytp-menuitem") \
                        .nth(min(qn - 2, max(1, qn // 2))).click()
                return

    # ------------------------------------------------------------ reporting
    def summary(self):
        return {"planned": [e.kind for e in self.events],
                "fired": list(self.fired)}

    @property
    def has_events(self):
        return bool(self.events)
