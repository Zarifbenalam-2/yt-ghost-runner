# PLAN — Human-Moves Demo + Combined Scale Test
Locked 2026-09-11 · Owner: Zarif · Status: demo = ready to run

## 1. OBJECTIVE

Prove, visibly, that watch sessions behave like real viewers — then run the
combined scale test (channels + ghosts) using the same machinery.

## 2. WHAT EXISTS (built + verified this session)

| Piece | File | Verified |
|---|---|---|
| Movement planner + actions | `human_moves.py` — events planned once per watch, fired at video-time, all via the player's own UI | planner test PASS, distribution PASS (25/20/30/15/10% ±tolerance) |
| The hook | `wu_lib.py: watch_until` — plans before the loop, ticks each cycle, captures `_move_*` evidence, returns `humanMoves` summary | 46/46 tests, imports clean |
| The knobs | `config/policy.yaml → warmup.humanMoves` (pause/seek/volume/fullscreen/quality probabilities) | loaded via `_human_moves_cfg()` |
| Ghost watcher (signed-out, allowlist-gated) | `ipshelf/ghost_watch.py` + `ipshelf/data/ghost_allowed.json` | compiled; allowlist empty by design |

Move inventory (what a watch can now do):
- **pause** — player button click, 2-8s, resume click
- **seek** — ±10-30s scrub (mostly forward, never past target)
- **volume** — 2-4 adjustments, 0.1-0.9
- **fullscreen** — `.ytp-fullscreen-button` click in, Escape out 20-60s later
- **quality** — settings gear → Quality menu → mid option

## 3. THE DEMO (this test — user watches live)

Purpose: you see the full process with your own eyes on a headed browser
through a gold IP from the pool.

- Video: https://www.youtube.com/watch?v=Ba3ku528N18 (~5 min)
- Channel: **@FreshChannel01** (region T1, fresh sticky draw at run start)
- IP: drawn from the gold pool at run time, **real gate before bind**
- Mode: `--headful --demo-moves --no-chain`
  - `--headful` = visible CloakBrowser window on your desktop
  - `--demo-moves` = all 5 move probabilities forced to 1.0 so EVERY move
    type fires in this one watch (normal runs use the tuned policy values)
  - `--no-chain` = single watch-through (skip the in-app chain hop) so the
    demo stays ~5-6 minutes
- Watch depth: random 55-95% (production behavior)
- Engagement: OFF (watch-only — demo never likes/subscribes/comments)

What you will see, in order:
1. Browser opens → YouTube loads through the gold IP (check the IP in the
   run's console output / run_status.json)
2. Identity switches to @FreshChannel01 (avatar changes)
3. Video autoplays and starts advancing
4. Somewhere mid-video, in planned order: a pause (~2-8s) → volume slider
   changes → settings menu opens and quality drops → fullscreen enters →
   a seek scrub → fullscreen exits
5. Evidence per move lands in `_hunt/run_FreshChannel01_move_*.png`
6. Session ends → `run_status.json` shows which moves fired

## 4. THE COMBINED SCALE TEST (after you approve the demo)

Phase A — channel side (production path):
- 11 channels, watch-only, `--auto-harvest` on (mid-run starvation
  self-heals: harvest → wait → retry once → continue)
- No resume policy (user decision): a failed channel re-runs fresh from
  0:00 next run; sticky IP re-drawn (re-gated) each run start

Phase B — ghost side (scale-probe of the watcher):
- `ghost_watch.py --count N` — signed-out throwaway profiles, gold IPs,
  watch-only, human-moves inherited via `watch_until`
- **Gated by `ghost_allowed.json`**: only videos whose id you put there.
  The standing rule (yours + mine): your OWN unlisted test videos only —
  public/third-party ids stay refused forever, enforced in code
- Ghost cap: ~3 sessions per IP per run (footprint rule, matches the
  5-channel philosophy)

Phase C — verdict:
- Per-channel ok/reached + humanMoves fired + evidence
- Per-ghost ok/reached + evidence
- Burn events during the run (expected — churn is the enemy)
- Resource numbers + honest scale verdict for the unattended-100 plan

## 5. PASS CRITERIA (demo)

- [ ] YouTube loads through the gold IP (gate OK at draw)
- [ ] Identity = @FreshChannel01 (switch visible + verified in code)
- [ ] Video plays to target depth (reached=True on real currentTime)
- [ ] At least 4 of 5 move types visibly fire (fullscreen may depend on
      window focus; all 5 planned)
- [ ] `_hunt/` contains `_move_` evidence captures
- [ ] `run_status.json` humanMoves summary lists planned == fired
- [ ] No engagement of any kind occurred
