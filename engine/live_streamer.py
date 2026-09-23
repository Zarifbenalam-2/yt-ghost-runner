"""live_streamer.py — Real-time frame and telemetry broadcaster.

Streams compressed live browser screen previews (base64 JPEG) and playback metrics
from cloud GitHub Actions runner VMs (or local test sessions) to the live dashboard.
"""
import base64
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED_DATA = ROOT / "data" / "live_feed.json"


class LiveStreamer:
    """Non-blocking streamer for live browser frames and telemetry."""

    def __init__(self, tab_id, stream_target_url=None):
        self.tab_id = str(tab_id)
        self.stream_target_url = stream_target_url or os.environ.get("STREAM_TARGET_URL")
        self._last_sent = 0.0

    def push_frame(self, page, telemetry, min_interval=1.0):
        """Capture JPEG screenshot and dispatch asynchronously."""
        now = time.time()
        if now - self._last_sent < min_interval:
            return
        self._last_sent = now

        try:
            # Capture viewport frame as JPEG bytes (lightweight)
            img_bytes = page.screenshot(type="jpeg", quality=55)
            b64_img = base64.b64encode(img_bytes).decode("ascii")
            
            payload = {
                "tabId": self.tab_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "image": f"data:image/jpeg;base64,{b64_img}",
                "telemetry": telemetry or {},
            }

            # Send in background thread so playback is never delayed
            threading.Thread(target=self._dispatch, args=(payload,), daemon=True).start()
        except Exception:
            pass

    def _dispatch(self, payload):
        """Send payload to remote dashboard or write to local feed data."""
        # 1. Update local feed cache
        try:
            FEED_DATA.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if FEED_DATA.exists():
                try:
                    existing = json.loads(FEED_DATA.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            existing[self.tab_id] = {
                "tabId": self.tab_id,
                "timestamp": payload["timestamp"],
                "image": payload["image"],
                "telemetry": payload["telemetry"],
            }
            # Unique tmp per write: several tab threads share this file, a
            # fixed tmp name lets two writers interleave (lost/crossed frames)
            tmp = FEED_DATA.with_name(
                f"live_feed.{os.getpid()}.{threading.get_ident()}.tmp.json")
            tmp.write_text(json.dumps(existing), encoding="utf-8")
            tmp.replace(FEED_DATA)
        except Exception:
            pass

        # 2. If remote stream target configured, HTTP POST to dashboard endpoint
        if self.stream_target_url:
            try:
                endpoint = self.stream_target_url.rstrip("/") + "/api/feed/update"
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    pass
            except Exception:
                pass
