"""relay.py — Runner-side live frame relay for TryCloudflare quick tunnel.

Serves latest tab screenshots (written by LiveStreamer to data/live_feed.json)
over HTTP so `cloudflared tunnel --url http://127.0.0.1:8899` can expose them.
Stdlib only. No auth (tunnel URL is unguessable; job-lifetime only).

Endpoints:
  /                simple HTML grid (direct viewing)
  /frame?tab=<id> latest JPEG bytes for a tab
  /api/frames      JSON telemetry for all tabs (CORS open)

Usage: python relay.py [--port 8899]
"""
import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEED = ROOT / "data" / "live_feed.json"


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
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), RelayHandler)
    print(f"[relay] serving on 127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
