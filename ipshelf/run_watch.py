"""IP-shelf watch-run orchestrator: ONE YouTube link watched by a set of
channels, each through its own bound ("sticky") gold shelf IP.

Core law of the shelf: one channel = one sticky gold IP for life; a watch run
= one YouTube link watched by a set of 10 channels, each drawing from its own
region's shelf. This script resolves the channel set (shared DB first,
channels_inventory.json fallback), draws each channel's IP via
ipshelf.core.assigner (with the real quick-gold gate), then runs the existing
engine warm-up session (engine.warmup.run_session) per channel sequentially -
one browser at a time, human-paced.

Usage (cwd = yt-channel-automation, venv python):
    python ipshelf\\run_watch.py --url https://www.youtube.com/watch?v=XXXX
        [--handles @a,@b | --count 10] [--region US] [--auto-harvest]
        [--dry-run] [--delay-min 60] [--delay-max 120]

Exit codes: 0 = at least one channel session succeeded; 1 = all channel
sessions failed (or nothing ran); 2 = aborted before execution (unknown or
personal handle, missing --region for unbound channels, starved shelf,
ipshelf core missing).
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # yt-channel-automation/
if str(ROOT) not in sys.path:  # allow running as a file OR as -m
    sys.path.insert(0, str(ROOT))

try:
    from engine.config import PROJECT_ROOT
except Exception:  # pragma: no cover - engine is always present in practice
    PROJECT_ROOT = ROOT

from wu_lib import CaptchaWall

# The user's PERSONAL channel - NEVER automate it, under any circumstances.
PERSONAL_HANDLES = {"@ZarifBenAlam"}


# --------------------------------------------------------------------------- #
# data dir (same rule as the ipshelf core: env IPSHELF_DATA, default
# ipshelf/data under the project), run status + JSON-lines log
# --------------------------------------------------------------------------- #

def data_dir():
    env = os.environ.get("IPSHELF_DATA")
    return Path(env) if env else PROJECT_ROOT / "ipshelf" / "data"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_status(status):
    p = data_dir() / "run_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def log_evt(evt, **fields):
    p = data_dir() / "ipshelf.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "evt": evt, **fields}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_core():
    try:
        from ipshelf.core import assigner, gate, shelf
    except ImportError as e:
        raise SystemExit(
            f"ipshelf core not importable ({e}). The shelf core "
            "(ipshelf/core/shelf.py, assigner.py, gate.py) must be built first.")
    return assigner, gate, shelf


# --------------------------------------------------------------------------- #
# channel resolution: shared DB rows first, channels_inventory.json fallback
# --------------------------------------------------------------------------- #

def _inventory_channels():
    try:
        inv = json.loads(
            (ROOT / "channels_inventory.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for c in inv.get("channels", []):
        h = (c.get("handle") or "").strip()
        # type "personal" is never automated; only created channels are eligible
        if not h or c.get("type") != "created" or h in PERSONAL_HANDLES:
            continue
        out.append({"handle": h, "name": c.get("name") or h})
    return out


def resolve_channels():
    """Eligible pool: shared-DB channels with status created|warming (the
    personal channel is excluded by the status filter AND the hardcoded
    blocklist); channels_inventory.json (type=created) as fallback."""
    try:
        import engine.db as db
        rows = [c for c in db.list_channels()
                if c["status"] in ("created", "warming")
                and (c["handle"] or "") not in PERSONAL_HANDLES]
        if rows:
            return [{"handle": c["handle"], "name": c["name"]} for c in rows], "db"
    except Exception:
        pass
    return _inventory_channels(), "inventory"


def pick_handles(pool, args):
    """Explicit handle set (validated) or a random sample of --count."""
    if args.handles:
        wanted = [h.strip() for h in args.handles.split(",") if h.strip()]
        personal = sorted(set(wanted) & PERSONAL_HANDLES)
        if personal:
            raise SystemExit(f"refusing to run: {personal} is the user's "
                             "personal channel and must never be automated")
        known = {c["handle"] for c in pool}
        unknown = [h for h in wanted if h not in known]
        if unknown:
            raise SystemExit(f"unknown handle(s): {unknown}\n"
                             f"known eligible handles: {sorted(known)}")
        return wanted
    if not pool:
        raise SystemExit("no eligible channels found (shared DB and "
                         "channels_inventory.json both came up empty)")
    k = min(args.count, len(pool))
    if k < args.count:
        print(f"warning: only {k} eligible channels available (asked {args.count})")
    if k <= 0:
        raise SystemExit("no channels picked (--count too small / empty pool)")
    return [c["handle"] for c in random.sample(pool, k)]


# --------------------------------------------------------------------------- #
# shelf draw phase
# --------------------------------------------------------------------------- #

class RegionMissing(Exception):
    def __init__(self, handles):
        super().__init__(f"no region for {handles}")
        self.handles = handles


class ShelfStarving(Exception):
    def __init__(self, regions, detail):
        super().__init__(f"shelf empty for {regions}")
        self.regions = regions
        self.detail = detail


def harvest_module():
    """Locate the harvest entry module. The core contract names
    ipshelf.hunt.harvest; the shipped hunt package uses a cycle runner, so
    probe a few candidates and fall back to the documented name (which may
    appear later)."""
    import importlib.util
    for name in ("ipshelf.hunt.harvest", "ipshelf.hunt.hunt_cycle"):
        try:
            if importlib.util.find_spec(name):
                return name
        except (ImportError, ValueError):
            continue
    return "ipshelf.hunt.harvest"


def run_harvest():
    mod = harvest_module()
    print(f"[harvest] spawning: {sys.executable} -m {mod} (waiting; "
          "this can take a while)")
    try:
        r = subprocess.run([sys.executable, "-m", mod], cwd=str(ROOT))
        print(f"[harvest] exited {r.returncode}")
        return r.returncode == 0
    except OSError as e:
        print(f"[harvest] failed to spawn: {e}")
        return False


def _draw_one(assigner, gate, handle, region, offline):
    gate_fn = None if offline else gate.quick_gold_check
    r = assigner.draw_for_channel(handle, region=region, gate_fn=gate_fn)
    if isinstance(r, dict) and r.get("error") == "no_region":
        raise RegionMissing([handle])
    return r


def draw_all(assigner, gate, shelf, handles, region_arg, auto_harvest,
             offline=False):
    """Draw a shelf IP for every handle. Returns {handle: draw-dict}.
    Raises RegionMissing (user must pass --region) or ShelfStarving (shelf
    cannot cover its channels; with --auto-harvest one harvest+retry is
    attempted first)."""
    bindings = shelf.load_bindings()
    unbound = [h for h in handles if not bindings.get(h)]
    if unbound and not region_arg:
        raise RegionMissing(unbound)

    draws = {}
    starving = []  # (handle, region tried)
    for h in handles:
        # bound channels keep their locked region (region=None -> assigner
        # uses the binding); unbound channels take --region
        reg = region_arg if not bindings.get(h) else None
        r = _draw_one(assigner, gate, h, reg, offline)
        draws[h] = r
        if isinstance(r, dict) and r.get("error") == "shelf_empty":
            starving.append((h, reg or (bindings.get(h) or {}).get("region") or "?"))

    if starving:
        regions = sorted({rg for _, rg in starving})
        if not auto_harvest:
            raise ShelfStarving(regions, [(h, draws[h]) for h, _ in starving])
        print(f"shelf empty for region(s) {regions} - auto-harvest ...")
        run_harvest()
        still = []
        for h, reg in starving:  # retry ONCE after the harvest
            r = _draw_one(assigner, gate, h, reg, offline)
            if isinstance(r, dict) and r.get("error") == "shelf_empty":
                still.append((h, reg, r))
            else:
                draws[h] = r
        if still:
            raise ShelfStarving(sorted({rg for _, rg, _ in still}),
                                [(h, r) for h, _, r in still])
    return draws


# --------------------------------------------------------------------------- #
# execution phase (engine pattern: one browser at a time)
# --------------------------------------------------------------------------- #

def _inventory_map():
    try:
        inv = json.loads(
            (ROOT / "channels_inventory.json").read_text(encoding="utf-8"))
        return {c.get("handle"): c for c in inv.get("channels", [])}
    except Exception:
        return {}


def session_rows(handle):
    """(acc, ch) rows for engine.warmup.run_session: real shared-DB rows when
    the channel is in the DB; otherwise minimal inventory-shaped dicts with
    account "default" (cloak-profile - the logged-in session)."""
    ch = acc = None
    try:
        import engine.db as db
        ch = db.conn().execute(
            "SELECT * FROM Channel WHERE handle=? ORDER BY createdAt LIMIT 1",
            (handle,)).fetchone()
        if ch is not None and ch["accountId"]:
            acc = db.get_account(account_id=ch["accountId"])
    except Exception:
        pass
    if acc is None:
        # single logged-in Gmail that all created channels live under
        acc = {"id": "default", "email": "default",
               "profileDir": "cloak-profile", "lane": None,
               "laneConfig": "{}", "status": "active"}
    if ch is None:
        e = _inventory_map().get(handle) or {}
        ch = {"id": e.get("id") or handle, "accountId": "default",
              "ytChannelId": e.get("id"), "name": e.get("name") or handle,
              "handle": handle, "url": "", "status": "created",
              "warmupDay": 0, "niche": None}
    return acc, ch


def extract_video_id(url):
    m = re.search(
        r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})",
        url or "")
    return m.group(1) if m else None


def proxy_dead(e):
    return "ProxyDead" in type(e).__name__ or "navigation kept failing" in str(e)


def pick_variant(variants, prefix):
    """First proxy URL variant starting with prefix (items may be plain
    strings or dicts carrying url/proxy_url)."""
    for v in variants or []:
        u = v if isinstance(v, str) else (
            (v or {}).get("url") or (v or {}).get("proxy_url"))
        if isinstance(u, str) and u.startswith(prefix):
            return u
    return None


def execute_channel(warmup, assigner, handle, drawn, url,
                    headful=False, demo_moves=False, no_chain=False):
    """One channel's watch session through its drawn shelf IP. Returns the
    per-channel summary record; never raises."""
    acc, ch = session_rows(handle)
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "", handle.lstrip("@")) or "channel"
    payload = {"seedVideo": url, "engage": False, "watchPct": None}
    vid = extract_video_id(url)
    if vid and not no_chain:
        # The engine's default chainTarget comes from niches.yaml and belongs
        # to the default seed's channel; an arbitrary run URL's channel will
        # not list it in its Videos tab, which would abort the session
        # mid-run. Point the in-app chain hop at the run URL's own video so
        # the session stays about THIS link and keeps the human in-app
        # navigation pattern.
        payload["chainTarget"] = vid
    if headful:
        payload["headful"] = True
    if demo_moves:
        payload["demoMoves"] = True
    proxy = drawn.get("proxy_url")
    rec = {"handle": handle, "ip": drawn.get("addr"), "ok": False,
           "reached": None, "error": None}

    def finish(res):
        watches = res.get("watch") or []
        rec["reached"] = watches[0].get("reached") if watches else None
        if res.get("error"):
            rec["error"] = str(res.get("error"))[:200]
        else:
            rec["ok"] = True
        return rec

    try:
        return finish(warmup.run_session(acc, ch, payload, proxy=proxy,
                                         evidence_prefix=f"run_{clean}"))
    except CaptchaWall as e:
        # exit IP is flagged: burn it (release of the binding happens inside
        # burn_ip, per the shelf contract)
        try:
            assigner.burn_ip(drawn.get("addr"), "captcha_flagged")
            print(f"  [shelf] burned {drawn.get('addr')} (captcha_flagged)")
        except Exception as be:
            print(f"  [shelf] burn_ip failed: {be}")
        rec["error"] = f"captcha wall: {str(e)[:120]}"
        return rec
    except Exception as e:
        # socks5 -> socks4 retry: Chromium accepts both schemes, and many
        # free "socks5" proxies are actually socks4
        if (proxy_dead(e) and str(proxy or "").startswith("socks5://")
                and str(drawn.get("proto") or "").upper().startswith("SOCKS")):
            alt = pick_variant(drawn.get("proxy_variants"), "socks4://")
            if alt:
                print(f"  [proxy] socks5 load failed; retrying once via {alt}")
                try:
                    return finish(warmup.run_session(
                        acc, ch, payload, proxy=alt,
                        evidence_prefix=f"run_{clean}"))
                except CaptchaWall as e2:
                    try:
                        assigner.burn_ip(drawn.get("addr"), "captcha_flagged")
                        print(f"  [shelf] burned {drawn.get('addr')} "
                              f"(captcha_flagged, socks4 retry)")
                    except Exception as be:
                        print(f"  [shelf] burn_ip failed: {be}")
                    rec["error"] = f"captcha wall (socks4 retry): {str(e2)[:120]}"
                    return rec
                except Exception as e2:
                    e = e2
        if proxy_dead(e):
            # proxy connect failed hard: free the binding + burn the IP
            try:
                assigner.release(handle, "proxy_dead")
            except Exception as le:
                print(f"  [shelf] release failed: {le}")
            try:
                assigner.burn_ip(drawn.get("addr"), "proxy_dead")
                print(f"  [shelf] burned {drawn.get('addr')} (proxy_dead)")
            except Exception as be:
                print(f"  [shelf] burn_ip failed: {be}")
            rec["error"] = f"proxy dead: {str(e)[:120]}"
        elif "get advanced features" in str(e).lower():
            rec["error"] = "verify gate (account needs verification)"
        else:
            rec["error"] = str(e)[:200]
        return rec


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Watch-run orchestrator: one YouTube link watched by a "
                    "set of channels, each through its own sticky gold "
                    "shelf IP.")
    ap.add_argument("--url", required=True, help="YouTube link to watch")
    ap.add_argument("--handles",
                    help="explicit channel set, comma-separated (e.g. @a,@b)")
    ap.add_argument("--count", type=int, default=10,
                    help="how many channels to pick at random (default 10)")
    ap.add_argument("--region",
                    help="region for channels with no binding yet "
                         "(US|GB|DE|T1...); required if any picked channel "
                         "is unbound - the first draw locks it forever")
    ap.add_argument("--auto-harvest", action="store_true",
                    help="if a shelf is empty: spawn the harvest module, "
                         "wait, and retry the draw once")
    ap.add_argument("--dry-run", action="store_true",
                    help="draw IPs and print the plan without launching any "
                         "browser")
    ap.add_argument("--delay-min", type=int, default=60,
                    help="min seconds between channel sessions (default 60)")
    ap.add_argument("--delay-max", type=int, default=120,
                    help="max seconds between channel sessions (default 120)")
    ap.add_argument("--offline-gate", action="store_true",
                    help=argparse.SUPPRESS)  # hidden: stub-data dry-runs only
                    # (draw with gate_fn=None: trust stored gold status)
    ap.add_argument("--headful", action="store_true",
                    help="visible browser window (demo / manual watching)")
    ap.add_argument("--demo-moves", action="store_true",
                    help="force ALL human-move probabilities to 1.0 for this "
                         "run — every move type fires in every watch")
    ap.add_argument("--no-chain", action="store_true",
                    help="skip the in-app chain hop (single watch-through)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if "youtube.com" not in args.url and "youtu.be" not in args.url:
        print(f"warning: --url does not look like a YouTube link: {args.url}")
    if args.delay_min > args.delay_max:
        raise SystemExit("--delay-min must be <= --delay-max")
    if args.handles and args.count != 10:
        print("note: --handles given; --count is ignored")

    assigner, gate, shelf = load_core()
    pool, source = resolve_channels()
    if not pool:
        raise SystemExit("no eligible channels found (shared DB has no "
                         "created|warming channels and channels_inventory.json "
                         "has no type=created channels)")
    picked = pick_handles(pool, args)
    print(f"channel source: {source}; picked {len(picked)}: {picked}")

    status = {"running": True, "started_at": now_iso(), "url": args.url,
              "channels": [], "stage": "drawing", "results": []}
    write_status(status)
    log_evt("run_start", url=args.url, count=len(picked), handles=picked,
            region=args.region, dry_run=bool(args.dry_run), source=source)

    def abort(msg, code=2):
        status["stage"] = "failed"
        status["running"] = False
        status["error"] = msg[:500]
        write_status(status)
        log_evt("run_done", ok=0, failed=0, total=len(picked), aborted=True,
                error=str(msg)[:200])
        print(msg)
        return code

    try:
        draws = draw_all(assigner, gate, shelf, picked, args.region,
                         args.auto_harvest, offline=args.offline_gate)
    except RegionMissing as e:
        return abort(f"no region bound for: {e.handles} - pass --region "
                     f"US|GB|DE|T1 (a channel's first draw locks its region "
                     f"forever)")
    except ShelfStarving as e:
        return abort(f"shelf empty for region(s) {e.regions} "
                     f"({'harvest ran, still empty' if args.auto_harvest else 'no --auto-harvest'}). "
                     f"Harvest more gold IPs for {e.regions} "
                     f"(python -m {harvest_module()}); details: "
                     f"{str(e.detail)[:300]}")

    # channel plan (regions come from the bindings the draw just wrote)
    bindings = shelf.load_bindings()
    for h in picked:
        d = draws.get(h) or {}
        status["channels"].append({
            "handle": h,
            "region": (bindings.get(h) or {}).get("region") or args.region or "?",
            "ip": d.get("addr") or (bindings.get(h) or {}).get("sticky_ip"),
            "fresh": bool(d.get("fresh")),
        })

    if args.dry_run:
        print(f"\nDRY RUN - plan for {args.url}")
        for c in status["channels"]:
            d = draws[c["handle"]]
            print(f"  {c['handle']:<28} region={str(c['region']):<4} "
                  f"ip={str(c['ip'] or '?'):<22} fresh={str(c['fresh']):<5} "
                  f"proxy={d.get('proxy_url')}")
        status["stage"] = "done"
        status["running"] = False
        status["dry_run"] = True
        write_status(status)
        log_evt("run_done", ok=len(picked), failed=0, total=len(picked),
                dry_run=True)
        print(f"\ndry run complete: {len(picked)} channel(s) bound to IPs "
              "(sticky bindings persist for the real run)")
        return 0

    import engine.warmup as warmup
    status["stage"] = "running"
    write_status(status)

    for i, h in enumerate(picked):
        d = draws[h]
        print(f"\n[{i + 1}/{len(picked)}] {h} via {d.get('proxy_url')} ...")
        if "error" in (d or {}):  # unexpected draw error surfaced per channel
            rec = {"handle": h, "ip": d.get("addr"), "ok": False,
                   "reached": None, "error": f"draw: {d.get('error')}"}
        else:
            rec = execute_channel(warmup, assigner, h, d, args.url,
                                  headful=args.headful,
                                  demo_moves=args.demo_moves,
                                  no_chain=args.no_chain)
        status["results"].append(rec)
        write_status(status)
        log_evt("run_channel", **{k: rec.get(k) for k in
                                  ("handle", "ip", "ok", "reached", "error")})
        print(f"  -> ok={rec['ok']} reached={rec['reached']} "
              f"error={rec['error']}")
        if i < len(picked) - 1:  # human pacing between sessions
            pause = random.randint(args.delay_min, args.delay_max)
            print(f"  (pacing: {pause}s until next channel)")
            time.sleep(pause)

    ok = sum(1 for r in status["results"] if r.get("ok"))
    failed = len(picked) - ok
    status["stage"] = "done"
    status["running"] = False
    status["summary"] = {"ok": ok, "failed": failed, "total": len(picked)}
    write_status(status)
    log_evt("run_done", ok=ok, failed=failed, total=len(picked))

    line = "=" * 64
    print("\n" + line)
    print(f"WATCH RUN DONE - {args.url}")
    print(line)
    for r in status["results"]:
        print(f"  {r['handle']:<28} ip={str(r['ip']):<22} ok={str(r['ok']):<5} "
              f"reached={str(r['reached']):<5} error={r['error']}")
    print(f"total={len(picked)}  ok={ok}  failed={failed}")
    print(f"status file: {data_dir() / 'run_status.json'}")
    return 0 if ok >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
