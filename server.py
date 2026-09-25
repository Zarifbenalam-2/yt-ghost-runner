"""server.py — Ghost Runner Central Dashboard Server & Action Dispatcher.

Features:
1. Live Tab Screen Grid HUD (real-time stream feed of all active tabs).
2. GitHub Actions Spawner (triggers workflow_dispatch via GitHub REST API).
3. Local Test Tab Runner (spawns local background ghost tabs for instant verification).
4. Manual IP Harvest & Pool Management.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI_INDEX = ROOT / "ui" / "index.html"
DATA_DIR = ROOT / "data"
FEED_DATA = DATA_DIR / "live_feed.json"

# In-memory live tab frame store
_LIVE_TABS = {}
_LIVE_LOCK = threading.Lock()

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


def factory_manager_loop():
    """Every 60s: watcher live -> factory runs; watcher gone -> factory stops."""
    from gold_factory import watcher_active
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
        except Exception as e:
            _factory_log(f"manager error: {type(e).__name__}: {e}")
        time.sleep(60)


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


def dispatch_workflow(repo, token, url, tabs, stream_target=""):
    """POST workflow_dispatch to one repo. Returns (ok, message)."""
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
            "stream_target": stream_target,
        },
    }).encode("utf-8")
    try:
        req = urllib.request.Request(dispatch_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True, f"{repo}: dispatched"
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
        elif self.path == "/api/accounts":
            # Safe list: names + repos only, never tokens
            self._send_json({
                "accounts": [
                    {"name": a["name"], "repo": a["repo"]}
                    for a in load_accounts()
                ],
            })
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

        if self.path == "/api/feed/update":
            tab_id = str(payload.get("tabId", "1"))
            payload["_last_seen"] = time.time()
            with _LIVE_LOCK:
                _LIVE_TABS[tab_id] = payload
            self._send_json({"ok": True})

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
            stream_target = payload.get("streamTarget", "")
            if not url:
                self._send_json({"error": "Missing video URL"}, 400)
                return

            # Explicit single target (prompt flow) or fan-out to all accounts
            targets = []
            if payload.get("ghToken") and payload.get("repo"):
                targets.append({
                    "name": "manual",
                    "repo": payload["repo"],
                    "pat": payload["ghToken"],
                })
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

            results = [dispatch_workflow(t["repo"], t["pat"], url, tabs, stream_target)
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
    # Factory manager: harvester auto-runs only while a watcher run is live
    threading.Thread(target=factory_manager_loop, daemon=True).start()
    _factory_log("dashboard up — factory manager watching for live watcher runs")
    print(f"============================================================")
    print(f"[*] GHOST RUNNER DASHBOARD LIVE: http://{host}:{port}")
    print(f"[*] FACTORY MANAGER: harvester auto-starts while a watch is live")
    if host == "0.0.0.0":
        print("[!] WARNING: dashboard is exposed to the network with NO auth.")
    print(f"============================================================")
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8766))
    run_server(port)
