"""server.py — Ghost Runner Central Dashboard Server & Action Dispatcher.

Features:
1. Live Tab Screen Grid HUD (real-time stream feed of all active tabs).
2. GitHub Actions Spawner (triggers workflow_dispatch via GitHub REST API).
3. Local Test Tab Runner (spawns local background ghost tabs for instant verification).
4. Manual IP Harvest & Pool Management.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import config as _cfg                       # noqa: E402
from hub.logging_setup import get_logger             # noqa: E402

log = get_logger("server")
UI_INDEX = ROOT / "ui" / "index.html"
DATA_DIR = ROOT / "data"
FEED_DATA = DATA_DIR / "live_feed.json"

# In-memory live tab frame store
_LIVE_TABS = {}
_LIVE_LOCK = threading.Lock()

# Cached read of the cloud fleet's live-deck branch (see /api/deck/live)
_LIVE_DECK_CACHE = {"deck": {}, "at": 0.0}

# ── Factory manager: harvester auto-runs ONLY while a watcher run is live ──
_FACTORY = {"proc": None, "started_at": None, "manual_stop": False,
            "watcher_active": None, "last_check": None}
_FACTORY_LOCK = threading.Lock()
_FACTORY_LOG = DATA_DIR / "factory_manager.log"


def _factory_log(msg):
    try:
        with open(_FACTORY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _gold_certified_count():
    try:
        with open(DATA_DIR / "factory_gold_log.jsonl", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _factory_running():
    with _FACTORY_LOCK:
        p = _FACTORY["proc"]
        return bool(p and p.poll() is None)


def _spawn_factory(reason):
    """Start the gold factory subprocess (no-op if already running/manual-stopped)."""
    with _FACTORY_LOCK:
        p = _FACTORY["proc"]
        if p and p.poll() is None:
            return False
        if _FACTORY["manual_stop"]:
            return False
        logf = open(_FACTORY_LOG, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "gold_factory.py"), "--ship", "--require-watch"],
            cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
        _FACTORY["proc"] = proc
        _FACTORY["started_at"] = time.time()
        _FACTORY["manual_stop"] = False
    _factory_log(f"STARTED ({reason}) pid={proc.pid}")
    return True


def _terminate_factory(reason):
    """Kill the factory subprocess without latching manual-stop."""
    with _FACTORY_LOCK:
        p = _FACTORY["proc"]
        _FACTORY["proc"] = None
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
        _factory_log(f"STOPPED ({reason}) pid={p.pid}")


def _git_ship_snapshot(message):
    """Commit + push the refreshed snapshot (what cloud runners bootstrap)
    so the sweep actually reaches the repo shelf, not just hub.db.
    Best-effort; retried because concurrent pushes (collector) race."""
    import subprocess as _sp
    _sp.run(["git", "add", "data/ip_pool_snapshot.json"], cwd=str(ROOT), check=False)
    for _ in range(8):
        _sp.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(ROOT),
                capture_output=True, text=True, timeout=60)
        _sp.run(["git", "-c", "user.email=ghost-hub@users.noreply.github.com",
                 "-c", "user.name=ghost-hub", "commit", "-m", message],
                cwd=str(ROOT), capture_output=True, text=True)
        r = _sp.run(["git", "push", "origin", "main"], cwd=str(ROOT),
                    capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return True
    return False


def factory_manager_loop():
    """Every 60s: watcher live -> factory runs; watcher gone -> factory stops.

    Also runs the rack re-gate sweep on the policy interval
    (policy 'regate_minutes', default 5) so the shelf continuously sheds
    exits that lost their gold grade.
    """
    from gold_factory import watcher_active
    last_sweep = 0.0
    while True:
        try:
            active = watcher_active()
            with _FACTORY_LOCK:
                _FACTORY["watcher_active"] = active
                _FACTORY["last_check"] = time.time()
                running = bool(_FACTORY["proc"] and _FACTORY["proc"].poll() is None)
                manual = _FACTORY["manual_stop"]
            if active and not running and not manual:
                _spawn_factory("watcher active")
            elif not active and running:
                _terminate_factory("watch ended")

            # ── periodic rack re-gate sweep (policy-driven interval) ──
            try:
                from hub import db, broker
                db.init()
                interval = max(1, int(db.get_policy("regate_minutes", 5))) * 60
                if time.time() - last_sweep >= interval:
                    last_sweep = time.time()

                    def _sweep_then_ship():
                        # previous behavior: the sweep only touched hub.db and
                        # never re-pushed the snapshot, so a sweep that burned
                        # bodies still left them in the committed shelf. Now:
                        # burn -> refresh snapshot -> commit -> push, exactly
                        # like collect.py, so the repo shelf learns.
                        res = broker.regate_sweep()
                        if res["checked"]:
                            n = broker.write_snapshot()
                            _git_ship_snapshot(
                                f"watch: post-sweep {n} gold [skip-ci]")
                            _factory_log(f"sweep: {res['alive']}/{res['checked']} alive, "
                                         f"burned {res['burned']}, snapshot {n}")
                    threading.Thread(target=_sweep_then_ship, daemon=True,
                                     name="regate-sweep").start()
            except Exception as se:
                _factory_log(f"sweep error: {type(se).__name__}: {se}")
        except Exception as e:
            _factory_log(f"manager error: {type(e).__name__}: {e}")
        time.sleep(60)


def resolve_github_token():
    """Find a GitHub token for the dashboard without asking the operator.

    Order: env GITHUB_TOKEN -> local git credential store (the PAT this clone
    already pushes with) -> data/.gh_token (gitignored). The work now happens
    on GitHub runners, so the dashboard is the CONTROL PANEL: without a token
    its run-control pane is blind ("Set GITHUB_TOKEN in the server env").
    """
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        return tok
    try:
        out = subprocess.run(
            ["git", "credential", "fill"], cwd=str(ROOT), input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    try:
        f = ROOT / "data" / ".gh_token"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    try:
        import json as _json
        sf = ROOT / "data" / "secrets.json"
        if sf.is_file():
            store = _json.loads(sf.read_text(encoding="utf-8"))
            return (store.get("tokens") or {}).get("github_pat", "")
    except Exception:
        pass
    return ""


def repo_slug_from_git():
    """owner/name from the origin remote (so GITHUB_REPOSITORY need not be set)."""
    try:
        url = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                             cwd=str(ROOT), capture_output=True, text=True,
                             timeout=10).stdout.strip()
        m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        pass
    return ""


def load_accounts():
    """Configured GitHub accounts for multi-account dispatch.

    Sources (merged, PAT never logged):
    1. accounts.json entries: {name, repo, pat_env} (pat read from env)
    2. Env fallback: GITHUB_TOKEN + GITHUB_REPOSITORY as account 'env'
    """
    accounts = []
    try:
        cfg = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))
        for entry in cfg.get("accounts", []):
            pat = os.environ.get(entry.get("pat_env", ""), "")
            if entry.get("repo") and pat:
                accounts.append({
                    "name": entry.get("name", entry["repo"]),
                    "repo": entry["repo"],
                    "pat": pat,
                })
    except Exception:
        pass
    if not accounts:
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if token and repo:
            accounts.append({"name": "env", "repo": repo, "pat": token})
    return accounts


def dispatch_workflow(repo, token, url, tabs, stream_target="", runners=None,
                      harvest_shards=2, depth_lo=20, depth_hi=45,
                      human_moves=True, unique_ips=True, claim_url=None):
    """POST workflow_dispatch to one repo. Returns (ok, message).

    Sends the FULL fleet config, not just url+tabs. The watch matrix is
    20-strong and every job above the `runners` input exits idle, so a
    dispatch carrying only video_url/tabs_per_runner silently ran FOUR
    runners (the workflow's default) — the dashboard could "start the
    system" and get a quarter of the fleet.

    `runners` defaults to the ceiling minus the harvest shards: 20 jobs is
    the Free-plan concurrent maximum, and the water-pile needs the harvest
    shards alive ALONGSIDE the watch (18 watch : 2 harvest).
    """
    try:
        ceiling = int(os.environ.get("JOB_CEILING", "20"))
    except ValueError:
        ceiling = 20
    hs = max(0, int(harvest_shards))
    if runners is None:
        runners = ceiling - hs
        try:
            runners = int(os.environ.get("DISPATCH_RUNNERS", str(runners)))
        except ValueError:
            pass
    # never over-subscribe the ceiling: queued jobs starve the water-pile
    runners = max(1, min(int(runners), ceiling - hs))
    dispatch_url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"cloud_ghost_watch.yml/dispatches"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ghost-Dashboard",
        "Content-Type": "application/json",
    }
    body = json.dumps({
        "ref": "main",
        "inputs": {
            "video_url": url,
            "tabs_per_runner": str(tabs),
            "runners": str(runners),
            "stream_target": stream_target,
            "relay_token": os.environ.get("RELAY_TOKEN", ""),
            "claim_url": claim_url or os.environ.get("RELAY_URL", ""),            "harvest_shards": str(hs),
            "harvest_par": "300",
            "depth_lo": str(depth_lo),
            "depth_hi": str(depth_hi),
            "human_moves": "true" if human_moves else "false",
            "unique_ips": "true" if unique_ips else "false",
            "use_ip_pool": "true",
        },
    }).encode("utf-8")
    try:
        req = urllib.request.Request(dispatch_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True, f"{repo}: dispatched {runners} runners x {tabs} tabs (+{hs} harvest)"
    except Exception as e:
        return False, f"{repo}: {e}"


def load_ip_shelf_stats():
    """Load count of gold, silver, and dead IPs from IP shelf."""
    try:
        from ipshelf.core import shelf
        pool = shelf.load_pool()
        exits = pool.get("exits", [])
        gold = sum(1 for e in exits if e.get("status") == "gold")
        dead = sum(1 for e in exits if e.get("status") == "dead")
        return {"gold": gold, "dead": dead, "total": len(exits)}
    except Exception:
        return {"gold": 0, "dead": 0, "total": 0}


class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            if UI_INDEX.exists():
                content = UI_INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, "UI file not found")
        elif self.path == "/api/state":
            ip_stats = load_ip_shelf_stats()
            # Clean up tabs that haven't updated in 45s
            now = time.time()
            with _LIVE_LOCK:
                active_tabs = {k: v for k, v in _LIVE_TABS.items()
                               if (now - v.get("_last_seen", 0)) < 45}
            self._send_json({
                "ipStats": ip_stats,
                "tabs": list(active_tabs.values()),
                "tabCount": len(active_tabs),
            })
        elif self.path == "/api/run_control":
            # Live state of the current watch + ability to stop it.
            # GET  /api/run_control            -> current run + per-runner state
            # POST /api/run_control ({"action":"cancel"}) -> cancel in flight
            repo = os.environ.get("GITHUB_REPOSITORY", _cfg.REPO)
            token = os.environ.get("GITHUB_TOKEN", "")
            try:
                from hub import gh, db
                db.init()
                run = gh.latest_run(repo, _cfg.WORKFLOW_WATCH, token)
                out = {"repo": repo, "run": None, "jobs": []}
                if run:
                    out["run"] = {k: run.get(k) for k in
                                  ("id", "run_number", "status", "conclusion",
                                   "created_at", "html_url")}
                    if run.get("status") in ("in_progress", "queued"):
                        try:
                            jobs = gh.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs",
                                          token=token)
                            for j in jobs.get("jobs", []):
                                out["jobs"].append({
                                    "name": j.get("name"), "status": j.get("status"),
                                    "conclusion": j.get("conclusion"),
                                    "started_at": j.get("started_at"),
                                    "steps": [{"name": s.get("name"),
                                               "status": s.get("status"),
                                               "conclusion": s.get("conclusion")}
                                              for s in (j.get("steps") or [])]})
                        except Exception:
                            pass
                self._send_json(out)
            except Exception as e:
                self._send_json({"error": f"run_control failed: {e}"}, 500)

        elif self.path == "/api/actions_live":
            # Boot/refresh detection of anything running across BOTH flows:
            # gold_factory.yml (harvest) and cloud_ghost_watch.yml (watch) —
            # the panel the dashboard polls on load and every 5-10s afterward.
            repo = os.environ.get("GITHUB_REPOSITORY", _cfg.REPO)
            token = os.environ.get("GITHUB_TOKEN", "")
            if not (repo and token):
                self._send_json({"error": "No GITHUB_REPOSITORY/GITHUB_TOKEN — "
                                          "run the pip/gh credential setup or "
                                          "export them. The dashboard cannot "
                                          "see GitHub Actions without a token."}, 400)
                return
            try:
                from hub import gh
                out = {"workflows": {}}
                for label, wf in (("watch", _cfg.WORKFLOW_WATCH),
                                  ("harvest", _cfg.WORKFLOW_FACTORY)):
                    try:
                        runs = gh.get(f"/repos/{repo}/actions/workflows/{wf}/runs"
                                      f"?per_page=5", token=token)
                        items = runs.get("workflow_runs", [])
                        live = [r for r in items
                                if r.get("status") in ("in_progress", "queued")]
                        out["workflows"][label] = {
                            "latest": {k: r.get(k) for k in
                                       ("id", "run_number", "status",
                                        "conclusion", "created_at", "html_url")
                                       for r in [items[0]] if items} if items else None,
                            "live": [{k: r.get(k) for k in
                                      ("id", "run_number", "created_at", "html_url")}
                                     for r in live],
                            "live_count": len(live),
                        }
                    except Exception as e:
                        out["workflows"][label] = {"error": f"{type(e).__name__}: {e}"}
                self._send_json(out)
            except Exception as e:
                self._send_json({"error": f"actions_live failed: {e}"}, 500)

        elif self.path == "/api/run_control/cancel":
            repo = os.environ.get("GITHUB_REPOSITORY", _cfg.REPO)
            token = os.environ.get("GITHUB_TOKEN", "")
            try:
                from hub import gh
                run = gh.latest_run(repo, _cfg.WORKFLOW_WATCH, token)
                if not run:
                    self._send_json({"ok": False, "error": "no run found"}, 404)
                elif run.get("status") not in ("in_progress", "queued"):
                    self._send_json({"ok": False,
                                     "error": f"run already {run.get('status')}"})
                else:
                    ok = gh.cancel_run(repo, run["id"], token)
                    self._send_json({"ok": ok,
                                     "message": f"cancel {'sent' if ok else 'failed'} "
                                                f"for run #{run.get('run_number')}"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/feed":
            with _LIVE_LOCK:
                tabs = list(_LIVE_TABS.values())
            self._send_json({"tabs": tabs})
        elif self.path == "/api/factory":
            with _FACTORY_LOCK:
                p = _FACTORY["proc"]
                self._send_json({
                    "running": bool(p and p.poll() is None),
                    "pid": p.pid if (p and p.poll() is None) else None,
                    "started_at": _FACTORY["started_at"],
                    "watcher_active": _FACTORY["watcher_active"],
                    "manual_stop": _FACTORY["manual_stop"],
                    "last_check": _FACTORY["last_check"],
                    "gold_certified": _gold_certified_count(),
                })
        elif self.path == "/api/deck":
            # ── Observation deck: live capacity, usage, rack, fleet ──
            try:
                from hub import db
                db.init()
                rack = db.rack_stats()
                fleet = db.fleet_capacity()
                with _FACTORY_LOCK:
                    p = _FACTORY["proc"]
                    fac = {"running": bool(p and p.poll() is None),
                           "watcher_active": _FACTORY["watcher_active"]}
                self._send_json({
                    "rack": rack,
                    "fleet": fleet,
                    "factory": fac,
                    "policy": db.all_policy(),
                    "capacity": {
                        "tabs_possible": rack["capacity"],
                        "tabs_riding": rack["riding"],
                        "utilisation_pct": round(rack["riding"] / rack["capacity"] * 100, 1)
                        if rack["capacity"] else 0,
                    },
                    "recent": db.recent_runs(25),
                })
            except Exception as e:
                self._send_json({"error": f"deck failed: {e}"}, 500)
        elif self.path == "/api/deck/live":
            # ── CLOUD watch, live, from the live-deck branch ──
            # Runners cannot reach this PC, and GitHub refuses to serve a
            # running job's logs, so the fleet commits its own state to the
            # `live-deck` branch mid-run. That branch is the only channel both
            # sides share — this endpoint turns it into the dashboard feed for
            # cloud runs (git fetch is cached, polling must not hammer git).
            try:
                now = time.time()
                if (not _LIVE_DECK_CACHE["deck"]
                        or now - _LIVE_DECK_CACHE["at"] > 45):
                    import live_deck
                    _LIVE_DECK_CACHE["deck"] = live_deck.read_deck(fetch=True)
                    _LIVE_DECK_CACHE["at"] = now
                deck = _LIVE_DECK_CACHE["deck"]
                runners = []
                tot_ok = tot_done = tot_tabs = 0
                for rid, d in deck.items():
                    tabs = d.get("tabs", [])
                    meta = d.get("meta", {}) or {}
                    ok = sum(1 for t in tabs if t.get("status") == "ok")
                    done = sum(1 for t in tabs
                               if t.get("status") in ("ok", "fail", "deadline"))
                    runners.append({
                        "runner": rid, "ok": ok, "done": done,
                        "tabs": len(tabs),
                        "watching": sum(1 for t in tabs if t.get("status") == "watching"),
                        "parked": sum(1 for t in tabs if t.get("status") == "parked"),
                        "pushed_at": d.get("pushed_at"),
                        "pool_live": meta.get("pool_live"),
                        "pool_candidates": meta.get("pool_candidates"),
                        "url": meta.get("url"),
                    })
                    tot_ok += ok
                    tot_done += done
                    tot_tabs += len(tabs)
                runners.sort(key=lambda r: int(r["runner"])
                             if str(r["runner"]).isdigit() else 999)
                self._send_json({"runners": runners, "fleet": {
                    "runners": len(runners), "ok": tot_ok,
                    "done": tot_done, "tabs": tot_tabs},
                    "cached_at": _LIVE_DECK_CACHE["at"]})
            except Exception as e:
                self._send_json({"error": f"live deck failed: {e}"}, 500)
        elif self.path.startswith("/api/policy"):
            # Every tunable number, dashboard-editable (absolute control).
            # GET /api/policy                    -> read
            # GET /api/policy?tabs_per_runner=20 -> set one or many
            from urllib.parse import urlparse, parse_qs
            try:
                from hub import db
                db.init()
                qs = parse_qs(urlparse(self.path).query)
                changed = {}
                for k, v in qs.items():
                    raw = v[0]
                    try:
                        val = int(raw)
                    except ValueError:
                        val = raw.lower() in ("1", "true", "yes")
                    db.set_policy(k, val)
                    changed[k] = val
                self._send_json({"ok": True, "policy": db.all_policy(), "changed": changed})
            except Exception as e:
                self._send_json({"error": f"policy failed: {e}"}, 500)
        elif self.path.startswith("/api/rack"):
            from urllib.parse import urlparse, parse_qs
            try:
                from hub import db
                db.init()
                qs = parse_qs(urlparse(self.path).query)
                region = qs.get("region", [None])[0]
                t1_only = qs.get("t1", ["0"])[0] in ("1", "true")
                limit = int(qs.get("limit", ["100"])[0])
                self._send_json({"gold": db.fetch_gold(region, t1_only, limit),
                                 "stats": db.rack_stats()})
            except Exception as e:
                self._send_json({"error": f"rack failed: {e}"}, 500)
        elif self.path == "/api/rack/purge":
            # Hard-delete burned exits (the Purge button).
            # Optional ?days=N to purge only burns older than N days.
            from urllib.parse import urlparse, parse_qs
            try:
                from hub import db
                db.init()
                qs = parse_qs(urlparse(self.path).query)
                days = qs.get("days", [None])[0]
                res = db.purge_burned(older_than_days=float(days) if days else None)
                self._send_json({"ok": True, "message":
                                 f"Purged {res['purged']} burned IPs "
                                 f"({res['remaining_burned']} left)", **res})
            except Exception as e:
                self._send_json({"ok": False, "error": f"purge failed: {e}"}, 500)

        elif self.path == "/api/accounts":
            # Registry accounts (safe fields only, never tokens)
            try:
                from hub import db
                db.init()
                if not db.list_accounts(only_enabled=False):
                    for a in load_accounts():      # first run: seed from env/accounts.json
                        db.upsert_account(a["name"], a["repo"])
                self._send_json({
                    "accounts": [{k: a[k] for k in ("name", "repo", "max_jobs",
                                                    "tab_runners", "harvest_runners",
                                                    "enabled")}
                                 for a in db.list_accounts(only_enabled=False)],
                    "fleet": db.fleet_capacity(),
                })
            except Exception as e:
                self._send_json({"error": f"accounts failed: {e}"}, 500)
        elif self.path.startswith("/api/ci_reports"):
            # Pull run reports + screenshots from GitHub Actions artifacts.
            # Query: ?repo=owner/name&run_id=latest (needs GITHUB_TOKEN env)
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            repo = qs.get("repo", [os.environ.get("GITHUB_REPOSITORY", "")])[0]
            token = os.environ.get("GITHUB_TOKEN", "")
            if not repo or not token:
                self._send_json({"error": "Set GITHUB_REPOSITORY and GITHUB_TOKEN env vars"}, 400)
                return
            try:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Ghost-Dashboard",
                }
                # Latest run for the workflow
                runs_url = f"https://api.github.com/repos/{repo}/actions/workflows/cloud_ghost_watch.yml/runs?per_page=1"
                req = urllib.request.Request(runs_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    runs = json.loads(resp.read().decode("utf-8"))
                if not runs.get("workflow_runs"):
                    self._send_json({"error": "No runs found"}, 404)
                    return
                run = runs["workflow_runs"][0]
                run_id = run["id"]
                # List artifacts
                art_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts"
                req = urllib.request.Request(art_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    arts = json.loads(resp.read().decode("utf-8"))
                self._send_json({
                    "run_id": run_id,
                    "run_number": run.get("run_number"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "artifacts": [
                        {"id": a["id"], "name": a["name"], "size": a["size_in_bytes"]}
                        for a in arts.get("artifacts", [])
                    ],
                })
            except Exception as e:
                self._send_json({"error": f"GitHub API failed: {e}"}, 500)
        elif self.path.startswith("/api/ci_live"):
            # Live tunnel URLs, grepped from streaming job logs (no extra infra).
            # Query: ?repo=owner/name (needs GITHUB_TOKEN env)
            import re
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            repo = qs.get("repo", [os.environ.get("GITHUB_REPOSITORY", "")])[0]
            token = os.environ.get("GITHUB_TOKEN", "")
            if not repo or not token:
                self._send_json({"error": "Set GITHUB_REPOSITORY and GITHUB_TOKEN env vars"}, 400)
                return
            try:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Ghost-Dashboard",
                }

                def _get(url, timeout=15):
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read().decode("utf-8", "replace")

                runs = json.loads(_get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/cloud_ghost_watch.yml/runs?per_page=1"))
                if not runs.get("workflow_runs"):
                    self._send_json({"error": "No runs found"}, 404)
                    return
                run = runs["workflow_runs"][0]
                jobs = json.loads(_get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs"))
                out = []
                for job in jobs.get("jobs", []):
                    tunnel = ""
                    try:
                        logs = _get(
                            f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}/logs",
                            timeout=20)
                        m = re.search(r"TUNNEL_URL=(https://[a-zA-Z0-9\-.]+\.trycloudflare\.com)", logs)
                        if m:
                            tunnel = m.group(1)
                    except Exception:
                        pass
                    out.append({
                        "id": job.get("id"),
                        "name": job.get("name"),
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion"),
                        "tunnel_url": tunnel,
                        "started_at": job.get("started_at"),
                        "completed_at": job.get("completed_at"),
                        "steps": [
                            {"name": s.get("name"), "status": s.get("status"),
                             "conclusion": s.get("conclusion")}
                            for s in (job.get("steps") or [])
                        ],
                    })
                self._send_json({
                    "run_id": run["id"],
                    "run_number": run.get("run_number"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "jobs": out,
                })
            except Exception as e:
                self._send_json({"error": f"GitHub API failed: {e}"}, 500)
        elif self.path.startswith("/api/ci_logs"):
            # Raw CI log tail for one job of the latest run (log viewer).
            # Query: ?repo=owner/name&job_id=<id>&tail=6000 (needs GITHUB_TOKEN env).
            # job_id omitted -> first job of the latest run.
            import re
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            repo = qs.get("repo", [os.environ.get("GITHUB_REPOSITORY", "")])[0]
            token = os.environ.get("GITHUB_TOKEN", "")
            if not repo or not token:
                self._send_json({"error": "Set GITHUB_REPOSITORY and GITHUB_TOKEN env vars"}, 400)
                return
            try:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Ghost-Dashboard",
                }

                def _get(url, timeout=15):
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read().decode("utf-8", "replace")

                runs = json.loads(_get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/cloud_ghost_watch.yml/runs?per_page=1"))
                if not runs.get("workflow_runs"):
                    self._send_json({"error": "No runs found"}, 404)
                    return
                run = runs["workflow_runs"][0]
                jobs = json.loads(_get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs"))
                job_list = jobs.get("jobs", [])
                if not job_list:
                    self._send_json({"error": "No jobs in latest run"}, 404)
                    return
                job_id = qs.get("job_id", [""])[0]
                job = next((j for j in job_list if str(j["id"]) == job_id), job_list[0])
                logs = _get(
                    f"https://api.github.com/repos/{repo}/actions/jobs/{job['id']}/logs",
                    timeout=30)
                tail = int(qs.get("tail", ["6000"])[0])
                self._send_json({
                    "run_id": run["id"],
                    "run_number": run.get("run_number"),
                    "job_id": job["id"],
                    "job_name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "log_tail": logs[-tail:],
                })
            except Exception as e:
                self._send_json({"error": f"GitHub API failed: {e}"}, 500)
        else:
            self._send_json({"error": "Not Found"}, 404)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(post_body)
        except Exception:
            payload = {}

        if self.path == "/api/run_control/cancel":
            # The Stop Run button (ui POST /api/run_control/cancel) hit a 404
            # because the cancel logic only lived on the GET branch. Same code,
            # now reachable via POST (and still via GET for old callers).
            repo = payload.get("repo") or os.environ.get("GITHUB_REPOSITORY", _cfg.REPO)
            token = payload.get("ghToken") or os.environ.get("GITHUB_TOKEN", "")
            try:
                from hub import gh
                run = gh.latest_run(repo, _cfg.WORKFLOW_WATCH, token)
                if not run:
                    self._send_json({"ok": False, "error": "no run found"}, 404)
                elif run.get("status") not in ("in_progress", "queued"):
                    self._send_json({"ok": False,
                                     "error": f"run already {run.get('status')}"})
                else:
                    ok = gh.cancel_run(repo, run["id"], token)
                    self._send_json({"ok": ok,
                                     "message": f"cancel {'sent' if ok else 'failed'} "
                                                f"for run #{run.get('run_number')}"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)

        if self.path == "/api/feed/update":
            # Public relay URL (cloudflared) can hit this when the tunnel is up,
            # so this endpoint is the one public surface: require a shared secret.
            auth = self.headers.get("Authorization", "")
            expected = os.environ.get("RELAY_TOKEN", "")
            allowed = not expected or auth in (f"Bearer {expected}", f"Token {expected}")
            if not allowed:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            tab_id = str(payload.get("tabId", "1"))
            payload["_last_seen"] = time.time()
            with _LIVE_LOCK:
                _LIVE_TABS[tab_id] = payload
            self._send_json({"ok": True})

        elif self.path == "/api/claim":
            # central one-tab-per-IP lease, served from the HUB (the public
            # dashboard address is what fleet runners reach via the tunnel).
            auth = self.headers.get("Authorization", "")
            expected = os.environ.get("RELAY_TOKEN", "")
            allowed = not expected or auth in (f"Bearer {expected}", f"Token {expected}")
            if not allowed:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            owner = str(payload.get("owner", ""))
            if not owner:
                self._send_json({"error": "missing owner"}, 400)
                return
            from hub.claims import lease as _lease
            cap = int(payload.get("cap_per_ip", "1"))
            lease = _lease(owner, cap)
            if not lease:
                self._send_json({"ok": False, "error": "no_free_lease"}, 409)
            else:
                self._send_json({"ok": True, "exit": lease})

        elif self.path == "/api/release":
            auth = self.headers.get("Authorization", "")
            expected = os.environ.get("RELAY_TOKEN", "")
            allowed = not expected or auth in (f"Bearer {expected}", f"Token {expected}")
            if not allowed:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            from hub.claims import release as _release
            self._send_json({"ok": _release(payload.get("addr"), payload.get("owner"))})

        elif self.path == "/api/run_local":
            url = payload.get("url")
            tabs = int(payload.get("tabs", 2))
            if not url:
                self._send_json({"error": "Missing video url"}, 400)
                return
            
            # Spawn local ghost watch in background process
            cmd = [
                sys.executable,
                str(ROOT / "ghost_watch.py"),
                "--url", url,
                "--tabs", str(tabs),
                "--direct",
                "--stream-target", f"http://127.0.0.1:{self.server.server_port}",
            ]
            subprocess.Popen(cmd, cwd=str(ROOT))
            self._send_json({"ok": True, "message": f"Spawned {tabs} local test tabs"})

        elif self.path == "/api/spawn_cloud":
            url = payload.get("url")
            tabs = int(payload.get("tabs", 10))
            stream_target = payload.get("streamTarget", "") or \
                os.environ.get("RELAY_URL", "")
            if not url:
                self._send_json({"error": "Missing video URL"}, 400)
                return

            # The frame feed only reaches the dashboard when the runners get a
            # reachable stream_target. In the old path stream_target defaulted
            # to "" so cloud frames went to a local file nobody reads and the
            # Live Browser Screen grid stayed empty. RELAY_URL (set when the
            # relay+cloudflared tunnel is up) is now forwarded on dispatch;
            # fall back to the relay's tunnel when RELAY_TOKEN is defined.
            if stream_target and "http" not in stream_target:
                stream_target = f"http://{stream_target}"

            # Optional control-plane overrides; defaults give the full fleet
            # (18 watch : 2 harvest = the 20-job ceiling, with unique IPs and
            # 20-45% watch depth). The old call path sent only url+tabs, so a
            # dashboard click silently ran 4 runners instead of the fleet.
            d_runners = payload.get("runners")
            d_shards = payload.get("harvestShards", 2)
            d_lo = payload.get("depthLo", 20)
            d_hi = payload.get("depthHi", 45)
            d_human = payload.get("humanMoves") not in ("false", False, "0")
            d_unique = payload.get("uniqueIps") not in ("false", False, "0")

            # Explicit single target (prompt flow) OR the env/default target.
            # Prefer the operator's saved token (data/.gh_token) so a fresh
            # dashboard doesn't force the prompt dialog every dispatch.
            d_token = payload.get("ghToken") or resolve_github_token()
            d_repo = payload.get("repo") or os.environ.get("GITHUB_REPOSITORY", _cfg.REPO)
            targets = []
            if d_token and d_repo:
                targets.append({"name": "manual", "repo": d_repo, "pat": d_token})
            elif payload.get("account"):
                for a in load_accounts():
                    if a["name"] == payload["account"]:
                        targets.append(a)
                if not targets:
                    self._send_json({"error": "Unknown account"}, 400)
                    return
            else:
                targets = load_accounts()

            if not targets:
                self._send_json({
                    "error": "No accounts configured. Set GITHUB_TOKEN + GITHUB_REPOSITORY env or create accounts.json (see accounts.example.json)."
                }, 400)
                return

            results = [dispatch_workflow(
                           t["repo"], t["pat"], url, tabs, stream_target,
                           runners=d_runners, harvest_shards=d_shards,
                           depth_lo=d_lo, depth_hi=d_hi,
                           human_moves=d_human, unique_ips=d_unique)
                       for t in targets]
            ok_all = all(r[0] for r in results)
            if ok_all or any(r[0] for r in results):
                # watch is going -> wake the harvester immediately (user rule:
                # the factory runs while a watcher run is live)
                threading.Thread(target=_spawn_factory, args=("spawn_cloud",),
                                 daemon=True).start()
            self._send_json({
                "ok": ok_all,
                "dispatched": sum(1 for r in results if r[0]),
                "total": len(results),
                "details": [r[1] for r in results],
            })

        elif self.path == "/api/factory/start":
            with _FACTORY_LOCK:
                _FACTORY["manual_stop"] = False
            ok = _spawn_factory("manual start")
            self._send_json({"ok": ok, "message":
                             "Factory started" if ok else "Factory already running"})

        elif self.path == "/api/factory/stop":
            with _FACTORY_LOCK:
                _FACTORY["manual_stop"] = True
            _terminate_factory("manual stop")
            self._send_json({"ok": True, "message": "Factory stopped (won't auto-restart until Start)"})

        elif self.path == "/api/harvest":
            # Trigger manual IP harvest in background thread.
            # The pipeline's own --timecap (default 1800s) governs the hunt;
            # this outer timeout must exceed it or we kill the harvest before
            # it can merge anything (a real run takes ~50 min).
            def _harvest():
                try:
                    subprocess.run([sys.executable, "-m", "ipshelf.hunt.harvest"],
                                   cwd=str(ROOT),
                                   timeout=int(os.environ.get("HARVEST_TIMEOUT", "2000")))
                except Exception:
                    pass
            threading.Thread(target=_harvest, daemon=True).start()
            self._send_json({"ok": True, "message": "IP Harvesting started in background"})

        else:
            self._send_json({"error": "Unknown endpoint"}, 404)


def run_server(port=8766):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Default to loopback: every endpoint is unauthenticated, so exposing
    # 0.0.0.0 lets any LAN peer (or any webpage via CORS *) dispatch runs,
    # burn Actions minutes and read telemetry. Set DASHBOARD_HOST=0.0.0.0
    # only if you deliberately want other devices to reach the dashboard.
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    # Harvester auto-start is OPT-IN. The work runs on GitHub runners; a
    # dashboard that silently started gold_factory on this PC as soon as it
    # was opened ran a harvest on the operator's machine and drained the local
    # rack (rule: no system runs on the PC). Set DASHBOARD_AUTOSTART_HARVEST=1
    # to restore the old behaviour deliberately.
    autostart = os.environ.get("DASHBOARD_AUTOSTART_HARVEST", "0") == "1"
    if autostart:
        threading.Thread(target=factory_manager_loop, daemon=True).start()
        _factory_log("dashboard up — factory manager watching for live watcher runs")
    else:
        _factory_log("dashboard up — local harvester auto-start DISABLED "
                     "(DASHBOARD_AUTOSTART_HARVEST=1 to enable)")
    print(f"============================================================")
    print(f"[*] GHOST RUNNER DASHBOARD LIVE: http://{host}:{port}")
    print(f"[*] cloud runs are controlled here; local harvester auto-start "
          f"{'ON' if autostart else 'OFF'}")
    if host == "0.0.0.0":
        print("[!] WARNING: dashboard is exposed to the network with NO auth.")
    print(f"============================================================")
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8766))
    # The dashboard is the control panel for cloud runs: resolve a token and
    # the repo slug up front so /api/run_control, /api/ci_live and dispatch
    # all work without hand-exporting env vars.
    if not os.environ.get("GITHUB_TOKEN"):
        tok = resolve_github_token()
        if tok:
            os.environ["GITHUB_TOKEN"] = tok
            print(f"[*] GITHUB_TOKEN resolved (len {len(tok)}) — run control enabled")
        else:
            print("[!] no GITHUB_TOKEN found (env, git credential store, "
                  "data/.gh_token) — run control disabled")
    if not os.environ.get("GITHUB_REPOSITORY"):
        slug = repo_slug_from_git()
        if slug:
            os.environ["GITHUB_REPOSITORY"] = slug
    run_server(port)
