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
            active_tabs = {k: v for k, v in _LIVE_TABS.items() if (now - v.get("_last_seen", 0)) < 45}
            self._send_json({
                "ipStats": ip_stats,
                "tabs": list(active_tabs.values()),
                "tabCount": len(active_tabs),
            })
        elif self.path == "/api/feed":
            self._send_json({"tabs": list(_LIVE_TABS.values())})
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
            gh_token = payload.get("ghToken") or os.environ.get("GITHUB_TOKEN")
            repo = payload.get("repo") or os.environ.get("GITHUB_REPOSITORY")  # e.g. "owner/repo"
            url = payload.get("url")
            total_tabs = int(payload.get("tabs", 10))

            if not url:
                self._send_json({"error": "Missing video URL"}, 400)
                return

            if not gh_token or not repo:
                self._send_json({
                    "error": "GitHub Token or Repository not configured in settings. Provide 'ghToken' and 'repo' (owner/name)."
                }, 400)
                return

            # Trigger GitHub Actions workflow dispatch via REST API
            workflow_id = "cloud_ghost_watch.yml"
            dispatch_url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/dispatches"
            
            headers = {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Ghost-Dashboard",
                "Content-Type": "application/json",
            }
            body = json.dumps({
                "ref": "main",
                "inputs": {
                    "video_url": url,
                    "tabs_per_runner": "5",
                    "stream_target": payload.get("streamTarget", ""),
                }
            }).encode("utf-8")

            try:
                req = urllib.request.Request(dispatch_url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self._send_json({"ok": True, "message": "GitHub Actions workflow dispatched successfully!"})
            except Exception as e:
                self._send_json({"error": f"Failed to dispatch: {str(e)}"}, 500)

        elif self.path == "/api/harvest":
            # Trigger manual IP harvest in background thread
            def _harvest():
                try:
                    subprocess.run([sys.executable, "-m", "ipshelf.hunt.harvest"], cwd=str(ROOT), timeout=300)
                except Exception:
                    pass
            threading.Thread(target=_harvest, daemon=True).start()
            self._send_json({"ok": True, "message": "IP Harvesting started in background"})

        else:
            self._send_json({"error": "Unknown endpoint"}, 404)


def run_server(port=8766):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"============================================================")
    print(f"[*] GHOST RUNNER DASHBOARD LIVE: http://127.0.0.1:{port}")
    print(f"============================================================")
    server.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8766))
    run_server(port)
