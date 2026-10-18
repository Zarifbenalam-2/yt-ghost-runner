"""relay.py — Runner-side live frame relay for TryCloudflare quick tunnel.

Serves latest tab screenshots (written by LiveStreamer to data/live_feed.json)
over HTTP so `cloudflared tunnel --url http://127.0.0.1:8899` can expose them.
Stdlib only. Token auth via RELAY_TOKEN when set (the public tunnel URL is
guessable/listed, so frames + claims need a shared secret the runners hold).

Endpoints:
  /                simple HTML grid (direct viewing)
  /frame?tab=<id> latest JPEG bytes for a tab
  /api/frames      JSON telemetry for all tabs (CORS open)
  /api/claim       POST {owner}  -> atomically claim a pooled exit for a tab
  /api/release     POST {addr, owner} -> free an exit claim

Usage: python relay.py [--port 8899]
"""
import argparse
import base64
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEED = ROOT / "data" / "live_feed.json"

#: central claim table shared with the dashboard server (hub/claims.py): the
#: hub (your PC) holds the global ONE-TAB-ONE-IP cap across ALL fleet runners
#: here. A runner without a reachable hub falls back to hash slicing
#: (ghost_watch handles it). The TOKEN guard (RELAY_TOKEN) keeps the public
#: tunnel URL from being abused to burn leases.
from hub.claims import CLAIM_TTL  # noqa: E402
from hub.claims import lease as _lease          # noqa: E402
from hub.claims import pool_exits as _pool_exits  # noqa: E402
from hub.claims import release as _release       # noqa: E402


def _release_claim(addr, owner):
    return _release(addr, owner)


def load_feed():
    try:
        return json.loads(FEED.read_text(encoding="utf-8"))
    except Exception:
        return {}


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _auth_ok(self):
        expected = os.environ.get("RELAY_TOKEN", "")
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        return auth in (f"Bearer {expected}", f"Token {expected}")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            feed = load_feed()
            cards = "".join(
                f'<div style="display:inline-block;margin:8px;text-align:center">'
                f'<div>TAB #{t.get("tabId", "?")}</div>'
                f'<img src="/frame?tab={t.get("tabId", "")}" '
                f'style="width:480px;border:1px solid #333"/></div>'
                for t in feed.values() if t.get("image")
            ) or "<p>No frames yet.</p>"
            body = f"<html><head><meta http-equiv='refresh' content='5'></head><body style='background:#111;color:#eee;font-family:sans-serif'><h2>Ghost Runner Live Relay</h2>{cards}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/frame"):
            from urllib.parse import urlparse, parse_qs
            tab = parse_qs(urlparse(self.path).query).get("tab", [""])[0]
            feed = load_feed()
            entry = feed.get(tab, {})
            img = entry.get("image", "")
            if img.startswith("data:image/jpeg;base64,"):
                raw = base64.b64decode(img.split(",", 1)[1])
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_error(404, "no frame")
        elif self.path == "/api/frames":
            feed = load_feed()
            slim = {
                k: {"tabId": v.get("tabId"), "timestamp": v.get("timestamp"),
                    "telemetry": v.get("telemetry"), "has_image": bool(v.get("image"))}
                for k, v in feed.items()
            }
            body = json.dumps(slim).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._json({"claims": len(_CLAIMS),
                        "pool": len(_pool_exits()),
                        "claim_ttl": CLAIM_TTL})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._auth_ok():
            self._json({"error": "Unauthorized"}, 401)
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode("utf-8") if n else "{}"
            payload = json.loads(body)
        except Exception:
            payload = {}

        if self.path == "/api/claim":
            owner = str(payload.get("owner", ""))
            if not owner:
                self._json({"error": "missing owner"}, 400)
                return
            lease = _lease(owner, int(payload.get("cap_per_ip", "1")))
            if not lease:
                self._json({"ok": False, "error": "no_free_lease"}, 409)
            else:
                self._json({"ok": True, "exit": lease})
            return
        if self.path == "/api/release":
            ok = _release_claim(payload.get("addr"), payload.get("owner"))
            self._json({"ok": ok})
            return
        self._json({"error": "Not Found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), RelayHandler)
    print(f"[relay] serving on 127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
